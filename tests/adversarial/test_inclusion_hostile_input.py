"""Repository inclusions read files any repository can commit, so a hostile one must cost no
more than its own claim: a malformed URL, a YAML alias that loops or multiplies, and two claims
at one line for one path may not raise, hang or break the global pass for the whole workspace.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.topology import crossing_groups
from repoatlas.errors import ExitCode
from repoatlas.gitstate.config import normalize_git_url, strip_credentials
from repoatlas.gitstate.remotes import read_remotes
from repoatlas.graph.inclusions import decision_ref, plan_inclusions
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import ExportedLink, LinksExport
from repoatlas.models.inclusions import (
    INCLUSION_FAMILY,
    MAX_INCLUSION_DECLARATIONS,
    MAX_LINK_INCLUSIONS,
    InclusionDeclarationError,
    InclusionDeclarationFields,
)
from repoatlas.models.parsing import INVALID_YAML, EdgeType, ParsedReference
from repoatlas.models.repository import Repository
from repoatlas.models.service_declarations import ExternalServiceFields
from repoatlas.models.topology import LinkKind, TopologyOverviewRequest
from repoatlas.parsers import inclusions as inclusion_parsers
from repoatlas.parsers.inclusions import (
    MAX_ENTRIES_READ,
    MAX_FIELD_CHARS,
    MAX_URL_CHARS,
    MAX_YAML_NODE_VISITS,
    RepositoryInclusionParser,
    inclusion_reference,
    repo_manifest_references,
    vcstool_references,
    west_references,
)
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import CrossingEdgeRow, FileRow, UnresolvedRefRecord
from repoatlas.storage.sqlite.inclusion_declarations import SqliteInclusionDeclarationStore
from tests.conftest import git, init_git_repository

pytestmark = pytest.mark.adversarial

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: A scheme URL ``urlsplit`` refuses (``ValueError: Invalid IPv6 URL``).
BAD_URL = "https://[oops/x"


def _parse(path: str, content: str) -> list[ParsedReference]:
    return list(RepositoryInclusionParser().parse(Path(path), content).references)


def _west(text: str) -> list[ParsedReference]:
    return west_references(list(yaml.compose_all(text, Loader=yaml.SafeLoader)))


def _within(seconds: float, call: Callable[[], Any]) -> Any:
    """``call()``'s result, failing if it has not returned in ``seconds``. The call runs in a
    daemon thread, so one that never returns fails the test instead of hanging the run."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # re-raised below, on the test's thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "did not return: unbounded walk"
    if "error" in box:
        raise box["error"]
    return box["value"]


# -- H1: a URL that does not parse ----------------------------------------------------------


def test_a_url_that_does_not_parse_is_no_identity_and_no_credential_leaks() -> None:
    assert normalize_git_url(BAD_URL) is None
    assert normalize_git_url("ssh://git@[oops/x", base="github.com/o") is None
    assert strip_credentials(BAD_URL) is None
    # Not told free of credentials, so none of it comes back.
    assert strip_credentials("https://user:s3cret@[oops/x") is None
    assert strip_credentials("https://user:s3cret@github.com/o/r") == "https://github.com/o/r"


@pytest.mark.parametrize(
    ("url", "stripped"),
    [
        ("https://h/o/r?private_token=s3cret#frag", "https://h/o/r"),
        ("https://h/o/r?private_token=s3cret", "https://h/o/r"),
        ("https://h/o/r#s3cret", "https://h/o/r"),
        ("https://u:s3cret@h:8443/o/r?token=s3cret#y", "https://h:8443/o/r"),
        ("u:s3cret@host:o/r", "host:o/r"),
        ("git@github.com:o/r", "git@github.com:o/r"),
        ("host:o/r", "host:o/r"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("../lib.git", "../lib.git"),
    ],
)
def test_a_token_in_a_query_or_an_scp_password_never_leaves(url: str, stripped: str) -> None:
    assert strip_credentials(url) == stripped
    assert "s3cret" not in (strip_credentials(url) or "")


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        # The user information ends at the `#`: the parser sees a host of `u:p` and a fragment.
        ("https://u:p#ss@h/r", "ss"),
        # No user, and a "port" that is no number: `tok:secret` is a user and a password.
        ("https://tok:secret/o/r", "secret"),
        # A user, and a port that is no number.
        ("https://u:p@h:abc/r", "p"),
        # No scheme, and no scp-like `host:` before the `@`: `user:pass` is a user and a password.
        ("user:pass@host/o/r", "pass"),
        # An `@` after the host, where the user information may have been cut short.
        ("https://h/r?x@y", "x@y"),
    ],
)
def test_a_url_that_cannot_be_told_free_of_credentials_is_dropped_whole(
    url: str, secret: str
) -> None:
    stripped = strip_credentials(url)
    assert stripped is None
    assert secret not in (stripped or "")


@pytest.mark.parametrize(
    ("url", "stripped"),
    [
        ("git@github.com:o/r", "git@github.com:o/r"),
        ("https://h/o/r?private_token=x#y", "https://h/o/r"),
        ("u:s3cret@host:o/r", "host:o/r"),
        ("https://user:s3cret@github.com/o/r", "https://github.com/o/r"),
    ],
)
def test_a_url_that_can_be_stripped_still_is(url: str, stripped: str) -> None:
    assert strip_credentials(url) == stripped


def test_a_gitmodules_url_with_a_token_in_its_query_stores_none_of_it() -> None:
    [reference] = _parse(
        ".gitmodules", '[submodule "a"]\n\tpath = a\n\turl = https://h/o/a?private_token=s3cret\n'
    )
    assert reference.meta["url"] == "https://h/o/a"
    assert "s3cret" not in json.dumps(reference.meta)


def test_a_vcstool_file_nested_past_the_recursion_limit_is_invalid_yaml_not_a_crash() -> None:
    depth = 10_000
    content = "[" * depth + "]" * depth + "\n"

    references, diagnostics = vcstool_references(content)

    assert references == []
    assert [d.message for d in diagnostics] == [INVALID_YAML]
    # Through the parser, as a sync reaches it.
    parsed = RepositoryInclusionParser().parse(Path("deep.repos"), content)
    assert parsed.references == ()
    assert [d.message for d in parsed.diagnostics] == [INVALID_YAML]


def test_a_vcstool_file_of_deeply_nested_mappings_is_invalid_yaml_not_a_crash() -> None:
    depth = 10_000
    content = "repositories: " + "{a: " * depth + "x" + "}" * depth + "\n"

    references, diagnostics = vcstool_references(content)

    assert references == []
    assert [d.message for d in diagnostics] == [INVALID_YAML]


def test_gitmodules_with_a_bad_url_keeps_the_claim_without_the_url() -> None:
    [reference] = _parse(
        ".gitmodules",
        f'[submodule "a"]\n\tpath = a\n\turl = {BAD_URL}\n'
        '[submodule "b"]\n\tpath = b\n\turl = https://u:p@github.com/o/b\n',
    )[:1]
    assert reference.target_key == "a"
    assert "url" not in reference.meta
    assert reference.meta["format"] == "gitmodules"


def test_a_later_good_entry_survives_an_earlier_bad_url() -> None:
    references = _parse(
        ".gitmodules",
        f'[submodule "a"]\n\tpath = a\n\turl = {BAD_URL}\n'
        '[submodule "b"]\n\tpath = b\n\turl = https://u:p@github.com/o/b\n',
    )
    assert [(r.target_key, r.meta.get("url")) for r in references] == [
        ("a", None),
        ("b", "https://github.com/o/b"),
    ]


def test_gitrepo_with_a_bad_remote_does_not_raise() -> None:
    [reference] = _parse("vendor/sub/.gitrepo", f"[subrepo]\n\tremote = {BAD_URL}\n")
    assert reference.target_key == "vendor/sub"
    assert "url" not in reference.meta


def test_vcstool_with_a_bad_url_does_not_raise() -> None:
    [reference] = _parse("d.repos", f"repositories:\n  a:\n    type: git\n    url: '{BAD_URL}'\n")
    assert reference.target_key == "a"
    assert "url" not in reference.meta


def test_west_with_a_bad_url_does_not_raise() -> None:
    [reference] = _west('manifest: {projects: [{name: a, url: "https://[x"}]}\n')
    assert reference.target_key == "a"
    assert "url" not in reference.meta


def test_west_with_a_bad_remote_base_does_not_raise() -> None:
    [reference] = _west(
        "manifest:\n  remotes:\n    - name: r\n      url-base: 'https://[x'\n"
        "  projects:\n    - name: a\n      remote: r\n"
    )
    assert reference.target_key == "a"
    assert "url" not in reference.meta


def test_repo_manifest_with_a_bad_fetch_does_not_raise() -> None:
    [reference] = _parse(
        "default.xml",
        '<manifest><remote name="o" fetch="https://[bad"/><default remote="o"/>'
        '<project name="a"/></manifest>',
    )
    assert reference.target_key == "a"
    assert "url" not in reference.meta


def test_a_remote_with_a_bad_url_is_skipped_by_read_remotes(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = {BAD_URL}\n'
        '[remote "backup"]\n\turl = https://example.com/o/r.git\n'
    )
    assert read_remotes(tmp_path) == ("example.com/o/r",)


# -- H2: a YAML alias that loops or multiplies ----------------------------------------------


def test_a_self_referencing_anchor_returns_and_still_yields_the_reference() -> None:
    content = "repositories:\n  a: &p\n    type: git\n    url: u\n    self: *p\n"
    references, diagnostics = _within(10, lambda: vcstool_references(content))
    assert diagnostics == []
    [reference] = references
    assert reference.target_key == "a"
    assert reference.meta["url"] == "u"
    assert reference.line_start == 2
    assert reference.line_end == 5


def test_nested_aliases_do_not_multiply_the_walk() -> None:
    """Nine aliases a level, nine levels: 9**9 visits to a walk that follows every one."""
    levels = ["l0: &l0 [x, y, z]"]
    for n in range(1, 10):
        levels.append(f"l{n}: &l{n} [{', '.join([f'*l{n - 1}'] * 9)}]")
    content = "\n".join(levels) + "\nrepositories:\n  a:\n    type: git\n    url: u\n    big: *l9\n"
    references, _ = _within(10, lambda: vcstool_references(content))
    [reference] = references
    assert reference.target_key == "a"
    assert reference.meta["url"] == "u"


def test_nested_aliases_in_a_west_project_return() -> None:
    levels = ["l0: &l0 [x, y, z]"]
    for n in range(1, 10):
        levels.append(f"l{n}: &l{n} [{', '.join([f'*l{n - 1}'] * 9)}]")
    text = "\n".join(levels) + "\nmanifest:\n  projects:\n    - name: a\n      big: *l9\n"
    [reference] = _within(10, lambda: _west(text))
    assert reference.target_key == "a"


def test_the_walk_is_bounded_even_for_distinct_nodes() -> None:
    items = ", ".join(f"[{n}]" for n in range(MAX_YAML_NODE_VISITS + 50))
    content = f"repositories:\n  a:\n    type: git\n    url: u\n    many: [{items}]\n"
    [reference] = _within(10, lambda: vcstool_references(content))[0]
    assert reference.target_key == "a"


def test_an_ordinary_entry_still_ends_at_its_last_scalar() -> None:
    [first, second] = _parse(
        "d.repos",
        "repositories:\n  a:\n    type: git\n    url: u\n    version: v\n  b:\n    type: git\n"
        "    url: w\n",
    )
    assert (first.line_start, first.line_end) == (2, 5)
    assert (second.line_start, second.line_end) == (6, 8)


def test_entries_sharing_one_large_anchor_do_not_each_walk_it() -> None:
    """One anchor of 50,000 aliases, shared by 2,000 entries whose paths are invalid (dropped,
    but their end line is still computed) and 2,000 valid ones: each entry's walk must stay
    inside its own text, or the shared anchor is walked 4,000 times."""
    lines = ["t: &t x", f"s: &s [{', '.join(['*t'] * 50_000)}]", "repositories:"]
    lines += [f"  /p{n}: {{v: *s}}" for n in range(2_000)]
    lines += [f"  p{n}: {{v: *s}}" for n in range(2_000)]
    content = "\n".join(lines) + "\n"
    references, diagnostics = _within(10, lambda: vcstool_references(content))
    assert diagnostics == []
    assert [r.target_key for r in references] == [f"p{n}" for n in range(2_000)]
    # Each entry is one line, so it starts and ends there, whatever the alias points at.
    assert all(r.line_end == r.line_start for r in references)


def test_many_entries_aliasing_one_anchor_past_the_cap_do_not_each_walk_it() -> None:
    """The anchor holds more nodes than one walk may visit, so no entry's walk finishes it:
    each of 22,000 entries must still skip what an earlier entry already walked, or the work
    is the cap times the entries."""
    items = ", ".join(f"[{n}]" for n in range(MAX_YAML_NODE_VISITS + 50))
    lines = [f"s: &s {{type: git, url: u, big: [{items}]}}", "repositories:"]
    lines += [f"  /p{n}: *s" for n in range(20_000)]
    lines += [f"  p{n}: *s" for n in range(2_000)]
    content = "\n".join(lines) + "\n"
    references, diagnostics = _within(10, lambda: vcstool_references(content))
    assert diagnostics == []
    assert [r.target_key for r in references] == [f"p{n}" for n in range(2_000)]
    assert all(r.meta["url"] == "u" for r in references)
    assert all(r.line_end == r.line_start for r in references)


def test_many_west_projects_aliasing_one_anchor_past_the_cap_return() -> None:
    items = ", ".join(f"[{n}]" for n in range(MAX_YAML_NODE_VISITS + 50))
    text = f"s: &s {{name: a, path: /x, big: [{items}]}}\nmanifest:\n  projects: ["
    text += ", ".join(["*s"] * 20_000) + "]\n"
    assert _within(10, lambda: _west(text)) == []


def test_an_alias_to_a_large_earlier_anchor_does_not_truncate_the_entrys_end_line() -> None:
    items = ", ".join(f"[{n}]" for n in range(MAX_YAML_NODE_VISITS + 50))
    content = f"big: &big [{items}]\nrepositories:\n  a:\n    type: git\n    url: u\n    v: *big\n"
    [reference] = _within(10, lambda: vcstool_references(content))[0]
    assert (reference.line_start, reference.line_end) == (3, 6)


def test_an_anchor_and_its_alias_inside_one_entry_are_both_read() -> None:
    [reference] = _within(
        10,
        lambda: vcstool_references(
            "repositories:\n  a:\n    type: git\n    url: &u x\n    mirror: *u\n"
        )[0],
    )
    assert (reference.line_start, reference.line_end) == (2, 5)


# -- H3: two claims at one line for one path ------------------------------------------------


def _records(
    references: list[ParsedReference], file_id: int, repo_id: int, src_id: int
) -> list[UnresolvedRefRecord]:
    return [
        UnresolvedRefRecord(
            id=index + 1,
            repo_id=repo_id,
            file_id=file_id,
            src_id=src_id,
            edge_type=reference.edge_type,
            target_kind=reference.target_kind,
            target_key=reference.target_key,
            line_start=reference.line_start,
            line_end=reference.line_end,
            meta_json=json.dumps(reference.meta),
        )
        for index, reference in enumerate(references)
    ]


def _anchor(workspace: Workspace) -> tuple[int, int, int]:
    row = workspace.database.execute(
        "SELECT f.repo_id, f.id, e.id FROM files f JOIN entities e ON e.file_id = f.id "
        "WHERE f.rel_path = 'src/config.py' AND e.type = 'file'"
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


SAME_LINE = {
    "vcstool": (
        "d.repos",
        "repositories: {a: {type: git, url: x}, ./a: {type: git, url: y}}\n",
    ),
    "repo-manifest": (
        "default.xml",
        '<manifest><project name="a"/><project name="a"/></manifest>',
    ),
}


@pytest.mark.parametrize("name", [*SAME_LINE, "west"])
def test_two_claims_at_one_line_for_one_path_are_one_decision_and_write_cleanly(
    initialized: Workspace, name: str
) -> None:
    if name == "west":
        references = _west("manifest: {projects: [{name: a}, {name: a}]}\n")
    else:
        references = _parse(*SAME_LINE[name])
    # The input is adversarial only if the parser really states the claim twice.
    assert [(r.target_key, r.line_start) for r in references] == [("a", 1), ("a", 1)]

    repo_id, file_id, src_id = _anchor(initialized)
    repository = Repository(id=repo_id, name="py", slug="py", path=Path("/nowhere/ws/py"))
    origin = FileRow(
        file_id=file_id,
        file_entity_id=src_id,
        repo_id=repo_id,
        repo_name="py",
        repo_slug="py",
        repo_path=repository.path,
        rel_path="src/config.py",
    )
    plan = plan_inclusions(
        _records(references, file_id, repo_id, src_id), {file_id: origin}, [repository]
    )

    [decision] = plan.decisions
    # The surviving ref is the one the pair always had: nothing is added to the hash.
    assert decision.decision_ref == decision_ref("py", "src/config.py", "a", 1)
    assert plan.unsettled == 1

    # A plain INSERT on a PRIMARY KEY: a duplicate ref raises inside the global swap.
    initialized.knowledge.replace_link_decisions(plan.decisions)
    assert [
        r.subject for r in initialized.knowledge.list_link_decisions(family=INCLUSION_FAMILY)
    ] == ["a"]


def test_claims_on_different_lines_or_paths_are_each_decided(initialized: Workspace) -> None:
    references = _parse(
        "d.repos",
        "repositories:\n  a:\n    type: git\n    url: x\n  b:\n    type: git\n    url: y\n"
        "  c: {type: git, url: z}\n",
    )
    assert len(references) == 3
    repo_id, file_id, src_id = _anchor(initialized)
    repository = Repository(id=repo_id, name="py", slug="py", path=Path("/nowhere/ws/py"))
    origin = FileRow(
        file_id=file_id,
        file_entity_id=src_id,
        repo_id=repo_id,
        repo_name="py",
        repo_slug="py",
        repo_path=repository.path,
        rel_path="src/config.py",
    )
    plan = plan_inclusions(
        _records(references, file_id, repo_id, src_id), {file_id: origin}, [repository]
    )
    assert sorted(d.subject for d in plan.decisions) == ["a", "b", "c"]
    assert len({d.decision_ref for d in plan.decisions}) == 3


# -- shared by M1 and M3 -----------------------------------------------------------------------


def _make_repo(path: Path, files: dict[str, str], remote: str | None = None) -> None:
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    init_git_repository(path)
    if remote is not None:
        git(path, "remote", "add", "origin", remote)


def _submodule_links(workspace: Workspace) -> dict[str, Any]:
    overview = CapabilityService(workspace).topology_overview(
        TopologyOverviewRequest(context=GLOBAL)
    )
    return {
        f"{link.source}->{link.target}": link
        for link in overview.links
        if link.kind is LinkKind.SUBMODULE
    }


# -- M3: a west project named like a config key is a claim, never a local edge ---------------


def test_a_west_project_named_like_a_config_key_is_decided_not_linked_to_the_config(
    workspace: Workspace, tmp_path: Path
) -> None:
    """``manifest`` and ``projects`` are also symbols of the west file itself (its YAML keys). A
    project's key is a path, so it must reach the global pass as a claim: resolved by name in
    the file it would be an edge to a config symbol, and no inclusion would be decided."""
    root = tmp_path / "repos"
    _make_repo(
        root / "app",
        {
            "west.yml": "manifest:\n  projects:\n"
            "    - name: manifest\n      url: https://github.com/org/manifest\n"
            "    - name: projects\n      url: https://github.com/org/projects\n"
        },
        "https://github.com/org/app.git",
    )
    _make_repo(root / "manifest", {"m.py": "M = 1\n"}, "https://github.com/org/manifest.git")
    _make_repo(root / "projects", {"p.py": "P = 1\n"}, "https://github.com/org/projects.git")
    initialize(workspace, root, config_path=tmp_path / "config.yaml")

    links = _submodule_links(workspace)
    assert [i.path for i in links["app->manifest"].inclusions] == ["manifest"]
    assert [i.path for i in links["app->projects"].inclusions] == ["projects"]
    (included,) = workspace.database.execute(
        "SELECT COUNT(*) FROM edges WHERE type = 'INCLUDES_REPOSITORY'"
    ).fetchone()
    assert included == 2
    (misplaced,) = workspace.database.execute(
        "SELECT COUNT(*) FROM edges e JOIN entities d ON d.id = e.dst_id "
        "WHERE e.type = 'INCLUDES_REPOSITORY' AND d.type <> 'repository'"
    ).fetchone()
    assert misplaced == 0
    subjects = {d.subject for d in workspace.knowledge.list_link_decisions(family=INCLUSION_FAMILY)}
    assert {"manifest", "projects"} <= subjects


# -- M2: `links import` judges everything before it stores anything -----------------------------

ORIGIN = "https://api.example.com"


def _declare(
    repository: str, path: str, target: str, file: str = ".gitmodules"
) -> InclusionDeclarationFields:
    return InclusionDeclarationFields(repository=repository, file=file, path=path, target=target)


def _links_file(
    tmp_path: Path,
    inclusions: list[InclusionDeclarationFields],
    services: list[ExternalServiceFields] | None = None,
    links: list[ExportedLink] | None = None,
) -> Path:
    exported = LinksExport(
        links=links or [],
        service_declarations=services or None,
        inclusion_declarations=inclusions or None,
    )
    file = tmp_path / "import.json"
    file.write_text(exported.model_dump_json(exclude_none=True), encoding="utf-8")
    return file


def _actions(workspace: Workspace) -> list[str]:
    return [
        row[0]
        for row in workspace.database.execute(
            "SELECT action FROM inclusion_declaration_audit ORDER BY id"
        ).fetchall()
    ]


def _stored(workspace: Workspace) -> list[tuple[str, str, str, str]]:
    return [
        (d.repository, d.file, d.path, d.target)
        for d in workspace.inclusion_declarations.list_all()
    ]


@pytest.fixture
def links_import(
    initialized: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[Path], Any]:
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    runner = CliRunner()
    return lambda file: runner.invoke(app, ["links", "import", str(file)])


def test_a_service_conflict_refuses_the_import_before_any_inclusion_is_stored(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    initialized.service_declarations.create(
        ExternalServiceFields(origin=ORIGIN, label="A"), via="import"
    )
    file = _links_file(
        tmp_path,
        [_declare("app", "ext/x", "lib")],
        [ExternalServiceFields(origin=ORIGIN, label="B")],
        [ExportedLink(repository="app", label="prod")],
    )

    result = links_import(file)

    assert result.exit_code == ExitCode.RUNTIME, result.output
    assert _stored(initialized) == []
    assert _actions(initialized) == []
    # The deployment links stated beside them are stored last, so none of them is stored either.
    assert initialized.deployment_links.list_all() == []


def test_more_declarations_than_are_kept_refuse_the_import_cleanly(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    file = _links_file(
        tmp_path,
        [_declare("app", f"ext/p{n}", "lib") for n in range(MAX_INCLUSION_DECLARATIONS + 1)],
    )

    result = links_import(file)

    assert result.exit_code == 1
    assert not isinstance(result.exception, InclusionDeclarationError)
    assert f"at most {MAX_INCLUSION_DECLARATIONS}" in result.output
    assert _stored(initialized) == []


def test_the_store_keeps_all_of_an_import_or_none_of_it(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = initialized.inclusion_declarations
    too_many = [_declare("app", f"ext/p{n}", "lib") for n in range(MAX_INCLUSION_DECLARATIONS + 1)]
    with pytest.raises(InclusionDeclarationError, match="at most"):
        store.import_all(too_many)
    assert store.list_all() == []

    # A write that fails after the first declaration is stored takes that one with it.
    real = SqliteInclusionDeclarationStore._audit
    calls: list[int] = []

    def fails_on_the_second(*args: Any) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        real(*args)

    monkeypatch.setattr(
        SqliteInclusionDeclarationStore, "_audit", staticmethod(fails_on_the_second)
    )
    with pytest.raises(RuntimeError, match="disk full"):
        store.import_all([_declare("app", f"ext/p{n}", "lib") for n in range(3)])
    assert store.list_all() == []
    assert _actions(initialized) == []


def test_an_import_never_replaces_a_declaration(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    initialized.inclusion_declarations.create(_declare("app", "ext/dup", "dup"))
    file = _links_file(
        tmp_path,
        [_declare("app", "ext/x", "lib"), _declare("app", "ext/dup", "forks/dup")],
        [ExternalServiceFields(origin=ORIGIN, label="A")],
        [ExportedLink(repository="app", label="prod")],
    )

    result = links_import(file)

    assert result.exit_code == 1, result.output
    assert "Not imported" in result.output
    assert _stored(initialized) == [("app", ".gitmodules", "ext/dup", "dup")]
    assert "update" not in _actions(initialized)
    # The conflict refuses the whole file: the service stated beside it is not stored either.
    assert initialized.service_declarations.list_all() == []
    assert initialized.deployment_links.list_all() == []

    # The store refuses it as well, whatever the caller checked first.
    with pytest.raises(InclusionDeclarationError, match=r"app/\.gitmodules:ext/dup"):
        initialized.inclusion_declarations.import_all([_declare("app", "ext/dup", "forks/dup")])
    assert _stored(initialized) == [("app", ".gitmodules", "ext/dup", "dup")]
    assert "update" not in _actions(initialized)


def test_different_entries_of_one_repository_coexist(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    initialized.inclusion_declarations.create(_declare("app", "ext/a", "lib"))
    file = _links_file(
        tmp_path,
        [_declare("app", "ext/b", "dup"), _declare("app", "ext/a", "lib", file="other.gitmodules")],
    )

    result = links_import(file)

    assert result.exit_code == 0, result.output
    assert sorted(_stored(initialized)) == [
        ("app", ".gitmodules", "ext/a", "lib"),
        ("app", ".gitmodules", "ext/b", "dup"),
        ("app", "other.gitmodules", "ext/a", "lib"),
    ]


def test_one_entry_stated_twice_with_two_targets_refuses_the_whole_file(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    file = _links_file(
        tmp_path,
        [
            _declare("app", "ext/other", "lib"),
            _declare("app", "ext/x", "lib"),
            _declare("app", "ext/x", "dup"),
        ],
    )

    result = links_import(file)

    assert result.exit_code == 1
    assert not isinstance(result.exception, InclusionDeclarationError)
    assert "Not imported" in result.output
    assert _stored(initialized) == []


def test_a_repository_cannot_be_declared_to_include_itself(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    result = links_import(_links_file(tmp_path, [_declare("app", "ext/x", "app")]))

    assert result.exit_code == 1
    assert not isinstance(result.exception, InclusionDeclarationError)
    assert "cannot include itself" in result.output
    assert _stored(initialized) == []


def test_importing_the_same_file_again_adds_nothing(
    initialized: Workspace, tmp_path: Path, links_import: Callable[[Path], Any]
) -> None:
    file = _links_file(tmp_path, [_declare("app", "ext/x", "lib")])

    first = links_import(file)
    second = links_import(file)

    assert first.exit_code == 0 and second.exit_code == 0, second.output
    assert "Added 1 inclusion declaration(s); 0 already recorded." in first.output
    assert "Added 0 inclusion declaration(s); 1 already recorded." in second.output
    assert _stored(initialized) == [("app", ".gitmodules", "ext/x", "lib")]
    assert _actions(initialized) == ["import"]


# -- M1: an overview lists a bounded number of inclusions per submodule link ----------------


def test_a_submodule_link_keeps_only_the_first_inclusions_by_where_they_are_stated() -> None:
    total = 40_000
    rows = [
        CrossingEdgeRow(
            edge_id=k + 1,
            type=str(EdgeType.INCLUDES_REPOSITORY),
            src_repo_id=1,
            dst_repo_id=2,
            src_key="a",
            dst_key="b",
            path=f"m{k // 2000}.repos",
            line_start=k % 2000 + 1,
        )
        for k in range(total)
    ]

    found = crossing_groups(rows, {1: "a", 2: "b"})

    assert len(found.inclusion_ids()) == MAX_LINK_INCLUSIONS
    assert found.groups[(LinkKind.SUBMODULE, "a", "b")].count == total
    # The head of the whole list by file, then numeric line, whatever order rows came in.
    expected = sorted((f"m{k // 2000}.repos", k % 2000 + 1, k + 1) for k in range(total))
    assert found.inclusions[("a", "b")] == expected[:MAX_LINK_INCLUSIONS]


def test_an_overview_lists_the_first_inclusions_of_a_link_and_counts_them_all(
    workspace: Workspace, tmp_path: Path
) -> None:
    n = MAX_LINK_INCLUSIONS + 5
    root = tmp_path / "repos"
    _make_repo(
        root / "app",
        {
            ".gitmodules": "".join(
                f'[submodule "l{i}"]\n\tpath = ext/l{i}\n\turl = https://github.com/org/lib\n'
                for i in range(n)
            )
        },
        "https://github.com/org/app.git",
    )
    _make_repo(root / "lib", {"lib.py": "L = 1\n"}, "git@github.com:org/lib.git")
    initialize(workspace, root, config_path=tmp_path / "config.yaml")

    link = _submodule_links(workspace)["app->lib"]

    assert link.count == n
    assert link.inclusions_total == n
    # By numeric line: ":10" is listed after ":7", not before ":4".
    assert [(i.declared_at, i.path) for i in link.inclusions] == [
        (f"app/.gitmodules:{1 + 3 * i}", f"ext/l{i}") for i in range(MAX_LINK_INCLUSIONS)
    ]
    assert all(i.method == "remote" for i in link.inclusions)


# -- bounded reads: an aliased mapping is read once, a long field is no claim, a list is capped --


def _count_scalar_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One item per real read of a mapping's pairs (``_read_scalars``), the original still run."""
    calls: list[int] = []
    real = inclusion_parsers._read_scalars

    def counted(node: yaml.MappingNode) -> tuple[dict[str, str], frozenset[str]]:
        calls.append(1)
        return real(node)

    monkeypatch.setattr(inclusion_parsers, "_read_scalars", counted)
    return calls


def _distinct_mappings(text: str) -> int:
    """How many different mapping nodes the document holds: an alias is the node it names."""
    seen: set[int] = set()
    pending: list[yaml.Node] = [d for d in yaml.compose_all(text, Loader=yaml.SafeLoader) if d]
    count = 0
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            count += 1
            for k, v in node.value:
                pending.extend((k, v))
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)
    return count


def _wide_anchor(name: str, head: str, pairs: int = 3_000) -> str:
    """``name: &s {head, k0: v, k1: v, ...}``: one anchored mapping of ``pairs`` filler pairs."""
    body = ", ".join([head, *(f"k{n}: v" for n in range(pairs))])
    return f"{name}: &s {{{body}}}"


def test_a_mapping_many_entries_alias_is_read_once_by_vcstool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [_wide_anchor("s", "type: git"), "repositories:"]
    lines += [f"  /p{n}: *s" for n in range(3_000)]
    lines.append("  ok: {type: git, url: u}")
    content = "\n".join(lines) + "\n"
    reads = _count_scalar_reads(monkeypatch)

    references, _ = _within(30, lambda: vcstool_references(content))

    assert [r.target_key for r in references] == ["ok"]
    # The root, the anchor, `repositories` and the valid entry at most; never one per alias.
    assert len(reads) <= _distinct_mappings(content) + 2


def test_a_mapping_many_west_projects_alias_is_read_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [_wide_anchor("s", "name: a, path: /x"), "manifest:", "  projects:"]
    lines += ["    - *s"] * 3_000
    lines.append("    - {name: ok}")
    text = "\n".join(lines) + "\n"
    reads = _count_scalar_reads(monkeypatch)

    references = _within(30, lambda: _west(text))

    assert [r.target_key for r in references] == ["ok"]
    assert len(reads) <= _distinct_mappings(text) + 2


def test_a_mapping_many_west_remotes_alias_is_read_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [_wide_anchor("s", "name: r, url-base: https://x.example"), "manifest:", "  remotes:"]
    lines += ["    - *s"] * 3_000
    lines += ["  projects:", "    - {name: ok, remote: r}"]
    text = "\n".join(lines) + "\n"
    reads = _count_scalar_reads(monkeypatch)

    references = _within(30, lambda: _west(text))

    assert [(r.target_key, r.meta.get("url")) for r in references] == [
        ("ok", "https://x.example/ok")
    ]
    assert len(reads) <= _distinct_mappings(text) + 2


def test_a_vcstool_key_longer_than_a_field_is_skipped_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = _count_scalar_reads(monkeypatch)
    # A plain key past 1,024 characters is not YAML at all; only the explicit `? key` form
    # reaches the parser with a key this long.
    content = f"repositories:\n  ? {'a' * (MAX_FIELD_CHARS + 1)}\n  : {{type: git, url: u}}\n"

    references, diagnostics = vcstool_references(content)

    assert (references, diagnostics) == ([], [])
    assert reads == []


#: One character past what a field may hold.
LONG = "x" * (MAX_FIELD_CHARS + 1)
#: Shared by the ``inclusion_reference`` calls below.
COMMON: dict[str, Any] = {"format": "west", "line_start": 1, "line_end": 1, "base": "workspace"}


def test_a_long_value_is_kept_out_of_the_fields_and_its_key_is_recorded() -> None:
    node = yaml.compose(
        f"{{a: ok, b: {LONG}, c: '', d: {'y' * MAX_FIELD_CHARS}}}",
        Loader=yaml.SafeLoader,
    )
    assert isinstance(node, yaml.MappingNode)
    # `b` was stated, over-long; `c` was stated empty, which is no field at all.
    assert inclusion_parsers._read_scalars(node) == (
        {"a": "ok", "d": "y" * MAX_FIELD_CHARS},
        frozenset({"b"}),
    )


@pytest.mark.parametrize("field", ["name", "revision", "branch"])
def test_a_claim_with_a_field_past_the_bound_is_no_claim(field: str) -> None:
    assert inclusion_reference(path="a", url=None, **{field: LONG}, **COMMON) is None


def test_a_claim_whose_url_is_past_the_url_bound_is_no_claim() -> None:
    prefix = "https://h/"
    over = inclusion_reference(path="a", url=prefix + "x" * (MAX_URL_CHARS), **COMMON)
    assert over is None


def test_fields_exactly_at_their_bounds_are_kept() -> None:
    exact = "x" * MAX_FIELD_CHARS
    prefix = "https://h/"
    exact_url = prefix + "x" * (MAX_URL_CHARS - len(prefix))
    assert len(exact_url) == MAX_URL_CHARS

    reference = inclusion_reference(
        path=exact, url=exact_url, name=exact, revision=exact, branch=exact, **COMMON
    )

    assert reference is not None
    assert reference.target_key == exact
    assert reference.meta["url"] == exact_url
    assert (reference.meta["name"], reference.meta["revision"], reference.meta["branch"]) == (
        exact,
        exact,
        exact,
    )
    # A path past the bound is no claim at all.
    assert inclusion_reference(path=LONG, url="https://h/r", **COMMON) is None


def test_a_url_built_from_two_fields_at_their_bound_is_still_a_url() -> None:
    """A remote's base and a project's ``repo-path`` may each be as long as a field, so the URL
    joined from them is longer than one: the URL bound has room for it."""
    base = "https://h/" + "b" * (MAX_FIELD_CHARS - len("https://h/"))
    repo_path = "p" * MAX_FIELD_CHARS
    [reference] = _west(
        f"manifest:\n  remotes:\n    - {{name: r, url-base: '{base}'}}\n  projects:\n"
        f"    - {{name: a, remote: r, repo-path: {repo_path}}}\n"
    )
    assert reference.meta["url"] == f"{base}/{repo_path}"
    assert len(reference.meta["url"]) <= MAX_URL_CHARS


def test_a_url_past_the_url_bound_gives_no_claim_in_gitmodules() -> None:
    """A dropped URL would have the linker match the submodule by path instead."""
    long = "https://example.com/" + "x" * MAX_URL_CHARS
    assert _parse(".gitmodules", f'[submodule "a"]\n\tpath = a\n\turl = {long}\n') == []
    assert _parse("vendor/sub/.gitrepo", f"[subrepo]\n\tremote = {long}\n") == []


def test_a_branch_past_the_bound_gives_no_claim_in_gitmodules() -> None:
    text = f'[submodule "a"]\n\tpath = a\n\turl = u\n\tbranch = {LONG}\n'
    assert _parse(".gitmodules", text) == []


def test_a_five_thousand_character_url_gives_no_vcstool_or_west_claim() -> None:
    long = "https://example.com/" + "x" * 5_000
    assert len(long) > MAX_FIELD_CHARS
    assert _parse("d.repos", f"repositories:\n  a:\n    type: git\n    url: {long}\n") == []
    assert _west(f"manifest:\n  projects:\n    - name: a\n      url: {long}\n") == []


# -- a field an entry reads, over-long: no claim (never a default, never another value) -------


def _vcstool_entry(**fields: str) -> str:
    pairs = ", ".join(f"{key}: {value}" for key, value in fields.items())
    return f"repositories:\n  a: {{{pairs}}}\n  ok: {{type: git, url: u}}\n"


@pytest.mark.parametrize(
    "fields",
    [
        {"type": LONG, "url": "u"},
        {"type": "git", "url": LONG},
        {"type": "git", "url": "u", "version": LONG},
    ],
    ids=["type", "url", "version"],
)
def test_a_vcstool_entry_with_an_over_long_field_it_reads_is_skipped_whole(
    fields: dict[str, str],
) -> None:
    # An over-long `type` would otherwise read as the default `git`.
    references = _parse("d.repos", _vcstool_entry(**fields))

    assert [r.target_key for r in references] == ["ok"]


def test_an_over_long_vcstool_type_gives_no_claim() -> None:
    assert _parse("d.repos", f"repositories:\n  a:\n    type: {LONG}\n    url: u\n") == []


def test_an_over_long_vcstool_field_it_never_reads_is_ignored() -> None:
    [reference] = _parse(
        "d.repos", f"repositories:\n  a:\n    type: git\n    url: u\n    note: {LONG}\n"
    )
    assert reference.target_key == "a"


WEST_REMOTES = "  remotes:\n    - {name: r, url-base: 'https://h/o'}\n"


@pytest.mark.parametrize(
    "project",
    [
        f"{{name: a, remote: r, path: {LONG}}}",
        f"{{name: a, remote: r, repo-path: {LONG}}}",
        f"{{name: a, url: {LONG}}}",
        f"{{name: a, remote: r, revision: {LONG}}}",
        f"{{name: a, remote: {LONG}}}",
    ],
    ids=["path", "repo-path", "url", "revision", "remote"],
)
def test_a_west_project_with_an_over_long_field_it_reads_gives_no_claim(project: str) -> None:
    # `path` would be claimed at the name, `repo-path` would give a URL the file never stated.
    text = f"manifest:\n{WEST_REMOTES}  projects:\n    - {project}\n"

    assert _west(text) == []


def test_a_west_manifest_keeps_its_other_projects_when_one_is_skipped() -> None:
    text = (
        f"manifest:\n{WEST_REMOTES}  projects:\n    - {{name: a, path: {LONG}}}\n"
        "    - {name: ok, remote: r}\n"
    )

    [reference] = _west(text)

    assert (reference.target_key, reference.meta["url"]) == ("ok", "https://h/o/ok")


def test_a_west_project_naming_an_over_long_remote_gives_no_claim() -> None:
    # The remote is unread, and the project would read it by its short name.
    text = (
        f"manifest:\n  remotes:\n    - {{name: r, url-base: {LONG}}}\n"
        "  projects:\n    - {name: a, remote: r}\n"
    )

    assert _west(text) == []


def test_a_west_project_with_its_own_url_keeps_its_claim_beside_an_unread_remote() -> None:
    text = (
        f"manifest:\n  remotes:\n    - {{name: r, url-base: {LONG}}}\n"
        "  projects:\n    - {name: a, url: 'https://h/a'}\n    - {name: b, remote: r}\n"
    )

    [reference] = _west(text)

    assert (reference.target_key, reference.meta["url"]) == ("a", "https://h/a")


def test_a_west_default_revision_over_long_does_not_hide_a_project_with_its_own() -> None:
    text = (
        f"manifest:\n  defaults: {{revision: {LONG}}}\n"
        "  projects:\n    - {name: a, revision: v1, url: u}\n"
    )

    [reference] = _west(text)

    assert (reference.target_key, reference.meta["revision"]) == ("a", "v1")


def test_a_west_remote_with_an_over_long_name_is_no_remote() -> None:
    text = (
        f"manifest:\n  remotes:\n    - {{name: {LONG}, url-base: 'https://h/o'}}\n"
        "  projects:\n    - {name: a, remote: r}\n"
    )

    [reference] = _west(text)

    assert "url" not in reference.meta


@pytest.mark.parametrize("key", ["revision", "remote"])
def test_an_over_long_west_default_is_absent_not_another_value(key: str) -> None:
    # A project that would inherit the default makes no claim: not a claim without it either.
    text = (
        f"manifest:\n  defaults: {{{key}: {LONG}}}\n{WEST_REMOTES}  projects:\n    - {{name: a}}\n"
    )

    assert _west(text) == []


def test_a_west_default_within_the_bound_is_inherited() -> None:
    """The control for the test above: the same manifest with short defaults gives both."""
    text = (
        f"manifest:\n  defaults: {{revision: main, remote: r}}\n{WEST_REMOTES}"
        "  projects:\n    - {name: a}\n"
    )

    [reference] = _west(text)

    assert (reference.meta["revision"], reference.meta["url"]) == ("main", "https://h/o/a")


REPO_REMOTE = '<remote name="o" fetch="https://h/o"/>'


@pytest.mark.parametrize(
    "attributes",
    [f'path="{LONG}"', f'remote="{LONG}"', f'revision="{LONG}"'],
    ids=["path", "remote", "revision"],
)
def test_a_repo_project_with_an_over_long_field_it_reads_gives_no_claim(attributes: str) -> None:
    content = (
        f'<manifest>{REPO_REMOTE}<default remote="o"/><project name="a" {attributes}/></manifest>'
    )

    assert _parse("default.xml", content) == []


def test_a_repo_remote_with_an_over_long_revision_gives_no_claim_to_a_project_naming_it() -> None:
    content = (
        f'<manifest><remote name="o" fetch="https://h/o" revision="{LONG}"/>'
        '<default remote="o"/><project name="a"/></manifest>'
    )

    assert _parse("default.xml", content) == []


def test_an_over_long_repo_default_revision_is_absent_not_another_value() -> None:
    # `a` would inherit the unread revision and makes no claim; `b` states its own.
    content = (
        f'<manifest>{REPO_REMOTE}<default remote="o" revision="{LONG}"/>'
        '<project name="a"/><project name="b" revision="v1"/></manifest>'
    )

    references = _parse("default.xml", content)

    assert [(r.target_key, r.meta.get("revision")) for r in references] == [("b", "v1")]
    assert all(r.meta["url"].startswith("https://h/o/") for r in references)


def test_a_repo_project_naming_another_remote_keeps_its_claim_beside_an_unread_one() -> None:
    """T1: the default's remote is unread, so `a` (which inherits it) makes no claim; `b` names
    a readable remote and inherits only the default's short revision."""
    content = (
        f'<manifest><remote name="o" fetch="{LONG}" revision="stable"/>'
        '<remote name="p" fetch="https://h/p"/><default remote="o" revision="main"/>'
        '<project name="a"/><project name="b" remote="p"/></manifest>'
    )

    [reference] = _parse("default.xml", content)

    assert reference.target_key == "b"
    assert reference.meta["url"] == "https://h/p/b"
    assert reference.meta["revision"] == "main"


def test_a_repo_project_with_its_own_revision_naming_an_unread_remote_gives_no_claim() -> None:
    """T2: its own revision does not make a remote it names readable."""
    content = (
        f'<manifest><remote name="o" fetch="{LONG}"/><default remote="o"/>'
        '<project name="a" revision="v1"/><project name="b" remote="o" revision="v1"/></manifest>'
    )

    assert _parse("default.xml", content) == []


def test_an_over_long_repo_default_remote_is_no_claim_for_a_project_inheriting_it() -> None:
    """T3: `a` would inherit the unread default remote; `b` names its own."""
    content = (
        f'<manifest>{REPO_REMOTE}<default remote="{LONG}"/>'
        '<project name="a"/><project name="b" remote="o"/></manifest>'
    )

    [reference] = _parse("default.xml", content)

    assert reference.target_key == "b"
    assert reference.meta["url"] == "https://h/o/b"


def test_a_repo_remote_revision_is_read_before_an_unread_default_revision() -> None:
    """T4, the control for precedence: the remote's revision is read, so the default's
    over-long one is never inherited and `a` keeps its claim."""
    content = (
        '<manifest><remote name="o" fetch="https://h/o" revision="r1"/>'
        f'<default remote="o" revision="{LONG}"/><project name="a"/></manifest>'
    )

    [reference] = _parse("default.xml", content)

    assert (reference.target_key, reference.meta["revision"]) == ("a", "r1")


def test_a_repo_remote_with_an_empty_name_is_no_remote_and_fabricates_no_url() -> None:
    """T7: a project naming no remote looks up the empty name; an empty-named remote is not it."""
    content = '<manifest><remote name="" fetch="https://h/x"/><project name="a"/></manifest>'

    [reference] = _parse("default.xml", content)

    assert reference.target_key == "a"
    assert "url" not in reference.meta


def test_an_over_long_fetch_on_an_empty_named_repo_remote_does_not_skip_a_project() -> None:
    """T8: the empty-named remote is no remote, so it cannot mark the empty name unread."""
    content = f'<manifest><remote name="" fetch="{LONG}"/><project name="a"/></manifest>'

    [reference] = _parse("default.xml", content)

    assert reference.target_key == "a"
    assert "url" not in reference.meta


def test_a_five_thousand_character_path_gives_no_claim() -> None:
    long = "a" * 5_000
    assert _parse(".gitmodules", f'[submodule "a"]\n\tpath = {long}\n\turl = u\n') == []
    assert _parse("d.repos", f"repositories:\n  {long}:\n    type: git\n    url: u\n") == []


def test_a_five_thousand_character_fetch_gives_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    joined: list[str] = []
    real = inclusion_parsers._fetch_url

    def recorded(fetch: str, name: str) -> str:
        joined.append(fetch)
        return real(fetch, name)

    monkeypatch.setattr(inclusion_parsers, "_fetch_url", recorded)
    content = (
        f'<manifest><remote name="o" fetch="{"h" * 5_000}"/><default remote="o"/>'
        '<project name="a"/><project name="b"/></manifest>'
    )

    references, diagnostics = repo_manifest_references(content)

    assert diagnostics == []
    # Both inherit the unread remote, so neither makes a claim.
    assert references == []
    # The remote with the long attribute is skipped: no URL is even joined from it.
    assert joined == []


def _vcstool_after(rejected: int) -> str:
    lines = ["s: &s {type: git, url: u}", "repositories:"]
    lines += [f"  /p{n}: *s" for n in range(rejected)]
    lines.append("  ok: {type: git, url: v}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("rejected", "expected"),
    [(MAX_ENTRIES_READ, []), (MAX_ENTRIES_READ - 1, ["ok"])],
    ids=["cap-reached", "one-under"],
)
def test_vcstool_stops_after_the_entries_it_may_read_claims_or_not(
    rejected: int, expected: list[str]
) -> None:
    content = _vcstool_after(rejected)

    references, diagnostics = _within(120, lambda: vcstool_references(content))

    assert diagnostics == []
    assert [r.target_key for r in references] == expected


@pytest.mark.parametrize(
    ("before", "expected"),
    [(MAX_ENTRIES_READ, []), (MAX_ENTRIES_READ - 1, ["ok"])],
    ids=["cap-reached", "one-under"],
)
def test_west_stops_after_the_projects_it_may_read_claims_or_not(
    before: int, expected: list[str]
) -> None:
    lines = ["s: &s {name: a, path: /x}", "manifest:", "  projects:"]
    lines += ["    - *s"] * before
    lines.append("    - {name: ok}")
    text = "\n".join(lines) + "\n"

    references = _within(120, lambda: _west(text))

    assert [r.target_key for r in references] == expected


@pytest.mark.parametrize(
    ("before", "expected"),
    [(MAX_ENTRIES_READ, []), (MAX_ENTRIES_READ - 1, [("p", "https://example.com/base/p")])],
    ids=["cap-reached", "one-under"],
)
def test_west_ignores_the_remotes_past_the_cap_and_still_reads_the_projects(
    before: int, expected: list[tuple[str, str]]
) -> None:
    lines = ["s: &s {name: r, url-base: 'https://x.example'}", "manifest:", "  remotes:"]
    lines += ["    - *s"] * before
    lines += [
        "    - {name: late, url-base: 'https://example.com/base'}",
        "  projects:",
        "    - {name: p, remote: late}",
    ]
    text = "\n".join(lines) + "\n"

    references = _within(120, lambda: _west(text))

    # The list is read as far as the cap: a project naming a remote past it would read what was
    # left unread, so it makes no claim; one under the cap is read however long the list.
    assert [(r.target_key, r.meta.get("url")) for r in references] == expected
