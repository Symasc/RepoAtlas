"""Repository inclusions: submodules and the manifests that state them, from the files and Git
metadata of the repositories to the links, the unsettled list, declarations and change impact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidInclusionDeclarationError
from repoatlas.capabilities.impact import decisions_near
from repoatlas.capabilities.inclusions import includers
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.topology import unsettled_inclusions
from repoatlas.gitstate.client import GitClient, GitResult
from repoatlas.gitstate.config import normalize_git_url, parse_git_config
from repoatlas.gitstate.remotes import read_remotes
from repoatlas.graph.inclusions import pin_in_sync
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.inclusions import (
    INCLUSION_FAMILY,
    InclusionDeclarationCreateRequest,
    InclusionDeclarationFields,
    InclusionDeclarationsRequest,
)
from repoatlas.models.topology import LinkKind, TopologyOverviewRequest
from repoatlas.parsers.inclusions import RepositoryInclusionParser
from repoatlas.parsers.structured import StructuredParser
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import LinkDecisionRow
from tests.conftest import git, init_git_repository

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


# -- URLs ------------------------------------------------------------------------------------


def test_one_repository_spelled_every_way_is_one_identity() -> None:
    spellings = [
        "git@github.com:Org/Repo.git",
        "ssh://git@github.com:22/org/repo",
        "https://user:token@GitHub.com/org/repo/",
        "git://github.com/org/repo.git",
    ]
    assert {normalize_git_url(url) for url in spellings} == {"github.com/org/repo"}


def test_a_relative_url_resolves_against_the_including_remote_then_its_directory() -> None:
    assert normalize_git_url("../lib.git", base="github.com/org/app") == "github.com/org/lib"
    assert normalize_git_url("./lib", base="github.com/org/app") == "github.com/org/app/lib"
    assert normalize_git_url("../../x", base="github.com/org") is None
    assert normalize_git_url("../lib", base_dir="/work/app") == "file:/work/lib"


def test_remotes_are_stored_as_identities_with_rewrites_applied_and_no_credentials(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[url "https://github.com/"]\n\tinsteadOf = gh:\n'
        '[remote "upstream"]\n\turl = https://bob:s3cret@example.com/x/up.git\n'
        '[remote "origin"]\n\turl = gh:org/r\n'
    )
    assert read_remotes(repo) == ("github.com/org/r", "example.com/x/up")


def test_a_submodule_checkout_reads_the_config_its_gitdir_file_points_at(tmp_path: Path) -> None:
    modules = tmp_path / "parent" / ".git" / "modules" / "child"
    modules.mkdir(parents=True)
    (modules / "config").write_text('[remote "origin"]\n\turl = git@host.example:o/child\n')
    checkout = tmp_path / "parent" / "child"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: ../.git/modules/child\n")
    assert read_remotes(checkout) == ("host.example/o/child",)


def test_only_a_commit_pin_says_whether_a_checkout_is_in_sync() -> None:
    head = "0123456789abcdef0123456789abcdef01234567"
    assert pin_in_sync(head, head) is True
    assert pin_in_sync(head.upper(), head) is True
    assert pin_in_sync("f" * 40, head) is False
    # Only a full-length commit is told from a branch: an abbreviation is unknown, never False.
    assert pin_in_sync("0123456", head) is None
    assert pin_in_sync("fedcba9", head) is None
    assert pin_in_sync("deadbeef", head) is None
    assert pin_in_sync("main", head) is None
    sha256 = "0123456789abcdef" * 4
    assert pin_in_sync(sha256, sha256) is True
    assert pin_in_sync("f" * 64, sha256) is False
    # A SHA-1 pin against a SHA-256 head (or the reverse) cannot be told: never False.
    assert pin_in_sync("f" * 40, sha256) is None
    assert pin_in_sync(sha256, head) is None


# -- what each format states -----------------------------------------------------------------


def _claims(path: str, content: str) -> list[tuple[str, int, int, dict[str, str]]]:
    parser = RepositoryInclusionParser()
    parsed = parser.parse(Path(path), content)
    return [(r.target_key, r.line_start, r.line_end, r.meta) for r in parsed.references]


def test_gitmodules_states_each_section_and_refuses_a_path_leaving_the_repository() -> None:
    claims = _claims(
        ".gitmodules",
        '[submodule "spec"]\n\tpath = spec\n\turl = https://u:p@github.com/org/spec\n'
        '[submodule "lib"]\n  path = "libs/lib" ; comment\n  url = ../lib.git\n  branch = main\n'
        '[submodule "bad"]\n  path = ../outside\n  url = x\n',
    )
    assert claims == [
        (
            "spec",
            1,
            3,
            {
                "format": "gitmodules",
                "base": "repository",
                "name": "spec",
                "url": "https://github.com/org/spec",
            },
        ),
        (
            "libs/lib",
            4,
            7,
            {
                "format": "gitmodules",
                "base": "repository",
                "name": "lib",
                "url": "../lib.git",
                "branch": "main",
            },
        ),
    ]


def test_a_gitrepo_includes_the_directory_holding_it_at_its_commit() -> None:
    [(path, _, _, meta)] = _claims(
        "third_party/sub/.gitrepo",
        "; managed by git-subrepo\n[subrepo]\n\tremote = git@github.com:o/sub.git\n"
        "\tcommit = 0123456789abcdef0123456789abcdef01234567\n",
    )
    assert path == "third_party/sub"
    assert meta["format"] == "git-subrepo"
    assert meta["revision"] == "0123456789abcdef0123456789abcdef01234567"


def test_vcstool_reads_git_entries_only() -> None:
    claims = _claims(
        "deps.repos",
        "repositories:\n  src/a:\n    type: git\n    url: https://github.com/o/a.git\n"
        "    version: v1.2\n  src/b:\n    type: hg\n    url: https://hg.example/b\n",
    )
    assert [(c[0], c[3]["revision"]) for c in claims] == [("src/a", "v1.2")]


def test_a_repo_manifest_joins_each_project_to_its_remote_and_default() -> None:
    claims = _claims(
        "default.xml",
        '<?xml version="1.0"?>\n<manifest>\n'
        '  <remote name="gh" fetch="https://github.com/org" />\n'
        '  <default remote="gh" revision="main" />\n'
        '  <project name="tools" path="src/tools" />\n'
        '  <project name="core" revision="v2" />\n'
        "</manifest>\n",
    )
    assert [(c[0], c[3]["url"], c[3]["revision"], c[3]["base"]) for c in claims] == [
        ("src/tools", "https://github.com/org/tools", "main", "workspace"),
        ("core", "https://github.com/org/core", "v2", "workspace"),
    ]


def test_xml_that_is_no_manifest_or_declares_entities_states_nothing() -> None:
    assert _claims("pom.xml", "<project><modules/></project>") == []
    parsed = RepositoryInclusionParser().parse(
        Path("default.xml"), '<!DOCTYPE m [<!ENTITY a "x">]><manifest/>'
    )
    assert parsed.references == () and len(parsed.diagnostics) == 1


def test_a_west_manifest_builds_urls_from_its_remotes_and_keeps_its_yaml_keys() -> None:
    parsed = StructuredParser().parse(
        Path("west.yml"),
        "manifest:\n  defaults:\n    remote: up\n    revision: main\n"
        "  remotes:\n    - name: up\n      url-base: https://github.com/zephyr\n"
        "  projects:\n    - name: cmsis\n      path: modules/cmsis\n      revision: abc1234\n"
        "    - name: other\n      url: https://example.com/other.git\n",
    )
    inclusions = [r for r in parsed.references if r.meta.get("format") == "west"]
    assert [(r.target_key, r.meta["url"], r.meta.get("revision")) for r in inclusions] == [
        ("modules/cmsis", "https://github.com/zephyr/cmsis", "abc1234"),
        ("other", "https://example.com/other.git", "main"),
    ]
    assert parsed.symbols


# -- resolution, end to end -------------------------------------------------------------------


def _repo(path: Path, files: dict[str, str], remote: str | None = None) -> None:
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    init_git_repository(path)
    if remote is not None:
        git(path, "remote", "add", "origin", remote)


@pytest.fixture
def included(workspace: Workspace, tmp_path: Path) -> Workspace:
    """``app`` includes ``modules/child`` (checked out inside it), ``lib`` (found only by its
    remote, elsewhere), ``dup`` (two clones share its remote) and ``gone`` (not indexed)."""
    root = tmp_path / "repos"
    # the checkout first, so the parent's commit records it as a gitlink
    _repo(root / "app" / "modules" / "child", {"c.py": "C = 1\n"}, "git@github.com:org/child.git")
    _repo(
        root / "app",
        {
            ".gitmodules": '[submodule "child"]\n\tpath = modules/child\n\turl = ../child.git\n'
            '[submodule "lib"]\n\tpath = ext/lib\n\turl = https://github.com/org/lib\n'
            '[submodule "dup"]\n\tpath = ext/dup\n\turl = https://github.com/org/dup\n'
            '[submodule "gone"]\n\tpath = ext/gone\n\turl = https://github.com/org/gone\n',
            "main.py": "import os\n",
        },
        "https://github.com/org/app.git",
    )
    _repo(root / "lib", {"lib.py": "L = 1\n"}, "git@github.com:org/lib.git")
    _repo(root / "dup", {"d.py": "D = 1\n"}, "https://github.com/org/dup.git")
    _repo(root / "forks" / "dup", {"d.py": "D = 1\n"}, "https://github.com/org/dup.git")
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace


def _submodule_links(service: CapabilityService) -> dict[str, list[tuple[str, str, bool | None]]]:
    overview = service.topology_overview(TopologyOverviewRequest(context=GLOBAL))
    return {
        f"{link.source}->{link.target}": [(i.path, i.method, i.in_sync) for i in link.inclusions]
        for link in overview.links
        if link.kind is LinkKind.SUBMODULE
    }


def test_a_checkout_path_or_a_single_remote_links_and_anything_else_is_listed(
    included: Workspace,
) -> None:
    service = CapabilityService(included)
    assert _submodule_links(service) == {
        "app->app/modules/child": [("modules/child", "path", True)],
        "app->lib": [("ext/lib", "remote", None)],
    }
    overview = service.topology_overview(TopologyOverviewRequest(context=GLOBAL))
    assert overview.unsettled_inclusions_total == 2
    assert [(u.path, u.decision, u.candidates) for u in overview.unsettled_inclusions] == [
        ("ext/dup", "ambiguous", ("dup", "forks/dup")),
        ("ext/gone", "unresolved", ()),
    ]


def test_a_declaration_settles_an_ambiguous_entry_at_the_next_sync_and_never_overrides_a_path(
    included: Workspace, tmp_path: Path
) -> None:
    service = CapabilityService(included)

    def declare(path: str, target: str) -> None:
        service.inclusion_declaration_create(
            InclusionDeclarationCreateRequest(
                context=GLOBAL,
                declaration=InclusionDeclarationFields(
                    repository="app", file=".gitmodules", path=path, target=target
                ),
            )
        )

    with pytest.raises(InvalidInclusionDeclarationError, match="already linked"):
        declare("modules/child", "lib")
    declare("ext/dup", "forks/dup")
    [pending] = service.inclusion_declarations(
        InclusionDeclarationsRequest(context=GLOBAL)
    ).declarations
    assert pending.status is None

    sync(included, included.resolver().resolve(tmp_path / "repos"))

    assert _submodule_links(service)["app->forks/dup"] == [("ext/dup", "declared", None)]
    [applied] = service.inclusion_declarations(
        InclusionDeclarationsRequest(context=GLOBAL)
    ).declarations
    assert applied.status == "applied"
    overview = service.topology_overview(TopologyOverviewRequest(context=GLOBAL))
    assert [u.path for u in overview.unsettled_inclusions] == ["ext/gone"]


def test_a_change_in_an_included_repository_reaches_the_repositories_including_it(
    included: Workspace,
) -> None:
    response = CapabilityService(included).impact(ImpactRequest(context=GLOBAL, seed="repo:lib"))
    assert [(i.repository, i.declared_at, i.path) for i in response.inclusions] == [
        ("app", "app/.gitmodules:4", "ext/lib"),
    ]


def test_inclusion_declarations_survive_an_export_and_import(
    included: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    fields = InclusionDeclarationFields(
        repository="app", file=".gitmodules", path="ext/dup", target="dup"
    )
    stored = included.inclusion_declarations.create(fields)
    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    runner = CliRunner()
    exported = tmp_path / "links.json"

    assert runner.invoke(app, ["links", "export", str(exported)]).exit_code == 0
    included.inclusion_declarations.delete(stored.id)
    result = runner.invoke(app, ["links", "import", str(exported)])

    assert result.exit_code == 0, result.output
    [restored] = included.inclusion_declarations.list_all()
    assert (restored.repository, restored.file, restored.path, restored.target) == (
        "app",
        ".gitmodules",
        "ext/dup",
        "dup",
    )


# -- review fixes ------------------------------------------------------------------------------


def test_the_last_value_of_a_repeated_key_wins_and_start_or_end_keys_move_no_line() -> None:
    [(path, start, end, meta)] = _claims(
        ".gitmodules",
        '[submodule "a"]\n\tpath = x\n\tpath = y\n\turl = u\n\turl = v\n'
        "\tstart = 99\n\tend = 98\n\tbranch = one\n\tbranch = two\n",
    )
    assert (path, start, end) == ("y", 1, 9)
    assert (meta["url"], meta["branch"]) == ("v", "two")

    [(path, start, end, meta)] = _claims(
        "vendor/sub/.gitrepo",
        "[subrepo]\n\tremote = a\n\tremote = b\n\tcommit = c1\n\tcommit = c2\n"
        "\tstart = 99\n\tend = 98\n",
    )
    assert (path, start, end) == ("vendor/sub", 1, 7)
    assert (meta["url"], meta["revision"]) == ("b", "c2")


def test_a_value_continued_over_five_lines_is_one_value_and_later_lines_keep_numbers() -> None:
    entries = parse_git_config("[s]\nk = a\\\nb\\\nc\\\nd\\\ne\nj = 1\n")
    assert [(e.key, e.value, e.line) for e in entries] == [("k", "abcde", 2), ("j", "1", 7)]

    # A continuation into an empty line ends there.
    entries = parse_git_config("[s]\nk = a\\\n\nj = 1\n")
    assert [(e.key, e.value, e.line) for e in entries] == [("k", "a", 2), ("j", "1", 4)]

    # A line that is only an even run of backslashes does not continue: the run before it lost
    # one, so parity is the line's own.
    entries = parse_git_config("[s]\nk = a\\\n\\\\\nj = 1\n")
    assert [(e.key, e.value, e.line) for e in entries] == [("k", "a\\", 2), ("j", "1", 4)]

    # A continuation at the end of the file has nothing to continue into.
    assert [e.value for e in parse_git_config("[s]\nk = a\\\n")] == ["a\\"]


def test_a_relative_url_resolved_through_a_directory_leaves_no_local_path_in_evidence(
    workspace: Workspace, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    # No remote, so the URL is resolved against the directory: the identity is a local path.
    _repo(
        root / "app",
        {
            ".gitmodules": '[submodule "lib"]\n\tpath = ext/lib\n\turl = ../lib.git\n'
            '[submodule "gone"]\n\tpath = ext/gone\n\turl = ../gone.git\n'
            '[submodule "net"]\n\tpath = ext/net\n\turl = https://github.com/org/net\n'
        },
    )
    _repo(root / "lib", {"lib.py": "L = 1\n"})
    initialize(workspace, root, config_path=tmp_path / "config.yaml")

    rows = {r.subject: r for r in workspace.knowledge.list_link_decisions(INCLUSION_FAMILY)}
    assert {s: r.decision for s, r in rows.items()} == {
        "ext/lib": "linked",
        "ext/gone": "unresolved",
        "ext/net": "unresolved",
    }
    for row in rows.values():
        assert str(tmp_path) not in row.evidence_json
        assert tmp_path.name not in row.evidence_json
    assert "identity" not in json.loads(rows["ext/gone"].evidence_json)
    # A network identity names no place on this machine, and stays.
    assert json.loads(rows["ext/net"].evidence_json)["identity"] == "github.com/org/net"

    keys = [
        row[0]
        for row in workspace.database.execute(
            "SELECT e.key FROM entities e JOIN files f ON f.id = e.file_id "
            "WHERE f.rel_path = '.gitmodules' AND e.type = 'file'"
        ).fetchall()
    ]
    near, _ = decisions_near(workspace.knowledge, keys)
    assert {d.subject for d in near} == {"ext/gone", "ext/net"}
    text = json.dumps([d.evidence for d in near])
    assert str(tmp_path) not in text and tmp_path.name not in text


def test_in_sync_follows_the_head_that_was_indexed_not_the_head_now(included: Workspace) -> None:
    child = next(r for r in included.repositories.list_all() if r.slug == "app/modules/child")
    assert child.last_indexed_head is not None
    included.repositories.upsert(child.model_copy(update={"head_sha": "f" * 40}))
    moved = next(r for r in included.repositories.list_all() if r.slug == "app/modules/child")
    assert moved.head_sha != moved.last_indexed_head

    assert _submodule_links(CapabilityService(included))["app->app/modules/child"] == [
        ("modules/child", "path", True)
    ]
    [impact] = includers(included, "app/modules/child", frozenset({"app", "app/modules/child"}))
    assert impact.in_sync is True


def _unsettled(repo_id: int, subject: str, decision: str, candidates: list[str]) -> LinkDecisionRow:
    return LinkDecisionRow(
        decision_ref=f"inc1-{subject}",
        family=INCLUSION_FAMILY,
        repo_id=repo_id,
        file_id=1,
        src_id=1,
        line_start=1,
        subject=subject,
        decision=decision,
        evidence_json=json.dumps({"format": "gitmodules", "candidates": candidates}),
        path=".gitmodules",
    )


def test_the_unsettled_total_counts_an_entry_the_list_leaves_out_for_its_candidate() -> None:
    in_scope = {1: "app", 3: "a", 4: "b"}
    rows = [
        _unsettled(1, "both-in-scope", "ambiguous", ["a", "b"]),
        _unsettled(1, "one-outside", "ambiguous", ["a", "outside"]),
        _unsettled(1, "no-candidates", "unresolved", []),
        _unsettled(2, "other-repository", "unresolved", []),
        _unsettled(1, "settled", "linked", []),
    ]

    listed, total = unsettled_inclusions(rows, in_scope)

    # Counted, never named: the entry with a candidate outside the scope is in the total only.
    assert [u.path for u in listed] == ["both-in-scope", "no-candidates"]
    assert total == 3
    assert all("outside" not in u.candidates for u in listed)


def test_a_sync_reads_the_index_once_per_repository_and_keeps_the_gitlinks(
    included: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {r.slug: r.gitlinks for r in included.repositories.list_all()}
    assert [path for path, _ in before["app"]] == ["modules/child"]
    # A file git does not track makes a repository dirty before its index is read, so the
    # gitlinks of that one are asked for once, not twice.
    (tmp_path / "repos" / "lib" / "scratch.txt").write_text("untracked\n")

    stages: dict[Path, int] = {}
    real = GitClient._run

    def counting(self: GitClient, cwd: Path, *args: str) -> GitResult:
        if "--stage" in args:
            stages[cwd] = stages.get(cwd, 0) + 1
        return real(self, cwd, *args)

    monkeypatch.setattr(GitClient, "_run", counting)
    sync(included, included.resolver().resolve(tmp_path / "repos"))

    assert len(stages) == len(before)
    assert set(stages.values()) == {1}
    assert {r.slug: r.gitlinks for r in included.repositories.list_all()} == before
