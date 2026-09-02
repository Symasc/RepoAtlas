"""Secrets, ignore rules and pathological files (V1_SPEC §5, §17, §20).

Repository content is untrusted input, and "untrusted" covers three separate
hazards that fail differently:

* **secrets** must never be read at all — the guarantee is exclusion at
  enumeration, so nothing downstream can leak what it never saw;
* **ignored content** must leave no trace, including after an ignore rule is added
  to a file that was already indexed;
* **pathological files** — binary, undecodable, empty, one 200 KB line, minified,
  huge JSON — must not crash a sync, be mistaken for source, or cost resources out
  of proportion to their value.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.adversarial.conftest import IGNORED_MARKERS, SECRET_SENTINEL

pytestmark = pytest.mark.adversarial

SECRET_FILES = (
    ".env",
    ".env.production",
    "secret.pem",
    "private.key",
    "id_rsa",
    "id_ed25519",
    "src/nested/deeply/hidden/service.pem",
    "src/nested/deeply/hidden/.env.local",
)


def indexed_paths(workspace: Workspace, repo: str) -> set[str]:
    rows = workspace.database.execute(
        "SELECT f.rel_path FROM files f JOIN repositories r ON r.id = f.repo_id WHERE r.name = ?",
        (repo,),
    ).fetchall()
    return {row["rel_path"] for row in rows}


def _sync(workspace: Workspace, root: Path, repo: str | None = None):
    target = root if repo is None else root / repo
    return sync(workspace, workspace.resolver().resolve(target))


# -- secrets -----------------------------------------------------------------


@pytest.mark.parametrize("path", SECRET_FILES)
def test_no_secret_file_is_enumerated(adversarial: Workspace, path: str) -> None:
    assert path not in indexed_paths(adversarial, "python-service")


def test_a_secret_nested_deeply_is_excluded_by_name_not_by_location(
    adversarial: Workspace,
) -> None:
    """The patterns match the file name, so depth is irrelevant — which is what
    makes a key sitting beside the code it serves safe."""
    paths = indexed_paths(adversarial, "python-service")
    assert not [p for p in paths if p.startswith("src/nested/deeply/hidden/")]


@pytest.mark.parametrize(
    "table,column",
    [("chunks", "text"), ("entities", "key"), ("entities", "name")],
)
def test_the_sentinel_reaches_no_stored_column(
    adversarial: Workspace, table: str, column: str
) -> None:
    rows = adversarial.database.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {column} LIKE ?",  # noqa: S608 - fixed names
        (f"%{SECRET_SENTINEL}%",),
    ).fetchone()
    assert rows["n"] == 0


def test_the_sentinel_is_not_searchable_by_any_route(adversarial: Workspace) -> None:
    assert adversarial.search.search_literal(SECRET_SENTINEL) == []
    assert adversarial.search.search_text(SECRET_SENTINEL, None, 20) == []
    assert adversarial.search.find_entities_by_name([SECRET_SENTINEL], None, 20) == []


def test_no_embedding_was_computed_from_secret_text(adversarial: Workspace) -> None:
    rows = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM chunks c JOIN embeddings e "
        "ON e.content_hash = c.content_hash WHERE c.text LIKE ?",
        (f"%{SECRET_SENTINEL}%",),
    ).fetchone()
    assert rows["n"] == 0


def test_a_secret_created_after_the_first_index_is_still_excluded(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    late = adversarial_root / "deployment" / "runtime.pem"
    late.write_text(f"-----BEGIN CERTIFICATE-----\n{SECRET_SENTINEL}_LATE\n")
    _sync(adversarial, adversarial_root, "deployment")
    assert "runtime.pem" not in indexed_paths(adversarial, "deployment")
    assert adversarial.search.search_literal(f"{SECRET_SENTINEL}_LATE") == []


def test_a_file_that_becomes_a_secret_by_rename_loses_its_facts(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """An indexed file renamed to a secret pattern must be removed, not orphaned."""
    ordinary = adversarial_root / "deployment" / "settings.yaml"
    ordinary.write_text("marker: RENAMED_TO_SECRET_MARKER\n")
    _sync(adversarial, adversarial_root, "deployment")
    assert adversarial.search.search_literal("RENAMED_TO_SECRET_MARKER")

    ordinary.rename(adversarial_root / "deployment" / "settings.pem")
    _sync(adversarial, adversarial_root, "deployment")
    assert adversarial.search.search_literal("RENAMED_TO_SECRET_MARKER") == []
    paths = indexed_paths(adversarial, "deployment")
    assert "settings.yaml" not in paths and "settings.pem" not in paths


# -- ignore rules ------------------------------------------------------------


@pytest.mark.parametrize("marker", IGNORED_MARKERS)
def test_ignored_content_is_never_searchable(adversarial: Workspace, marker: str) -> None:
    assert adversarial.search.search_literal(marker) == []


def test_an_ignore_rule_added_later_removes_the_facts_it_now_covers(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The dangerous direction: content indexed *before* the rule existed."""
    repo = adversarial_root / "python-service"
    (repo / "temporary.py").write_text("BECOMES_IGNORED_MARKER = 1\n")
    _sync(adversarial, adversarial_root, "python-service")
    assert adversarial.search.search_literal("BECOMES_IGNORED_MARKER")

    (repo / ".repoatlasignore").write_text("generated/\n*.generated.json\ntemporary.py\n")
    _sync(adversarial, adversarial_root, "python-service")
    assert adversarial.search.search_literal("BECOMES_IGNORED_MARKER") == []
    assert "temporary.py" not in indexed_paths(adversarial, "python-service")


def test_removing_an_ignore_rule_indexes_what_it_was_hiding(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    repo = adversarial_root / "python-service"
    assert adversarial.search.search_literal("IGNORED_MARKER_MUST_NOT_BE_INDEXED") == []

    (repo / ".repoatlasignore").write_text("*.generated.json\n")
    _sync(adversarial, adversarial_root, "python-service")
    assert adversarial.search.search_literal("IGNORED_MARKER_MUST_NOT_BE_INDEXED")


def test_a_gitignored_file_is_not_indexed_even_though_it_is_on_disk(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    assert (adversarial_root / "python-service" / "gitignored" / "g.py").exists()
    assert adversarial.search.search_literal("GITIGNORED_MARKER_MUST_NOT_BE_INDEXED") == []


def test_secrets_win_over_a_permissive_ignore_file(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Secret patterns are applied last and unconditionally (§5 precedence)."""
    repo = adversarial_root / "python-service"
    (repo / ".repoatlasignore").write_text("!.env\n!*.pem\n")
    _sync(adversarial, adversarial_root, "python-service")
    assert ".env" not in indexed_paths(adversarial, "python-service")
    assert adversarial.search.search_literal(SECRET_SENTINEL) == []


# -- pathological files ------------------------------------------------------


def test_the_ecosystem_with_every_hostile_file_indexes_without_error(
    adversarial: Workspace,
) -> None:
    """The fixture already contains all of them; reaching this assertion at all
    means `init` survived binary, undecodable, empty, huge and minified input."""
    assert adversarial.repositories.list_all()


def test_binary_files_are_not_treated_as_source(adversarial: Workspace) -> None:
    for name in ("binary.bin", "image.png"):
        rows = adversarial.database.execute(
            "SELECT f.is_binary, f.language FROM files f "
            "JOIN repositories r ON r.id = f.repo_id "
            "WHERE r.name = 'python-service' AND f.rel_path = ?",
            (name,),
        ).fetchall()
        if rows:
            assert rows[0]["is_binary"], name
    assert adversarial.search.search_literal("BINARY_MARKER_NOT_SOURCE") == []
    assert adversarial.search.search_literal("PNG_MARKER_NOT_SOURCE") == []


def test_undecodable_bytes_do_not_abort_the_repository(adversarial: Workspace) -> None:
    """`invalid_utf8.py` has a .py extension and undecodable bytes. Whatever is
    done with it, the rest of the repository must still be indexed."""
    paths = indexed_paths(adversarial, "python-service")
    assert "src/config.py" in paths
    assert "src/app.py" in paths


def test_an_empty_file_produces_no_symbols_and_no_chunks(adversarial: Workspace) -> None:
    """The file and module entities are legitimate — an empty Python file is still
    an importable module. Nothing is derived from content that is not there."""
    symbols = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE f.rel_path = 'empty.py' AND e.type NOT IN ('file', 'module')"
    ).fetchone()["n"]
    chunks = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM chunks c JOIN files f ON f.id = c.file_id "
        "WHERE f.rel_path = 'empty.py'"
    ).fetchone()["n"]
    assert (symbols, chunks) == (0, 0)


def test_a_two_hundred_kilobyte_single_line_is_bounded(adversarial: Workspace) -> None:
    """One enormous line must not become one enormous chunk in a prompt. The cap
    and its rationale live in tests/adversarial/test_chunk_bounds.py."""
    cap = adversarial.settings.indexing.max_chunk_chars
    rows = adversarial.database.execute(
        "SELECT LENGTH(c.text) AS size FROM chunks c JOIN files f ON f.id = c.file_id "
        "WHERE f.rel_path = 'longline.py'"
    ).fetchall()
    assert rows
    assert all(row["size"] <= cap for row in rows)


def test_a_large_generated_json_does_not_dominate_the_index(
    adversarial: Workspace,
) -> None:
    """4000 entries of machine-written JSON is not 4000 useful facts. The parser
    caps a document at 400 keys and records that it did, which is the honest
    behaviour: bounded, and visible in the diagnostics rather than silent."""
    entities = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE f.rel_path = 'large.json' AND e.type != 'file'"
    ).fetchone()["n"]
    assert entities <= 400, entities


def test_indexing_the_hostile_repository_stays_fast(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """A pathological file must not cost time out of proportion to its value. The
    bound is generous: it catches quadratic behaviour, not milliseconds."""
    started = time.perf_counter()
    _sync(adversarial, adversarial_root, "python-service")
    assert time.perf_counter() - started < 20.0


def test_a_file_growing_past_the_size_cap_is_recorded_but_not_read(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The cap is documented (`indexing.max_file_bytes`), so crossing it changes
    what is stored rather than whether the sync succeeds."""
    huge = adversarial_root / "deployment" / "huge.yaml"
    huge.write_text("marker: OVERSIZED_FILE_MARKER\n" + ("padding: x\n" * 200_000))
    assert huge.stat().st_size > adversarial.settings.indexing.max_file_bytes
    _sync(adversarial, adversarial_root, "deployment")
    assert adversarial.search.search_literal("OVERSIZED_FILE_MARKER") == []
