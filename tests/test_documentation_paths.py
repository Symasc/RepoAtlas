"""A document the question names by path is found, read and kept current.

Markdown was always indexed: every non-binary UTF-8 file within the size cap gets a
``files`` row, a file entity and line-window chunks whatever its language. What
failed was the question. Query analysis recognised a path only by a handful of
source extensions, so ``docs/ARCHITECTURE.md`` was never taken as a path at all —
only its basename reached file lookup, where a basename earns no rank anchor, and
the document an agent asked for came seventh, text-less, behind files that merely
mention its name. The agent concluded that Markdown is not indexed.

The ecosystem below reproduces that shape: the named document is quiet, and many
other files quote its path. Everything else about documents is ordinary evidence,
so the tests also pin what must stay ordinary: inspection, staleness, deletion,
repository scope, and the ignore file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import FileNotIndexedError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.discovery.languages import _EXTENSIONS
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceTier, MatchClass
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.retrieval.query import analyze
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

DOCS = "docs-service"
OTHER = "other-service"
DOC = "docs/ARCHITECTURE.md"
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

ARCHITECTURE = """# Architecture

The service is three processes: an API, a worker and a scheduler.

## Storage

Each process owns its own schema. Nothing reads another process's tables.

## Messaging

Processes talk over a queue, never through shared memory.
"""


def _repo(slug: str = DOCS) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    docs = root / DOCS
    (docs / "docs").mkdir(parents=True)
    (docs / "notes").mkdir()
    (docs / "src").mkdir()
    (docs / DOC).write_text(ARCHITECTURE, encoding="utf-8")
    (docs / "docs" / "private.md").write_text("# Private\n\nNot for the index.\n", encoding="utf-8")
    (docs / ".repoatlasignore").write_text("docs/private.md\n", encoding="utf-8")
    # The document is quiet; everything around it talks about it. Each of these
    # collects an exact-content vote and a lexical vote for the path, which is
    # exactly what used to bury the file the user named.
    (docs / "README.md").write_text(
        "".join(f"Read {DOC} before changing section {n}.\n" for n in range(40)),
        encoding="utf-8",
    )
    for n in range(14):
        (docs / "notes" / f"note-{n:02d}.md").write_text(
            f"# Note {n}\n\nThe layout is described in {DOC}; see {DOC} again.\n",
            encoding="utf-8",
        )
    (docs / "src" / "app.ts").write_text(
        f"// Structure: {DOC}\nexport function start(): void {{}}\n", encoding="utf-8"
    )
    other = root / OTHER / "docs"
    other.mkdir(parents=True)
    (other / "ARCHITECTURE.md").write_text("# Other architecture\n\nUnrelated.\n", encoding="utf-8")
    init_git_repository(root / DOCS)
    init_git_repository(root / OTHER)
    return root


@pytest.fixture
def indexed(workspace: Workspace, docs_root: Path, tmp_path: Path) -> Iterator[Workspace]:
    initialize(workspace, docs_root, config_path=tmp_path / "config.yaml")
    yield workspace


# ---------------------------------------------------------------------------
# Query analysis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "docs/ARCHITECTURE.md",
        "README.md",
        "docs/guide.mdx",
        "docs/index.rst",
        "notes/todo.txt",
        "docs/intro.markdown",
    ],
)
def test_a_documentation_path_is_read_as_a_path(path: str) -> None:
    analysis = analyze(f"what does {path} say about storage?")
    assert path in analysis.paths
    # A path is a definite term: its absence from the index is an answer.
    assert path in analysis.definite_terms


def test_every_extension_discovery_detects_is_recognised_as_a_path() -> None:
    """Discovery and query analysis must agree on what a source file looks like."""
    for extension in _EXTENSIONS:
        path = f"src/module{extension}"
        assert path in analyze(f"read {path}").paths, extension


def test_a_dashed_document_name_is_one_path_not_a_package() -> None:
    analysis = analyze("see docs/authorization-routes.md for the list")
    assert analysis.paths == ("docs/authorization-routes.md",)
    assert "authorization-routes" not in analysis.ambiguous_packages


def test_prose_with_a_version_or_abbreviation_is_not_a_path() -> None:
    assert analyze("did v1.2 change e.g. the timeout?").paths == ()


# ---------------------------------------------------------------------------
# Search and inspection through the capability
# ---------------------------------------------------------------------------


def test_a_named_document_is_the_first_result(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    response = service.search(SearchRequest(question=DOC, context=_repo()))

    first = response.evidence[0]
    assert (first.ref.repository_slug, first.ref.path) == (DOCS, DOC)
    assert first.match is MatchClass.EXACT_PATH
    assert first.tier is EvidenceTier.DETERMINISTIC
    # File identity: a claim about the file, never about a line.
    assert first.ref.line_start is None and first.text == ""
    assert first.declaration is False


def test_a_named_document_is_found_inside_a_question(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    response = service.search(
        SearchRequest(question=f"What does {DOC} say about storage?", context=_repo())
    )
    assert (response.evidence[0].ref.path, response.evidence[0].match) == (
        DOC,
        MatchClass.EXACT_PATH,
    )


def test_a_found_document_is_inspectable_by_path_and_by_its_handle(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    found = service.search(SearchRequest(question=DOC, context=_repo()))
    handle = found.evidence[0].entity
    assert handle is not None

    by_path = service.inspect(
        InspectRequest(
            context=_repo(),
            target=InspectTarget(path=DOC),
            investigation_id=found.investigation_id,
        )
    )
    by_key = service.inspect(
        InspectRequest(
            context=_repo(),
            target=InspectTarget(entity_key=handle.key),
            investigation_id=found.investigation_id,
        )
    )
    for inspected in (by_path, by_key):
        text = "".join(segment.text for segment in inspected.segments)
        assert "Nothing reads another process's tables." in text
        assert all(segment.ref.line_start is not None for segment in inspected.segments)


def test_an_edited_document_is_reread_and_its_old_evidence_goes_stale(
    indexed: Workspace, docs_root: Path
) -> None:
    service = CapabilityService(indexed)
    inspected = service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=DOC)))
    old = inspected.segments[0].id

    (docs_root / DOCS / DOC).write_text(
        ARCHITECTURE + "\n## Caching\n\nNothing is cached.\n", encoding="utf-8"
    )
    service.sync(SyncRequest(context=GLOBAL))

    ledger = service.sessions._sessions[inspected.investigation_id].ledger
    entry = ledger.get(old)
    assert entry is not None and entry.status is EvidenceStatus.STALE
    reread = service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=DOC)))
    assert "Nothing is cached." in "".join(segment.text for segment in reread.segments)


def test_a_deleted_document_is_neither_found_nor_inspectable(
    indexed: Workspace, docs_root: Path
) -> None:
    service = CapabilityService(indexed)
    inspected = service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=DOC)))
    old = inspected.segments[0].id

    (docs_root / DOCS / DOC).unlink()
    service.sync(SyncRequest(context=GLOBAL))

    ledger = service.sessions._sessions[inspected.investigation_id].ledger
    entry = ledger.get(old)
    assert entry is not None and entry.status is EvidenceStatus.INVALID
    response = service.search(SearchRequest(question=DOC, context=_repo()))
    assert all(item.ref.path != DOC for item in response.evidence)
    with pytest.raises(FileNotIndexedError):
        service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=DOC)))


def test_a_repository_scope_names_only_its_own_document(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    scoped = service.search(SearchRequest(question=DOC, context=_repo()))
    assert {item.ref.repository_slug for item in scoped.evidence} == {DOCS}

    everywhere = service.search(SearchRequest(question=DOC, context=GLOBAL))
    named = {
        item.ref.repository_slug
        for item in everywhere.evidence
        if item.ref.path == DOC and item.match is MatchClass.EXACT_PATH
    }
    assert named == {DOCS, OTHER}


def test_an_ignored_document_stays_unfindable(indexed: Workspace) -> None:
    service = CapabilityService(indexed)
    response = service.search(SearchRequest(question="docs/private.md", context=_repo()))
    # The ignore file itself mentions the path, so the search is answerable; what
    # must never appear is the excluded document or a byte of its text.
    assert all(item.ref.path != "docs/private.md" for item in response.evidence)
    assert all("Not for the index." not in item.text for item in response.evidence)
    with pytest.raises(FileNotIndexedError):
        service.inspect(
            InspectRequest(context=_repo(), target=InspectTarget(path="docs/private.md"))
        )
