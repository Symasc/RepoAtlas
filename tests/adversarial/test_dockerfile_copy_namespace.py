"""``COPY --from`` sources are not repository files (V1_SPEC §7.8, ARCHITECTURE.md §5).

Docker's grammar is::

    COPY [--from=<image|stage|context>] <src> ... <dest>

Without ``--from``, sources are resolved in the **build context** — the tree
RepoAtlas indexes — so a repository file may genuinely be the thing named. With
``--from``, they are resolved in the root filesystem of another build stage,
another image, or a named build context. RepoAtlas has none of those, cannot read
them, and in V1 never will: it is a static repository analyser and determining a
stage's contents would mean running the build.

The parser proved only half of what a file reference needs. It proved *what the
source is called* and never asked *where that name is resolved*, so::

    COPY --from=builder assets/main.js /app/main.js

produced, against a repository that happened to contain ``assets/main.js``::

    sym:svc:Dockerfile:stage2:5  --REFERENCES(1.0)-->  file:svc:assets/main.js

A deterministic fact, at full confidence, that was simply untrue. Citation
verification cannot catch it, which is what makes it worse than a fabricated
target: both endpoints genuinely exist and both line numbers are real. The lie is
in the *relationship*, and a checker that verifies endpoints has nothing to
compare it against.

The fix is not confidence calibration. ``REFERENCES … confidence=0.5`` would
claim the same wrong thing less loudly; the relationship is not uncertain local
evidence, it is the wrong namespace. Nor is it associative evidence: RepoAtlas
was not asked to guess where a stage's files came from, and a matching path
string is a coincidence, not a lineage model.

What is *not* suppressed is the instruction. The Dockerfile line keeps its path,
its line number, its raw text and its chunk, so a question about it is still
answerable from the file itself. Only the invented relationship is gone.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import ReposSettings, Settings
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile
from repoatlas.parsers.dockerfile import DockerfileParser
from repoatlas.parsers.dockerwords import SourceDomain, source_domain, split_flags
from repoatlas.services.init_service import initialize
from repoatlas.services.retrieval_service import locate
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import git, init_git_repository

pytestmark = pytest.mark.adversarial


# -- harness ------------------------------------------------------------------


def parse(source: str, name: str = "Dockerfile") -> ParsedFile:
    return DockerfileParser().parse(Path(name), source)


def files(source: str) -> set[str]:
    """Every ``REFERENCES`` target key the parser records as a repository file."""
    return {
        reference.target_key
        for reference in parse(source).references
        if reference.edge_type is EdgeType.REFERENCES and reference.target_kind is EntityType.FILE
    }


def all_keys(source: str) -> set[str]:
    return {reference.target_key for reference in parse(source).references}


def settings_for(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
        }
    )


def open_at(settings: Settings, root: Path) -> Workspace:
    """A workspace whose managed root is ``root``.

    ``initialize`` persists the root into a config file; reopening from the same
    ``Settings`` object does not read it back, so a later ``sync`` would resolve
    no context at all.
    """
    return open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )


def make_repo(root: Path, name: str, files_: dict[str, str]) -> Path:
    repo = root / name
    for rel, content in files_.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def rewrite(repo: Path, source: str) -> None:
    (repo / "Dockerfile").write_text(source, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "rewrite")


def rows(workspace: Workspace, query: str) -> list[sqlite3.Row]:
    return list(workspace.database.execute(query).fetchall())


def file_reference_edges(workspace: Workspace) -> set[tuple[str, str]]:
    """Every ``REFERENCES`` edge whose target is a repository file entity."""
    return {
        (row["src"], row["dst"])
        for row in rows(
            workspace,
            "SELECT src.key AS src, dst.key AS dst FROM edges e "
            "JOIN entities src ON src.id = e.src_id "
            "JOIN entities dst ON dst.id = e.dst_id "
            "WHERE e.type = 'REFERENCES' AND dst.type = 'file'",
        )
    }


def claim_keys(workspace: Workspace) -> set[str]:
    return {row[0] for row in rows(workspace, "SELECT target_key FROM unresolved_refs")}


# -- §1 the review's own reproduction -----------------------------------------

#: The Dockerfile from the finding, verbatim.
F3_DOCKERFILE = """\
FROM node:22 AS builder
WORKDIR /build
RUN mkdir -p assets

FROM node:22
COPY --from=builder assets/main.js /app/main.js
"""

#: The reproduction reduced to the one instruction under test.
STAGE_COPY_DOCKERFILE = """\
FROM node:22 AS builder

FROM node:22
COPY --from=builder assets/main.js /app/main.js
"""

#: A symbol that exists only in the repository copy, so any evidence naming it
#: came from the working tree rather than from the Dockerfile's text.
REPO_ASSET = "export function uniqueBuilderArtifact918273() { return 1; }\n"


@pytest.fixture
def f3_root(tmp_path: Path) -> Path:
    """A managed root holding only the reproduction, so sweeps mean what they say."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(
        root,
        "svc",
        {
            "Dockerfile": F3_DOCKERFILE,
            "assets/main.js": REPO_ASSET,
            "app.py": "VALUE = 1\n",
        },
    )
    return root


@pytest.fixture
def f3_indexed(tmp_path: Path, f3_root: Path) -> Iterator[Workspace]:
    workspace = open_workspace(settings=settings_for(tmp_path), require_root=False)
    try:
        initialize(workspace, f3_root, config_path=tmp_path / "c.yaml")
        yield workspace
    finally:
        workspace.close()


def test_the_reproduction_creates_no_repository_file_relationship(
    f3_indexed: Workspace,
) -> None:
    """The finding, end to end through the real indexer.

    Before this pass the edge below existed at confidence 1.0, produced by the
    global linker from an unresolved ``file`` claim the parser had no business
    making.
    """
    assert file_reference_edges(f3_indexed) == set()
    assert "assets/main.js" not in claim_keys(f3_indexed)


def test_no_edge_of_any_kind_reaches_the_repository_asset(f3_indexed: Workspace) -> None:
    """Not merely no ``REFERENCES``: nothing may point at it from the Dockerfile."""
    reaching = rows(
        f3_indexed,
        "SELECT e.type, src.key AS src FROM edges e "
        "JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key LIKE '%assets/main.js'",
    )
    assert [dict(row) for row in reaching] == []


def test_the_repository_file_is_still_independently_retrievable(
    f3_indexed: Workspace, f3_root: Path
) -> None:
    """Suppressing a false edge must not cost the true evidence beside it."""
    context = f3_indexed.resolver().resolve(f3_root, command="locate")
    evidence = locate(f3_indexed, "uniqueBuilderArtifact918273", context)
    assert any("assets/main.js" in candidate.path for candidate in evidence.candidates)


def test_the_dockerfile_line_survives_as_provenance(f3_indexed: Workspace, f3_root: Path) -> None:
    """§38. The instruction is real; only the relationship was invented.

    Its path, line number and raw text stay retrievable, so "where is
    assets/main.js copied from" is still answerable — truthfully, from the
    Dockerfile itself rather than from a fabricated edge.
    """
    context = f3_indexed.resolver().resolve(f3_root, command="locate")
    evidence = locate(f3_indexed, "assets/main.js", context)
    docker_lines = [
        candidate
        for candidate in evidence.candidates
        if candidate.path == "Dockerfile" and "COPY --from=builder" in (candidate.text or "")
    ]
    assert docker_lines, [c.path for c in evidence.candidates]
    assert docker_lines[0].line_start == 6


def test_no_evidence_arrives_through_a_graph_hop_from_the_dockerfile(
    f3_indexed: Workspace, f3_root: Path
) -> None:
    """§35, §36. The false edge was an expansion edge, and it dragged in a hop.

    Before the fix, ``locate "assets/main.js"`` returned ``Dockerfile`` line 5 —
    ``FROM node:22``, a line that has nothing to do with the query — reached by
    expanding the fabricated ``REFERENCES`` edge from the repository file back
    into the Dockerfile's stage.
    """
    from repoatlas.models.evidence import EvidenceSource

    context = f3_indexed.resolver().resolve(f3_root, command="locate")
    evidence = locate(f3_indexed, "assets/main.js", context)
    graph_hops = [c for c in evidence.candidates if c.source is EvidenceSource.GRAPH]
    assert graph_hops == [], [(c.path, c.line_start, c.text) for c in graph_hops]


# -- §13 the control: a local COPY still resolves ------------------------------


def test_a_local_copy_still_names_a_repository_file() -> None:
    """The regression control. Half of this pass is *not* breaking this."""
    assert files("COPY assets/main.js /app/main.js\n") == {"assets/main.js"}
    assert files("COPY src/app.py /app/app.py\n") == {"src/app.py"}


def test_a_local_copy_still_links_end_to_end(tmp_path: Path) -> None:
    """And through the linker, not only at the parser."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(
        root,
        "svc",
        {
            "Dockerfile": "FROM node:22\nCOPY assets/main.js /app/main.js\n",
            "assets/main.js": REPO_ASSET,
        },
    )
    workspace = open_workspace(settings=settings_for(tmp_path), require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert file_reference_edges(workspace) == {
            ("sym:svc:Dockerfile:stage1:1", "file:svc:assets/main.js")
        }
    finally:
        workspace.close()


# -- §2, §7-§11 the stage-source matrix ---------------------------------------

#: Every shape of ``--from`` Docker's grammar accepts, against a repository that
#: contains the very path each one names.
STAGE_SOURCES = (
    pytest.param("COPY --from=builder assets/main.js /app/\n", id="named-stage-relative"),
    pytest.param("COPY --from=builder /out/app /app/\n", id="named-stage-absolute"),
    pytest.param("COPY --from=0 assets/main.js /app/\n", id="numeric-stage"),
    pytest.param("COPY --from=nginx:1.27 /etc/nginx/nginx.conf /x\n", id="external-image"),
    pytest.param("COPY --from=nginx assets/main.js /app/\n", id="external-image-no-tag"),
    pytest.param("COPY --from=ctx assets/main.js /app/\n", id="named-context"),
    pytest.param("COPY --from=${BUILD_STAGE} assets/main.js /app/\n", id="dynamic-stage"),
    pytest.param("COPY --from=$STAGE assets/main.js /app/\n", id="dynamic-stage-bare"),
)


@pytest.mark.parametrize("source", STAGE_SOURCES)
def test_no_stage_source_becomes_a_repository_file(source: str) -> None:
    assert files(source) == set()


def test_a_relative_stage_source_is_the_dangerous_one(f3_indexed: Workspace) -> None:
    """§7. Absolute sources were already rejected downstream *by accident*.

    ``/out/app`` never resolved because repository containment rejects an
    absolute path, so the finding was invisible until a relative source made the
    same claim resolvable. That incidental guard is not what this relies on:
    §8 pins the parser emitting nothing, upstream of it.
    """
    assert files("COPY --from=builder assets/main.js /app/\n") == set()


def test_an_absolute_stage_source_is_suppressed_at_the_parser() -> None:
    """§8. Not "the linker fails to resolve it" — nothing is claimed at all.

    Pinning the parser rather than the outcome is what stops a future change to
    containment or to path resolution from quietly reviving F3.
    """
    parsed = parse("FROM alpine AS builder\nFROM alpine\nCOPY --from=builder /out/app /app/\n")
    assert not any(reference.target_kind is EntityType.FILE for reference in parsed.references), [
        r.target_key for r in parsed.references
    ]


def test_a_stage_named_like_a_repository_directory_does_not_resolve(tmp_path: Path) -> None:
    """§11. ``builder`` is a stage here and a directory there. Different namespaces."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(
        root,
        "svc",
        {
            "Dockerfile": "FROM alpine AS builder\nFROM alpine\nCOPY --from=builder foo.txt /x\n",
            "builder/foo.txt": "BUILDER_DIR_MARKER = 1\n",
            "foo.txt": "ROOT_MARKER = 1\n",
        },
    )
    workspace = open_workspace(settings=settings_for(tmp_path), require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert file_reference_edges(workspace) == set()
    finally:
        workspace.close()


def test_the_same_path_in_several_repositories_resolves_nowhere(tmp_path: Path) -> None:
    """§12. ``REFERENCES`` is a cross-repository expansion edge.

    One false claim would otherwise have as many candidate targets as there are
    repositories holding that path, and the global linker resolves across the
    whole managed set.
    """
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "repo-a", {"assets/main.js": "A = 1\n"})
    make_repo(root, "repo-b", {"assets/main.js": "B = 1\n"})
    make_repo(
        root,
        "repo-c",
        {"Dockerfile": STAGE_COPY_DOCKERFILE},
    )
    workspace = open_workspace(settings=settings_for(tmp_path), require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert file_reference_edges(workspace) == set()
    finally:
        workspace.close()


# -- §14, §15, §16 operand and flag grammar -----------------------------------


def test_every_source_operand_inherits_the_from_namespace() -> None:
    """§14. Not only the first. ``--from`` is an instruction-level flag."""
    assert files("COPY a.txt b.txt /dest/\n") == {"a.txt", "b.txt"}
    assert files("COPY --from=builder a.txt b.txt /dest/\n") == set()


def test_the_json_form_does_not_bypass_the_namespace_check() -> None:
    """§15. JSON supplies quoting, not a different resolution rule."""
    assert files('COPY ["a.txt", "b.txt", "/dest/"]\n') == {"a.txt", "b.txt"}
    assert files('COPY --from=builder ["a.txt", "b.txt", "/dest/"]\n') == set()


#: ``--from`` in every position Docker's flag grammar allows it to appear.
FLAG_ORDERS = (
    "COPY --from=builder a b\n",
    "COPY --chown=1000 --from=builder a b\n",
    "COPY --from=builder --chown=1000 a b\n",
    "COPY --chown=1000:1000 --chmod=755 --from=builder a b\n",
    "COPY --link --from=builder a b\n",
)


@pytest.mark.parametrize("source", FLAG_ORDERS)
def test_the_namespace_check_does_not_depend_on_flag_order(source: str) -> None:
    """§16. Docker does not require ``--from`` first, so neither may this."""
    assert files(source) == set()


def test_flags_that_are_not_from_leave_the_build_context_alone() -> None:
    """The other half of §16: only ``--from`` relocates anything."""
    assert files("COPY --chown=1000 a.txt /dest/\n") == {"a.txt"}
    assert files("COPY --chmod=755 --link a.txt /dest/\n") == {"a.txt"}
    assert files("COPY --parents src/a.txt /dest/\n") == {"src/a.txt"}


def test_source_domain_reads_the_flag_name_not_the_line() -> None:
    """§24. One classifier, asked of parsed flags — not a substring search.

    ``--from`` inside an operand is a filename, and a rule that searched the raw
    line would suppress a legitimate local reference because a file was named
    ``--from-cache``.
    """
    assert source_domain(split_flags("--from=builder a b")[0]) is SourceDomain.FOREIGN
    assert source_domain(split_flags("--chown=1 --from=x a b")[0]) is SourceDomain.FOREIGN
    assert source_domain(split_flags("--chown=1000 a b")[0]) is SourceDomain.BUILD_CONTEXT
    assert source_domain(split_flags("a b")[0]) is SourceDomain.BUILD_CONTEXT


def test_a_file_spelled_like_the_flag_is_still_a_local_reference() -> None:
    """The scattered-substring-check trap, pinned."""
    assert files("COPY from-cache.txt /dest/\n") == {"from-cache.txt"}


# -- §19, §20 dynamic namespaces ----------------------------------------------


def test_a_dynamic_stage_is_not_more_resolvable_than_a_named_one() -> None:
    """§19. A namespace nothing names is less provable, not more."""
    assert files("COPY --from=${BUILD_STAGE} assets/main.js /app/\n") == set()
    assert all_keys("COPY --from=${BUILD_STAGE} assets/main.js /app/\n") == set()


def test_a_dynamic_source_under_a_dynamic_stage_stays_suppressed() -> None:
    """§20. Both halves unproven; neither half rescues the other."""
    assert files("COPY --from=${STAGE} ${ARTIFACT} /app/\n") == set()
    assert all_keys("COPY --from=${STAGE} ${ARTIFACT} /app/\n") == set()


# -- §21 destinations are never sources ---------------------------------------


def test_a_destination_is_never_a_repository_file_reference() -> None:
    """§21. The last operand is a path *inside the image being built*."""
    assert "/app/main.js" not in all_keys("COPY assets/main.js /app/main.js\n")
    assert "/app/" not in all_keys("COPY --from=builder assets/main.js /app/\n")
    assert files("COPY app.py app.py\n") == {"app.py"}


# -- §22, §23 ADD --------------------------------------------------------------


def test_ordinary_add_is_unchanged() -> None:
    assert files("ADD src/app.py /app/app.py\n") == {"src/app.py"}


def test_a_remote_add_remains_the_known_separate_finding() -> None:
    """§41. Carried forward untouched: a URL is read correctly and filed wrongly.

    Not fixed here. It is a *kind* error on a value the source really does name,
    not a namespace error, and it is pinned so this pass's shared-helper changes
    cannot silently fix or worsen it.
    """
    assert files("ADD https://example.com/x.tar.gz /tmp/x.tar.gz\n") == {
        "https://example.com/x.tar.gz"
    }


def test_add_from_would_be_treated_as_foreign_too() -> None:
    """§23. Docker documents ``--from`` for ``COPY`` only; ``ADD`` shares the rule.

    A conservatism, not a feature claim: if a Dockerfile writes it, suppressing
    costs a reference RepoAtlas could not justify anyway, while resolving it
    invents exactly the fact this pass removed.
    """
    assert files("ADD --from=builder assets/main.js /app/\n") == set()


# -- §33 instructions are independent ------------------------------------------


def test_a_from_flag_does_not_leak_into_the_next_instruction() -> None:
    """§33. Every instruction is classified from its own flags."""
    source = "FROM alpine AS builder\nFROM alpine\nCOPY --from=builder a.txt /x\nCOPY b.txt /y\n"
    assert files(source) == {"b.txt"}


def test_a_local_copy_before_a_stage_copy_keeps_its_reference() -> None:
    source = "FROM alpine AS builder\nCOPY b.txt /y\nFROM alpine\nCOPY --from=builder a.txt /x\n"
    assert files(source) == {"b.txt"}


# -- §34 a realistic multi-stage build -----------------------------------------

MULTISTAGE = """\
FROM node AS deps
COPY package.json ./
RUN npm ci

FROM deps AS builder
COPY src/index.js ./
RUN npm run build

FROM nginx
COPY --from=builder src/dist/bundle.js /usr/share/nginx/html/bundle.js
"""


def test_a_realistic_multi_stage_build_keeps_local_facts_and_drops_stage_ones(
    tmp_path: Path,
) -> None:
    """§34. The final stage must not point at the repository's own ``src/dist``."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(
        root,
        "web",
        {
            "Dockerfile": MULTISTAGE,
            "package.json": '{"name":"web","version":"1.0.0"}\n',
            "src/index.js": "export const x = 1;\n",
            "src/dist/bundle.js": "export const BUNDLE_MARKER_918273 = 1;\n",
        },
    )
    workspace = open_workspace(settings=settings_for(tmp_path), require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        edges = file_reference_edges(workspace)
        targets = {dst for _src, dst in edges}
        assert "file:web:package.json" in targets
        assert "file:web:src/index.js" in targets
        assert "file:web:src/dist/bundle.js" not in targets, edges
    finally:
        workspace.close()


# -- §28, §29 an index built before the fix ------------------------------------


def test_an_index_holding_the_false_edge_is_repaired_by_an_ordinary_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§28, §29. The Dockerfile is byte-identical, so content cannot trigger this.

    The old parser is simulated by restoring the pre-fix behaviour *and* the
    parser version it shipped with, indexing, then running an ordinary
    ``repoatlas sync`` on the fixed build. The facts-version change is what makes
    the unchanged file reparse; without the version bump the stale edge would
    survive every sync until the user deleted their index.
    """
    from repoatlas.parsers import dockerfile as dockerfile_module

    root = tmp_path / "root"
    root.mkdir()
    repo = make_repo(
        root,
        "svc",
        {"Dockerfile": F3_DOCKERFILE, "assets/main.js": REPO_ASSET},
    )
    settings = settings_for(tmp_path)

    # --- the pre-fix build -------------------------------------------------
    def legacy_copy(self, instruction, references, stage):  # type: ignore[no-untyped-def]
        from repoatlas.models.parsing import ParsedReference
        from repoatlas.parsers.dockerwords import exec_form, expanded, split_words

        _flags, rest = dockerfile_module.split_flags(instruction.arguments)
        argv = exec_form(rest)
        words = (
            [expanded(element) for element in argv]
            if argv is not None
            else [w for w in split_words(rest) if not w.raw.startswith("--")]
        )
        for source in words[:-1]:
            if source.literal is None:
                continue
            references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.FILE,
                    target_key=source.literal,
                    line_start=instruction.line_start,
                    line_end=instruction.line_end,
                    source_symbol=stage,
                    meta={"via": instruction.keyword},
                )
            )

    monkeypatch.setattr(dockerfile_module.DockerfileParser, "_copy", legacy_copy)
    monkeypatch.setattr(dockerfile_module.DockerfileParser, "_add", legacy_copy)
    monkeypatch.setattr(dockerfile_module.DockerfileParser, "version", "3")

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        stale = file_reference_edges(workspace)
        assert ("sym:svc:Dockerfile:stage2:5", "file:svc:assets/main.js") in stale, stale
    finally:
        workspace.close()

    # --- the fixed build, one ordinary sync --------------------------------
    # `undo` restores the real `_copy`, `_add` and `version` together, which is
    # exactly the upgrade being simulated: new code, same Dockerfile, same index.
    monkeypatch.undo()

    digest_before = (repo / "Dockerfile").read_bytes()

    workspace = open_at(settings, root)
    try:
        sync(workspace, workspace.resolver().resolve(root, command="sync"))
        assert file_reference_edges(workspace) == set()
        assert "assets/main.js" not in claim_keys(workspace)
    finally:
        workspace.close()

    assert (repo / "Dockerfile").read_bytes() == digest_before, "the file was not touched"

    # --- a second sync is an ordinary no-op --------------------------------
    workspace = open_at(settings, root)
    try:
        report = sync(workspace, workspace.resolver().resolve(root, command="sync"))
        entry = report.repositories[0]
        assert (entry.changed, entry.new, entry.deleted) == (0, 0, 0), entry
        state = status(workspace, workspace.resolver().resolve(root, command="status"))
        assert state.index_state == "current", state.index_state
    finally:
        workspace.close()


def test_the_dockerfile_parser_version_changed_with_its_semantics() -> None:
    """§47. The bump is the mechanism, so it is asserted rather than assumed."""
    assert DockerfileParser.version != "3"


# -- §30, §31, §32 incremental transitions -------------------------------------


def indexed_file_edges(settings: Settings, root: Path) -> set[tuple[str, str]]:
    workspace = open_at(settings, root)
    try:
        return file_reference_edges(workspace)
    finally:
        workspace.close()


def resync(settings: Settings, root: Path) -> None:
    workspace = open_at(settings, root)
    try:
        sync(workspace, workspace.resolver().resolve(root, command="sync"))
    finally:
        workspace.close()


@pytest.fixture
def transition(tmp_path: Path) -> tuple[Settings, Path, Path]:
    root = tmp_path / "root"
    root.mkdir()
    repo = make_repo(
        root,
        "svc",
        {
            "Dockerfile": "FROM node:22\nCOPY assets/main.js /app/\n",
            "assets/main.js": REPO_ASSET,
        },
    )
    settings = settings_for(tmp_path)
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
    finally:
        workspace.close()
    return settings, root, repo


LOCAL_EDGE = ("sym:svc:Dockerfile:stage1:1", "file:svc:assets/main.js")


def test_local_to_stage_removes_the_edge(transition: tuple[Settings, Path, Path]) -> None:
    """§30. No stale deterministic evidence may survive the transition."""
    settings, root, repo = transition
    assert LOCAL_EDGE in indexed_file_edges(settings, root)

    rewrite(repo, STAGE_COPY_DOCKERFILE)
    resync(settings, root)

    assert indexed_file_edges(settings, root) == set()


def test_stage_to_local_restores_the_edge(transition: tuple[Settings, Path, Path]) -> None:
    """§31. Suppression is a property of the instruction, not a sticky flag."""
    settings, root, repo = transition
    rewrite(repo, STAGE_COPY_DOCKERFILE)
    resync(settings, root)
    assert indexed_file_edges(settings, root) == set()

    rewrite(repo, "FROM node:22\nCOPY assets/main.js /app/\n")
    resync(settings, root)

    assert LOCAL_EDGE in indexed_file_edges(settings, root)


def test_changing_the_source_stage_keeps_both_states_empty(
    transition: tuple[Settings, Path, Path],
) -> None:
    """§32."""
    settings, root, repo = transition
    for stage in ("builder", "release"):
        rewrite(
            repo,
            f"FROM node:22 AS builder\nFROM node:22 AS release\nFROM node:22\n"
            f"COPY --from={stage} assets/main.js /app/\n",
        )
        resync(settings, root)
        assert indexed_file_edges(settings, root) == set(), stage


# -- §39 dependency extraction is untouched ------------------------------------


def test_image_dependencies_are_unchanged() -> None:
    parsed = parse("FROM node:22 AS builder\nFROM nginx:1.27\nCOPY --from=builder /a /b\n")
    packages = {
        reference.target_key
        for reference in parsed.references
        if reference.edge_type is EdgeType.DEPENDS_ON
    }
    assert packages == {"node:22", "nginx:1.27"}


def test_an_external_image_in_copy_from_is_not_a_dependency() -> None:
    """It is a source of files, not a base image, and V1 models neither for it."""
    parsed = parse("FROM alpine\nCOPY --from=nginx:1.27 /etc/nginx/nginx.conf /x\n")
    packages = {
        reference.target_key
        for reference in parsed.references
        if reference.edge_type is EdgeType.DEPENDS_ON
    }
    assert packages == {"alpine"}


# -- §50 cost ------------------------------------------------------------------


def test_the_namespace_check_reads_each_instructions_flags_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§50, counted rather than timed: one flag scan and one domain decision per
    instruction, never one per source operand, whether or not the sources are
    suppressed. A count is the claim itself; a stopwatch only approximates it."""
    from repoatlas.parsers import dockerfile

    calls: Counter[str] = Counter()

    def counted(name: str, real: Callable[..., object]) -> Callable[..., object]:
        def wrapper(*args: object, **kwargs: object) -> object:
            calls[name] += 1
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(dockerfile, "split_flags", counted("flags", dockerfile.split_flags))
    monkeypatch.setattr(dockerfile, "source_domain", counted("domain", dockerfile.source_domain))

    def scans(source: str) -> dict[str, int]:
        calls.clear()
        parse(source)
        return dict(calls)

    operands = " ".join(f"src/m{i}.js" for i in range(20))
    local = "FROM node:22\n" + "".join(f"COPY {operands} /app/{i}/\n" for i in range(10))
    staged = "FROM node:22 AS b\nFROM node:22\n" + "".join(
        f"COPY --from=b {operands} /app/{i}/\n" for i in range(10)
    )
    # Ten COPY instructions of twenty sources each: ten domain decisions, and one
    # flag scan per instruction (each FROM scans its own flags too).
    assert scans(local) == {"flags": 11, "domain": 10}
    assert scans(staged) == {"flags": 12, "domain": 10}
