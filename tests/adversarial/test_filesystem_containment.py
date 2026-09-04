"""The filesystem containment boundary (V1_SPEC §17, ARCHITECTURE.md §12).

The invariant under test:

    Before RepoAtlas opens repository file content, the actual filesystem target
    must be proven to reside within the canonical repository root.

The defect this suite exists to prevent is subtle enough to be worth stating in
full. Git's index records *paths*, not objects. A path can stay in the index
while the working tree beneath it changes shape entirely:

    git add sub/passwd        # sub is a real directory
    rm -rf sub && ln -s /etc sub

``git ls-files --cached`` still reports ``sub/passwd``. The object that path now
names is ``/etc/passwd``, and it is a perfectly ordinary regular file — so
``Path.is_symlink()`` is ``False`` and ``Path.is_file()`` is ``True``. Checking
only the final component therefore admits it, and RepoAtlas reads, chunks,
indexes, embeds and cites foreign content under a repository path that does not
contain it.

Every test here uses a **real filesystem and a real Git repository**. Mocking
``Path.resolve`` or ``GitClient`` would test the mock: the whole defect lives in
the gap between what git reports and what the kernel resolves, and only the
kernel can be asked about that.

Assertions come in pairs throughout — the forbidden content is absent *and* the
legitimate content is still there. A containment fix that indexed nothing would
satisfy half of this file and destroy the product.
"""

from __future__ import annotations

import io
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import git

pytestmark = pytest.mark.adversarial

#: Planted in files that live outside every managed repository. None of these may
#: reach any table, index, embedding, evidence set or prompt.
OUTSIDE_CANARY = "P0_OUTSIDE_CANARY_5F3A9"
STATE_CANARY = "P0_STATE_CANARY_C41D7"

#: Planted in ordinary files that must keep working. A containment fix that
#: over-refuses is a different bug, not a fix.
INSIDE_CANARY = "P0_INSIDE_CANARY_8B2E6"


def make_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "--initial-branch=main")


def commit(path: Path, message: str = "c") -> None:
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", message)


def build_outside(tmp_path: Path) -> Path:
    """A directory outside every repository, holding a unique canary."""
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "config.yaml").write_text(f"outside_canary: {OUTSIDE_CANARY}\n", encoding="utf-8")
    (outside / "nested" / "deep.py").write_text(f"X = '{OUTSIDE_CANARY}'\n", encoding="utf-8")
    return outside


def swap_for_symlink(directory: Path, target: Path | str) -> None:
    """Replace a committed directory with a symlink, leaving the index intact.

    This is the shape the escape needs, and it arrives through entirely ordinary
    means: an unpacked archive, a restored backup, a shared mount, or a developer
    replacing a vendored directory with a link.
    """
    for child in sorted(directory.rglob("*"), reverse=True):
        child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
    directory.rmdir()
    directory.symlink_to(target)


@pytest.fixture
def containment_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


@pytest.fixture
def managed_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


def index(settings: Settings, root: Path, tmp_path: Path, tag: str = "cfg") -> Workspace:
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, root, config_path=tmp_path / f"{tag}.yaml")
    return workspace


def indexed_paths(workspace: Workspace) -> set[str]:
    return {
        str(row["rel_path"])
        for row in workspace.database.execute("SELECT rel_path FROM files").fetchall()
    }


def contains_text(workspace: Workspace, needle: str) -> bool:
    """Whether ``needle`` reached any stored text, by any route."""
    database = workspace.database
    chunk_hits = database.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE text LIKE ?", (f"%{needle}%",)
    ).fetchone()["n"]
    entity_hits = database.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE name LIKE ? OR qualified_name LIKE ?",
        (f"%{needle}%", f"%{needle}%"),
    ).fetchone()["n"]
    return bool(chunk_hits or entity_hits)


# --------------------------------------------------------------------------- #
# 1-9. The escape shapes
# --------------------------------------------------------------------------- #


@pytest.fixture
def victim(managed_root: Path, tmp_path: Path) -> Path:
    """The independent review's exact fixture, plus a legitimate control file."""
    outside = build_outside(tmp_path)
    repo = managed_root / "victim"
    make_repository(repo)
    (repo / "sub").mkdir()
    (repo / "sub" / "passwd").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (repo / "st").mkdir()
    (repo / "st" / "config.yaml").write_text("placeholder: 1\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(f"HOST = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)
    swap_for_symlink(repo / "sub", "/etc")
    swap_for_symlink(repo / "st", outside)
    return repo


def test_an_ancestor_symlink_to_etc_yields_no_indexed_file(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    """The reproduction from the independent review, asserted at the file table."""
    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "sub/passwd" not in indexed_paths(workspace)
        assert "src/app.py" in indexed_paths(workspace), "a real file must survive"
    finally:
        workspace.close()


def test_no_etc_content_reaches_any_table(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert not contains_text(workspace, "/usr/sbin/nologin")
        assert not contains_text(workspace, "root:x:0:0")
    finally:
        workspace.close()


def test_an_ancestor_symlink_to_an_ordinary_outside_directory_is_refused(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    """The escape is arbitrary filesystem reach, not something special about /etc."""
    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "st/config.yaml" not in indexed_paths(workspace)
        assert not contains_text(workspace, OUTSIDE_CANARY)
    finally:
        workspace.close()


def test_a_final_component_symlink_is_still_refused(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    """The case that already worked, pinned so a rewrite cannot lose it."""
    outside = build_outside(tmp_path)
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    (repo / "leak.yaml").symlink_to(outside / "config.yaml")
    commit(repo)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "leak.yaml" not in indexed_paths(workspace)
        assert not contains_text(workspace, OUTSIDE_CANARY)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_two_nested_symlink_ancestors_are_refused(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    outside = build_outside(tmp_path)
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "deep.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)
    # a/b -> outside/nested, reached through a second link a -> hop
    swap_for_symlink(repo / "a" / "b", outside / "nested")
    (repo / "hop").symlink_to(repo / "a")

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "a/b/deep.py" not in indexed_paths(workspace)
        assert not contains_text(workspace, OUTSIDE_CANARY)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_a_relative_symlink_climbing_out_of_the_repository_is_refused(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    build_outside(tmp_path)
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "up").mkdir()
    (repo / "up" / "config.yaml").write_text("placeholder: 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)
    swap_for_symlink(repo / "up", Path("..") / ".." / "outside")

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "up/config.yaml" not in indexed_paths(workspace)
        assert not contains_text(workspace, OUTSIDE_CANARY)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_a_symlink_to_the_state_directory_is_refused(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    """The container case: a repository reaching RepoAtlas's own writable volume."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "config.yaml").write_text(f"state_canary: {STATE_CANARY}\n", encoding="utf-8")
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "st").mkdir()
    (repo / "st" / "config.yaml").write_text("placeholder: 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)
    swap_for_symlink(repo / "st", state)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "st/config.yaml" not in indexed_paths(workspace)
        assert not contains_text(workspace, STATE_CANARY)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_a_broken_ancestor_symlink_is_skipped_without_raising(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "gone").mkdir()
    (repo / "gone" / "x.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)
    swap_for_symlink(repo / "gone", tmp_path / "does-not-exist")

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "gone/x.py" not in indexed_paths(workspace)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_a_self_referential_symlink_loop_is_skipped_without_raising(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "loop").mkdir()
    (repo / "loop" / "x.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)
    swap_for_symlink(repo / "loop", "loop")

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "loop/x.py" not in indexed_paths(workspace)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_a_symlink_that_escapes_and_returns_inward_is_still_refused(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    """Leaving the root and coming back is leaving the root.

    The target genuinely resolves inside the repository, so a pure
    ``is_relative_to`` check on the resolved path would admit it. It is refused
    anyway: a link whose *route* passes outside is a link the repository can
    re-aim, and the file is reachable by its real path regardless.
    """
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "real").mkdir()
    (repo / "real" / "inside.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    (repo / "detour").mkdir()
    (repo / "detour" / "inside.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    commit(repo)
    swap_for_symlink(repo / "detour", Path("..") / ".." / "root" / "r" / "real")

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "detour/inside.py" not in indexed_paths(workspace)
        assert "real/inside.py" in indexed_paths(workspace)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_a_repository_root_that_is_itself_a_symlink_still_indexes_its_files(
    tmp_path: Path, containment_settings: Settings
) -> None:
    """Containment is measured against the *resolved* root, not its spelling.

    A managed root reached through a symlink is an ordinary deployment (a home
    directory link, a mounted workspace). Every file inside it must remain
    indexable, or the fix would break more than the defect did.
    """
    real = tmp_path / "real-root"
    real.mkdir()
    repo = real / "r"
    make_repository(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real)

    workspace = index(containment_settings, linked_root, tmp_path)
    try:
        assert "src/app.py" in indexed_paths(workspace)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


# --------------------------------------------------------------------------- #
# 10-12. Legitimate content keeps working
# --------------------------------------------------------------------------- #


def test_ordinary_and_deeply_nested_files_are_unaffected(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "flat.py").write_text(f"A = '{INSIDE_CANARY}'\n", encoding="utf-8")
    deep = repo / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("B = 1\n", encoding="utf-8")
    commit(repo)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        paths = indexed_paths(workspace)
        assert "flat.py" in paths
        assert "a/b/c/d/e/deep.py" in paths
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_paths_with_spaces_and_unicode_are_unaffected(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "with space").mkdir()
    (repo / "with space" / "naïve café.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "配置").mkdir()
    (repo / "配置" / "設定.py").write_text("B = 1\n", encoding="utf-8")
    commit(repo)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        paths = indexed_paths(workspace)
        assert "with space/naïve café.py" in paths
        assert "配置/設定.py" in paths
    finally:
        workspace.close()


# --------------------------------------------------------------------------- #
# 13-14. Transitions and cleanup
# --------------------------------------------------------------------------- #


def test_a_directory_replaced_by_a_symlink_after_indexing_loses_its_facts(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    """The transition, not just the end state: a good file turning into an escape."""
    outside = build_outside(tmp_path)
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "assets").mkdir()
    (repo / "assets" / "config.yaml").write_text("real: 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        assert "assets/config.yaml" in indexed_paths(workspace)
        swap_for_symlink(repo / "assets", outside)
        context = workspace.resolver().resolve(managed_root)
        sync(workspace, context)
        assert "assets/config.yaml" not in indexed_paths(workspace)
        assert not contains_text(workspace, OUTSIDE_CANARY)
        assert contains_text(workspace, INSIDE_CANARY)
    finally:
        workspace.close()


def test_an_ordinary_sync_removes_content_a_weaker_build_indexed(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    """Contaminated-state cleanup, without --full and without touching the database.

    A database written by the vulnerable build holds rows for the escaped paths.
    Nothing marks them: the file list is recomputed from the current policy on
    every sync, so a path the boundary now refuses is simply absent from the scan
    and is classified *deleted* by the ordinary no-stale-facts mechanism.
    """
    contaminated = tmp_path / "state" / "atlas.db"
    contaminated.parent.mkdir(parents=True, exist_ok=True)
    workspace = index(containment_settings, managed_root, tmp_path)
    repo_id = workspace.database.execute("SELECT id FROM repositories").fetchone()["id"]
    try:
        # Simulate the vulnerable build: write the escaped rows directly.
        for rel_path, text in (
            ("sub/passwd", "root:x:0:0:root:/root:/bin/bash\n"),
            ("st/config.yaml", f"outside_canary: {OUTSIDE_CANARY}\n"),
        ):
            cursor = workspace.database.execute(
                "INSERT INTO files (repo_id, rel_path, size, mtime_ns, content_hash, "
                "language, parser, parser_version, is_binary, indexed_at) "
                "VALUES (?, ?, ?, 0, 'stale', 'YAML', 'structured', 'v', 0, '2020-01-01')",
                (repo_id, rel_path, len(text)),
            )
            workspace.database.execute(
                "INSERT INTO chunks (file_id, repo_id, line_start, line_end, text, "
                "token_estimate, content_hash, span_index, split_index, split_total) "
                "VALUES (?, ?, 1, 1, ?, 1, 'h', 0, 0, 1)",
                (cursor.lastrowid, repo_id, text),
            )
        workspace.database.connection.commit()
        assert "sub/passwd" in indexed_paths(workspace)
        assert contains_text(workspace, OUTSIDE_CANARY)

        context = workspace.resolver().resolve(managed_root)
        sync(workspace, context)

        paths = indexed_paths(workspace)
        assert "sub/passwd" not in paths
        assert "st/config.yaml" not in paths
        assert not contains_text(workspace, OUTSIDE_CANARY)
        assert not contains_text(workspace, "root:x:0:0")
        orphans = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN files f ON f.id = c.file_id "
            "WHERE f.id IS NULL"
        ).fetchone()["n"]
        assert orphans == 0
    finally:
        workspace.close()


# --------------------------------------------------------------------------- #
# 15-17. Nothing downstream ever sees foreign content
# --------------------------------------------------------------------------- #


def test_retrieval_returns_no_escaped_evidence(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        context = workspace.resolver().resolve(managed_root)
        for question in ("/usr/sbin/nologin", OUTSIDE_CANARY, "sub/passwd", "st/config.yaml"):
            evidence = workspace.retriever().retrieve(question, context)
            for candidate in evidence.candidates:
                assert candidate.path not in {"sub/passwd", "st/config.yaml"}
                assert OUTSIDE_CANARY not in candidate.text
                assert "/usr/sbin/nologin" not in candidate.text
    finally:
        workspace.close()


def test_the_embedding_backend_never_receives_escaped_content(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    """Asserted at the backend, not at the table: embeddings leave the process."""
    seen: list[str] = []
    workspace = open_workspace(settings=containment_settings, require_root=False)
    try:
        model = workspace.embeddings()
        assert model is not None
        original = model.embed

        def recording(texts: list[str]) -> object:
            seen.extend(list(texts))
            return original(texts)

        model.embed = recording  # type: ignore[method-assign]
        initialize(workspace, managed_root, config_path=tmp_path / "embed.yaml")
        joined = "\n".join(seen)
        assert seen, "the embedding backend must actually have been exercised"
        assert OUTSIDE_CANARY not in joined
        assert "/usr/sbin/nologin" not in joined
        assert INSIDE_CANARY in joined
    finally:
        workspace.close()


def test_the_model_prompt_never_contains_escaped_content(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    from repoatlas.llm.reasoner import QueryIntent, build_messages

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        context = workspace.resolver().resolve(managed_root)
        for question in ("/usr/sbin/nologin", OUTSIDE_CANARY, "configuration"):
            evidence = workspace.retriever().retrieve(question, context)
            messages = build_messages(question, evidence, QueryIntent.ASK)
            prompt = "\n".join(m.content for m in messages)
            assert OUTSIDE_CANARY not in prompt.replace(question, "")
            assert "/usr/sbin/nologin" not in prompt.replace(question, "")
    finally:
        workspace.close()


# --------------------------------------------------------------------------- #
# 13 (read boundary). No foreign file is ever opened at all
# --------------------------------------------------------------------------- #


@pytest.fixture
def recorded_opens(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every path RepoAtlas actually opens, resolved to its real target.

    The strongest available assertion: absence from the database proves the
    content was not *kept*, and this proves it was not *read*. A fix that opened
    ``/etc/passwd`` and then discarded it would still have opened it, which is a
    different guarantee from the one V1_SPEC §17 makes.
    """
    opened: list[str] = []
    real_io_open = io.open
    real_os_open = os.open

    def record(path: object, dir_fd: int | None = None) -> None:
        """Observe an open. Never raise, whatever it is handed.

        This wraps ``os.open`` for the entire process — SQLite, git, importlib
        and the code under test all pass through it — so an exception here would
        not merely lose a record, it would make the open *fail* and silently
        change the behaviour being measured. An instrument that can break the
        experiment is worse than no instrument: it reports success because the
        escape crashed rather than because it was refused.
        """
        try:
            name = os.fspath(path)  # type: ignore[arg-type]
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="surrogateescape")
            if dir_fd is not None:
                # A descriptor-relative open resolves against the *directory the
                # descriptor points at*, which is the whole point of the boundary
                # — and which a naive recorder cannot see. Reading the
                # descriptor's own target is what makes an escape observable.
                name = str(Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / name)  # noqa: PTH115
            opened.append(os.path.realpath(name))
        except Exception:  # noqa: S110 - see the docstring: this must not raise
            pass

    def watched_io_open(file: object, *args: object, **kwargs: object) -> object:
        record(file)
        return real_io_open(file, *args, **kwargs)  # type: ignore[arg-type]

    def watched_os_open(path: object, *args: object, **kwargs: object) -> int:
        record(path, kwargs.get("dir_fd"))  # type: ignore[arg-type]
        return real_os_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(io, "open", watched_io_open)
    monkeypatch.setattr(os, "open", watched_os_open)
    yield opened


def test_no_foreign_file_is_ever_opened(
    victim: Path,
    containment_settings: Settings,
    managed_root: Path,
    tmp_path: Path,
    recorded_opens: list[str],
) -> None:
    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        context = workspace.resolver().resolve(managed_root)
        sync(workspace, context, full=True)
    finally:
        workspace.close()

    outside_root = str(tmp_path / "outside")
    offenders = [
        path
        for path in recorded_opens
        if path == "/etc/passwd" or path.startswith(outside_root + os.sep)
    ]
    assert offenders == [], (
        f"RepoAtlas opened files outside the repository: {sorted(set(offenders))}"
    )


def test_dirty_state_hashing_never_opens_an_escaped_file(
    managed_root: Path, recorded_opens: list[str]
) -> None:
    """The git working-tree comparison hashes every tracked file, and must not
    hash these. "Is this tree dirty?" is not a reason to open ``/etc/passwd``.

    The fixture shape matters more than it looks. If the replacement symlink is
    merely untracked, ``ls-files --others`` reports it and the dirty computation
    returns ``True`` before it ever reaches the per-file hash — so a naive
    fixture passes this test without exercising the code it names. Ignoring the
    symlink makes the earlier stages find nothing, which forces the comparison
    all the way down to ``_blob_hash``, where the escape used to happen.
    """
    from repoatlas.gitstate.client import GitClient

    repo = managed_root / "r"
    make_repository(repo)
    (repo / "sub").mkdir()
    (repo / "sub" / "passwd").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (repo / "app.py").write_text("A = 1\n", encoding="utf-8")
    commit(repo, "tracked")
    (repo / ".gitignore").write_text("/sub\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "--quiet", "-m", "ignore")
    swap_for_symlink(repo / "sub", "/etc")

    # Preconditions: the earlier, cheaper stages must all find nothing, or this
    # test would pass without reaching the hash at all.
    for probe in (("--others", "--exclude-standard"), ("--deleted",)):
        result = GitClient()._run(repo, "ls-files", *probe, "-z")
        assert result.stdout.strip("\0").strip() == "", f"{probe} short-circuits the comparison"

    recorded_opens.clear()
    state = GitClient().state(repo)
    assert state.known
    offenders = [path for path in recorded_opens if path.startswith("/etc" + os.sep)]
    assert offenders == [], f"dirty-state hashing opened: {sorted(set(offenders))}"
    assert not state.is_dirty, (
        "an escaped path cannot change a stored fact, so it cannot make the tree dirty"
    )


# --------------------------------------------------------------------------- #
# 17 (nested). Repository boundaries are not crossed by canonicalization
# --------------------------------------------------------------------------- #


def test_a_nested_repository_keeps_its_own_files(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    outer = managed_root / "outer"
    make_repository(outer)
    (outer / "outer.py").write_text("A = 1\n", encoding="utf-8")
    inner = outer / "tools" / "inner"
    make_repository(inner)
    (inner / "inner.py").write_text("B = 1\n", encoding="utf-8")
    commit(inner)
    commit(outer)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        rows = workspace.database.execute(
            "SELECT r.slug AS slug, f.rel_path AS rel_path FROM files f "
            "JOIN repositories r ON r.id = f.repo_id"
        ).fetchall()
        owned = {(str(r["slug"]), str(r["rel_path"])) for r in rows}
        assert ("outer", "outer.py") in owned
        assert ("outer/tools/inner", "inner.py") in owned
        assert not any(slug == "outer" and "inner" in path for slug, path in owned)
    finally:
        workspace.close()


def test_a_symlink_from_a_parent_into_a_nested_repository_is_refused(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    outer = managed_root / "outer"
    make_repository(outer)
    inner = outer / "tools" / "inner"
    make_repository(inner)
    (inner / "secret_of_inner.py").write_text(f"B = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(inner)
    (outer / "peek").mkdir()
    (outer / "peek" / "secret_of_inner.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (outer / "outer.py").write_text("A = 1\n", encoding="utf-8")
    commit(outer)
    swap_for_symlink(outer / "peek", inner)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        rows = workspace.database.execute(
            "SELECT r.slug AS slug, f.rel_path AS rel_path FROM files f "
            "JOIN repositories r ON r.id = f.repo_id"
        ).fetchall()
        owned = {(str(r["slug"]), str(r["rel_path"])) for r in rows}
        assert ("outer", "peek/secret_of_inner.py") not in owned
        assert ("outer/tools/inner", "secret_of_inner.py") in owned
    finally:
        workspace.close()


# --------------------------------------------------------------------------- #
# 18. Incremental still equals a clean full index
# --------------------------------------------------------------------------- #


def test_incremental_equals_a_clean_index_across_a_containment_transition(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    from tests.conftest import fact_snapshot, reindex_from_scratch

    outside = build_outside(tmp_path)
    repo = managed_root / "r"
    make_repository(repo)
    (repo / "assets").mkdir()
    (repo / "assets" / "config.yaml").write_text("real: 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)

    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        swap_for_symlink(repo / "assets", outside)
        (repo / "added.py").write_text("N = 1\n", encoding="utf-8")
        context = workspace.resolver().resolve(managed_root)
        sync(workspace, context)
        incremental = fact_snapshot(workspace)
    finally:
        workspace.close()

    clean = reindex_from_scratch(containment_settings, managed_root, tmp_path, tag="clean")
    assert incremental == clean


# --------------------------------------------------------------------------- #
# 20. The check/use race
# --------------------------------------------------------------------------- #


def test_swapping_an_ancestor_between_check_and_read_cannot_leak(
    managed_root: Path, containment_settings: Settings, tmp_path: Path
) -> None:
    """A concurrent attacker flipping a directory to a symlink mid-sync.

    The boundary descends through directory file descriptors with ``O_NOFOLLOW``,
    so the object opened is the object that was checked — there is no window in
    which a swapped ancestor can redirect the read. This runs the swap in a tight
    loop against a repeated sync to make the attempt real rather than argued.
    """
    import threading

    outside = build_outside(tmp_path)
    repo = managed_root / "r"
    make_repository(repo)
    target = repo / "assets"
    target.mkdir()
    (target / "config.yaml").write_text("real: 1\n", encoding="utf-8")
    (repo / "keep.py").write_text(f"K = '{INSIDE_CANARY}'\n", encoding="utf-8")
    commit(repo)

    workspace = index(containment_settings, managed_root, tmp_path)
    stop = threading.Event()

    def flap() -> None:
        while not stop.is_set():
            try:
                if target.is_symlink():
                    target.unlink()
                    target.mkdir()
                    (target / "config.yaml").write_text("real: 1\n", encoding="utf-8")
                else:
                    for child in list(target.iterdir()):
                        child.unlink()
                    target.rmdir()
                    target.symlink_to(outside)
            except OSError:
                pass

    worker = threading.Thread(target=flap, daemon=True)
    worker.start()
    try:
        context = workspace.resolver().resolve(managed_root)
        for _ in range(25):
            sync(workspace, context, full=True)
            assert not contains_text(workspace, OUTSIDE_CANARY), (
                "a swapped ancestor leaked outside content into the index"
            )
    finally:
        stop.set()
        worker.join(timeout=5)
        workspace.close()


# --------------------------------------------------------------------------- #
# The boundary primitive, tested directly
# --------------------------------------------------------------------------- #


def test_the_boundary_refuses_traversal_and_absolute_paths(tmp_path: Path) -> None:
    """``..`` and an absolute path are refused even though no symlink is involved.

    Git never emits either, so this is defence against a future caller rather
    than against git — but a containment primitive that trusts its input is not
    a containment primitive.
    """
    from repoatlas.containment import RepositoryBoundary

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("B = 1\n", encoding="utf-8")

    boundary = RepositoryBoundary(root)
    assert boundary.read_bytes("src/ok.py") == b"A = 1\n"
    assert boundary.read_bytes("../outside.py") is None
    assert boundary.read_bytes("/etc/passwd") is None
    assert boundary.read_bytes("src/../../outside.py") is None
    assert boundary.read_bytes("") is None
    assert boundary.read_bytes("src/nope.py") is None


def test_the_boundary_refuses_a_non_regular_file(tmp_path: Path) -> None:
    """A FIFO must not be opened for reading: it would block the sync forever."""
    from repoatlas.containment import RepositoryBoundary

    root = tmp_path / "repo"
    root.mkdir()
    os.mkfifo(root / "pipe")
    (root / "ok.py").write_text("A = 1\n", encoding="utf-8")

    boundary = RepositoryBoundary(root)
    assert boundary.stat("pipe") is None
    assert boundary.read_bytes("pipe") is None
    assert boundary.stat("ok.py") is not None


def test_the_boundary_counts_what_it_refused(tmp_path: Path) -> None:
    """Refusals are counted, never named: an absolute host path a hostile
    repository chose is not something to print (V1_SPEC §17)."""
    from repoatlas.containment import RepositoryBoundary

    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.py").write_text("A = 1\n", encoding="utf-8")
    (root / "leak").symlink_to("/etc")

    boundary = RepositoryBoundary(root)
    assert boundary.refused == 0
    assert boundary.stat("ok.py") is not None
    assert boundary.refused == 0
    assert boundary.stat("leak/passwd") is None
    assert boundary.refused == 1


def test_the_descriptor_walk_is_the_active_implementation() -> None:
    """The race-free path must be the one actually running on this platform.

    The boundary carries a portable fallback for kernels without
    ``openat``/``O_NOFOLLOW``, and that fallback is correct but *not* race-free.
    Nothing else in this file can tell the two apart — both refuse every escape —
    so a regression that silently selected the weaker one would leave the whole
    suite green. This is the only assertion that notices.
    """
    from repoatlas import containment

    assert containment._supported(), (
        "the descriptor walk is unavailable, so containment is running on its "
        "racy fallback; every other test in this file passes either way"
    )


def test_capability_detection_survives_a_wrapped_os_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrapping ``os.open`` must not downgrade the boundary.

    ``os.supports_dir_fd`` holds the *original* ``os.open`` object, so the
    obvious membership test is an identity test — and any profiler, audit hook or
    test harness that decorates ``os.open`` silently switches this security
    control to its weaker implementation. Worst of all at exactly the moment
    someone is instrumenting file access to check it.
    """
    from repoatlas import containment

    real = os.open
    monkeypatch.setattr(os, "open", lambda *a, **k: real(*a, **k))
    assert containment._supported(), "a wrapped os.open downgraded the boundary"


def test_two_repositories_get_independent_boundaries(tmp_path: Path) -> None:
    from repoatlas.containment import RepositoryBoundary

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "a.py").write_text("A = 1\n", encoding="utf-8")
    (b / "b.py").write_text("B = 1\n", encoding="utf-8")

    assert RepositoryBoundary(a).read_bytes("a.py") == b"A = 1\n"
    assert RepositoryBoundary(a).read_bytes("../b/b.py") is None
    assert RepositoryBoundary(b).read_bytes("b.py") == b"B = 1\n"


def test_the_database_holds_no_foreign_row_after_indexing(
    victim: Path, containment_settings: Settings, managed_root: Path, tmp_path: Path
) -> None:
    """One sweep over every table that can carry text or a path."""
    workspace = index(containment_settings, managed_root, tmp_path)
    try:
        database: sqlite3.Connection = workspace.database.connection
        forbidden = ("sub/passwd", "st/config.yaml")
        for rel_path in forbidden:
            assert (
                database.execute(
                    "SELECT COUNT(*) FROM files WHERE rel_path = ?", (rel_path,)
                ).fetchone()[0]
                == 0
            )
        for needle in (OUTSIDE_CANARY, "/usr/sbin/nologin", "root:x:0:0"):
            assert (
                database.execute(
                    "SELECT COUNT(*) FROM chunks WHERE text LIKE ?", (f"%{needle}%",)
                ).fetchone()[0]
                == 0
            )
        assert (
            database.execute(
                "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'nologin'"
            ).fetchone()[0]
            == 0
        )
        assert (
            database.execute(
                "SELECT COUNT(*) FROM unresolved_refs WHERE target_key LIKE ?",
                (f"%{OUTSIDE_CANARY}%",),
            ).fetchone()[0]
            == 0
        )
    finally:
        workspace.close()
