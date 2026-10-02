"""The inspect capability: what it discloses and what it refuses (V2 Slice E).

Progressive disclosure over trusted indexed evidence. The tests here are about
the *contract* — which targets resolve, which ranges they imply, and what
inspection is categorically not allowed to do. Security lives in
``test_inspect_security``, coverage arithmetic in ``test_inspect_coverage`` and
evidence identity in ``test_inspect_ledger``.

The property every test in this file circles is the one that separates inspection
from reading a file: **nothing is searched.** A path resolves to a ``files`` row
or to nothing, an entity key resolves exactly or to nothing, and an evidence id
resolves inside its own investigation or to nothing. There is no ranking here
because there is no query.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import (
    EntityAmbiguousError,
    EntityNotFoundError,
    EntityNotInspectableError,
    EvidenceNotFoundError,
    FileNotIndexedError,
    InvestigationRequiredError,
    TargetOutOfScopeError,
)
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.errors import ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceTier, MatchClass
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.models.inspection import (
    DEFAULT_CONTEXT_LINES,
    InspectionSource,
    InspectRequest,
    InspectTarget,
    InspectTargetKind,
)
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import EntityIdentity

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
CONFIG = "src/config.py"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _repo(slug: str = SERVICE) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _symbol_key(workspace: Workspace, needle: str = "") -> str:
    rows = workspace.database.execute(
        "SELECT key FROM entities WHERE key LIKE 'sym:%' ORDER BY key"
    ).fetchall()
    keys = [row["key"] for row in rows]
    assert keys, "the fixture corpus holds no symbol entities"
    return next((key for key in keys if needle in key), keys[0])


# ---------------------------------------------------------------------------
# Target resolution: path
# ---------------------------------------------------------------------------


def test_a_named_path_returns_its_indexed_source(initialized: Workspace) -> None:
    """The plain case, and the shape everything else is measured against."""
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )

    assert response.file.repository_slug == SERVICE
    assert response.file.path == CONFIG
    assert response.segments
    assert response.coverage.requested is not None
    assert "".join(segment.text for segment in response.segments)


def test_a_bare_path_in_global_scope_is_refused_rather_than_searched_for(
    initialized: Workspace,
) -> None:
    """``src/config.py`` may exist in several repositories at once.

    Picking one would be a path search whose winner is decided by iteration order,
    presented with the confidence of an exact lookup. Refusing names the ambiguity
    and hands back the candidates, which is what V1 does with an ambiguous
    repository selector.
    """
    with pytest.raises(ContextResolutionError) as raised:
        _service(initialized).inspect(
            InspectRequest(context=GLOBAL, target=InspectTarget(path=CONFIG))
        )
    assert raised.value.candidates


def test_a_path_is_resolved_only_inside_the_named_repository(initialized: Workspace) -> None:
    """Scope is a wall for a path target, not a preference."""
    with pytest.raises(FileNotIndexedError):
        _service(initialized).inspect(
            InspectRequest(context=_repo("ts-service"), target=InspectTarget(path=CONFIG))
        )


def test_a_path_is_never_matched_by_basename_or_suffix(initialized: Workspace) -> None:
    """Suffix and basename matching belong to search. Inspection is exact."""
    service = _service(initialized)
    for spelling in ("config.py", "onfig.py", "src/conf.py", "SRC/CONFIG.PY"):
        with pytest.raises(FileNotIndexedError):
            service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=spelling)))


def test_redundant_path_spellings_reach_the_same_row(initialized: Workspace) -> None:
    """``./src/config.py`` names the file ``src/config.py`` names.

    Normalizing separators and dropping ``.`` segments is not fuzzy matching: it
    is the same path written another way, and every spelling here has exactly one
    possible target.
    """
    service = _service(initialized)
    canonical = service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)))
    for spelling in ("./src/config.py", "src//config.py", "src\\config.py"):
        other = service.inspect(
            InspectRequest(context=_repo(), target=InspectTarget(path=spelling))
        )
        assert other.file.path == canonical.file.path


# ---------------------------------------------------------------------------
# Target resolution: entity key
# ---------------------------------------------------------------------------


def test_an_exact_symbol_key_inspects_its_own_source(initialized: Workspace) -> None:
    key = _symbol_key(initialized)
    response = _service(initialized).inspect(
        InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=key))
    )

    assert response.target.kind is InspectTargetKind.ENTITY
    assert response.file.path
    assert all(segment.match is MatchClass.EXACT_NAME for segment in response.segments)


def test_a_key_that_does_not_resolve_yields_nothing_rather_than_something_similar(
    initialized: Workspace,
) -> None:
    """The whole value of a natural key is that it is exact.

    A ``sym:`` key embeds the line its definition sat on, so it stops resolving
    when the definition moves. Falling back to a name search would answer about
    whichever definition currently carries that name — confidently, and about
    something the caller never named.
    """
    key = _symbol_key(initialized)
    moved = key.rsplit(":", 1)[0] + ":9999"
    service = _service(initialized)
    with pytest.raises(EntityNotFoundError):
        service.inspect(InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=moved)))

    name = key.rsplit(":", 2)[1]
    with pytest.raises(EntityNotFoundError):
        service.inspect(InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=name)))


def test_an_interned_entity_is_refused_rather_than_attributed_to_a_file(
    initialized: Workspace,
) -> None:
    """``env:SERVICE_HOST`` is referenced by files and defined in none of them.

    Choosing one of its referencing files as "the source" would hand back a
    citation implying that file defines the variable. Several files read it; none
    owns it, and RepoAtlas has no basis to pick.
    """
    with pytest.raises(EntityNotInspectableError):
        _service(initialized).inspect(
            InspectRequest(context=GLOBAL, target=InspectTarget(entity_key="env:SERVICE_HOST"))
        )


def test_entity_keys_are_never_resolved_by_name() -> None:
    """Exact identity only, asserted as an absence in the source."""
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    assert "find_entities_by_name" not in source
    assert "get_entities_by_key" in source


def test_an_ambiguous_key_returns_its_candidates_rather_than_one_of_them(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key uniqueness is enforced by a partial index, so a collision is possible.

    Constructed rather than waited for. What matters is that RepoAtlas refuses and
    says what it saw, instead of resolving with the same confidence it would have
    had if the key had been unique.
    """
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    key = _symbol_key(initialized)
    real = SqliteKnowledgeStore.get_entities_by_key

    def doubled(self: SqliteKnowledgeStore, keys: Sequence[str]) -> list[EntityIdentity]:
        found = real(self, keys)
        return list(found) * 2 if found else []

    monkeypatch.setattr(SqliteKnowledgeStore, "get_entities_by_key", doubled)
    with pytest.raises(EntityAmbiguousError) as raised:
        _service(initialized).inspect(
            InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=key))
        )
    assert raised.value.candidates


def test_an_entity_outside_the_requested_scope_is_refused(initialized: Workspace) -> None:
    key = _symbol_key(initialized, "common-lib")
    with pytest.raises(TargetOutOfScopeError):
        _service(initialized).inspect(
            InspectRequest(context=_repo(SERVICE), target=InspectTarget(entity_key=key))
        )


# ---------------------------------------------------------------------------
# Target resolution: evidence id
# ---------------------------------------------------------------------------


def test_an_evidence_target_without_its_investigation_is_refused(initialized: Workspace) -> None:
    """``E7`` means nothing on its own, and two investigations hold different ones."""
    with pytest.raises(InvestigationRequiredError):
        _service(initialized).inspect(
            InspectRequest(context=GLOBAL, target=InspectTarget(evidence_id="E1"))
        )


def test_an_evidence_id_is_never_looked_for_in_another_investigation(
    initialized: Workspace,
) -> None:
    """One investigation's evidence cannot be reached from another's id.

    Both ledgers hold an ``E1``. Naming the second investigation must not find the
    first one's, because a global search for an evidence id would let a caller
    read evidence from a task it never ran.
    """
    service = _service(initialized)
    first = service.search(SearchRequest(question="SERVICE_HOST", context=GLOBAL))
    second = service.search(SearchRequest(question="nothing-matches-this-token", context=GLOBAL))
    assert first.investigation_id != second.investigation_id
    assert first.evidence

    with pytest.raises(EvidenceNotFoundError):
        service.inspect(
            InspectRequest(
                context=GLOBAL,
                target=InspectTarget(evidence_id=first.evidence[0].id),
                investigation_id=second.investigation_id,
            )
        )


def test_an_unknown_investigation_is_refused_rather_than_created(initialized: Workspace) -> None:
    with pytest.raises(UnknownInvestigationError):
        _service(initialized).inspect(
            InspectRequest(
                context=_repo(),
                target=InspectTarget(path=CONFIG),
                investigation_id="not-an-id-this-server-issued",
            )
        )


def test_inspecting_evidence_widens_around_the_coordinates_it_named(
    initialized: Workspace,
) -> None:
    """CASE 1. The evidence's span, plus context, and only proven text."""
    service = _service(initialized)
    found = service.search(SearchRequest(question="SERVICE_HOST", context=GLOBAL))
    cited = next(item for item in found.evidence if item.ref.line_start is not None)

    response = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=cited.id),
            investigation_id=found.investigation_id,
            context_lines=4,
        )
    )

    assert response.investigation_id == found.investigation_id
    assert response.file.path == cited.ref.path
    assert response.coverage.requested is not None
    assert cited.ref.line_start is not None
    assert response.coverage.requested.start <= cited.ref.line_start
    # Every returned line is inside what was asked for, and every returned span is
    # exactly as wide as the text behind it.
    for segment in response.segments:
        assert segment.ref.line_start is not None and segment.ref.line_end is not None
        assert len(segment.text.split("\n")) == segment.ref.line_end - segment.ref.line_start + 1


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


def test_an_explicit_range_chooses_the_lines_and_the_target_chooses_the_file(
    initialized: Workspace,
) -> None:
    key = _symbol_key(initialized)
    entity = _service(initialized).inspect(
        InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=key), context_lines=0)
    )
    pinned = _service(initialized).inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(entity_key=key),
            line_start=1,
            line_end=1,
            context_lines=0,
        )
    )
    assert pinned.file.path == entity.file.path
    assert pinned.coverage.requested is not None
    assert (pinned.coverage.requested.start, pinned.coverage.requested.end) == (1, 1)


def test_a_lone_line_start_names_one_line(initialized: Workspace) -> None:
    response = _service(initialized).inspect(
        InspectRequest(
            context=_repo(), target=InspectTarget(path=CONFIG), line_start=5, context_lines=0
        )
    )
    assert response.coverage.requested is not None
    assert (response.coverage.requested.start, response.coverage.requested.end) == (5, 5)


def test_context_lines_widen_the_range_on_both_sides(initialized: Workspace) -> None:
    response = _service(initialized).inspect(
        InspectRequest(
            context=_repo(),
            target=InspectTarget(path=CONFIG),
            line_start=10,
            line_end=10,
            context_lines=3,
        )
    )
    assert response.coverage.requested is not None
    assert (response.coverage.requested.start, response.coverage.requested.end) == (7, 13)


def test_context_never_reaches_below_line_one_or_past_a_known_last_line(
    initialized: Workspace,
) -> None:
    """Clamped only where the bound is provable, which a verified read makes it."""
    response = _service(initialized).inspect(
        InspectRequest(
            context=_repo(),
            target=InspectTarget(path=CONFIG),
            line_start=1,
            line_end=1,
            context_lines=200,
        )
    )
    assert response.coverage.source is InspectionSource.VERIFIED_WORKTREE
    assert response.file.line_count is not None
    assert response.coverage.requested is not None
    assert response.coverage.requested.start == 1
    assert response.coverage.requested.end == response.file.line_count


def test_a_path_with_no_range_asks_for_the_whole_indexed_file(initialized: Workspace) -> None:
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )
    assert response.coverage.requested is not None
    assert response.coverage.requested.start == 1
    assert response.file.line_count == response.coverage.requested.end


def test_the_default_context_is_the_documented_one() -> None:
    """A default that drifts silently changes every response that relied on it."""
    assert DEFAULT_CONTEXT_LINES == 20
    assert (
        InspectRequest(context=GLOBAL, target=InspectTarget(path="a.py")).context_lines
        == DEFAULT_CONTEXT_LINES
    )


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_exactly_one_target_form_is_accepted() -> None:
    with pytest.raises(ValueError):
        InspectTarget()
    with pytest.raises(ValueError):
        InspectTarget(path="a.py", entity_key="sym:x")
    with pytest.raises(ValueError):
        InspectTarget(evidence_id="E1", path="a.py")
    assert InspectTarget(path="a.py").kind is InspectTargetKind.PATH
    assert InspectTarget(entity_key="env:X").kind is InspectTargetKind.ENTITY
    assert InspectTarget(evidence_id="E1").kind is InspectTargetKind.EVIDENCE


def test_a_range_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError):
        InspectRequest(context=GLOBAL, target=InspectTarget(path="a.py"), line_start=9, line_end=4)


def test_a_line_end_without_a_line_start_names_no_range() -> None:
    with pytest.raises(ValueError):
        InspectRequest(context=GLOBAL, target=InspectTarget(path="a.py"), line_end=4)


def test_context_lines_are_bounded_by_the_server() -> None:
    """A bound rather than a policy: context multiplies by two sides and by gaps."""
    with pytest.raises(ValueError):
        InspectRequest(context=GLOBAL, target=InspectTarget(path="a.py"), context_lines=100_000)
    with pytest.raises(ValueError):
        InspectRequest(context=GLOBAL, target=InspectTarget(path="a.py"), context_lines=-1)


# ---------------------------------------------------------------------------
# Inspection is not retrieval
# ---------------------------------------------------------------------------


def test_inspection_runs_no_retrieval_stage(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every target resolves exactly, so nothing here has a query to run.

    Asserted by making the funnel explode. A capability that reached for search to
    turn a path into a file would be a path *search* with one result, and it would
    answer where an exact lookup should have refused.
    """
    from repoatlas.retrieval.retriever import Retriever
    from repoatlas.storage.sqlite.search import SqliteSearchIndex

    def explode(*_: object, **__: object) -> object:
        raise AssertionError("inspection reached retrieval")

    monkeypatch.setattr(Retriever, "retrieve", explode)
    monkeypatch.setattr(Retriever, "retrieve_detailed", explode)
    monkeypatch.setattr(SqliteSearchIndex, "search_text", explode)
    monkeypatch.setattr(SqliteSearchIndex, "find_literal", explode)
    monkeypatch.setattr(SqliteSearchIndex, "find_files_by_path", explode)
    monkeypatch.setattr(SqliteSearchIndex, "find_entities_by_name", explode)
    monkeypatch.setattr(SqliteSearchIndex, "search_entities", explode)

    service = _service(initialized)
    service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)))
    service.inspect(
        InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=_symbol_key(initialized)))
    )
    with pytest.raises(FileNotIndexedError):
        service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path="nope.py")))


def test_segments_are_ranked_by_source_order_alone(initialized: Workspace) -> None:
    """``rank`` is an ordinal, not a score. There is nothing here to score."""
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG), allow_worktree=False)
    )
    assert [segment.rank for segment in response.segments] == list(
        range(1, len(response.segments) + 1)
    )
    starts = [segment.ref.line_start for segment in response.segments]
    assert starts == sorted(s for s in starts if s is not None)


def test_inspected_evidence_is_deterministic(initialized: Workspace) -> None:
    """Addressed content is a fact about the repository, never a guess."""
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )
    assert response.segments
    assert all(segment.tier is EvidenceTier.DETERMINISTIC for segment in response.segments)


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_stored_freshness_invokes_no_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading indexed source should not cost a git process per repository."""
    from repoatlas.gitstate.client import GitClient, GitRepositoryState

    def forbidden(self: GitClient, repo_path: Path) -> GitRepositoryState:
        raise AssertionError("inspection invoked git at stored depth")

    monkeypatch.setattr(GitClient, "state", forbidden)
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )
    assert response.freshness.depth is FreshnessDepth.STORED
    assert all(repo.head_matches is None for repo in response.freshness.repositories)


def test_a_stale_index_is_labelled_rather_than_refused(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The agent decides what a stale index is worth. Inspection does not refuse it."""
    edited = workspace_root / SERVICE / CONFIG
    edited.write_text(edited.read_text() + "\n# edited, never synced\n", encoding="utf-8")

    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )
    assert response.segments
    assert response.coverage.worktree_matches_index is False


def test_inspection_never_syncs_the_index_it_reads(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid(name: str):
        def spy(*_: object, **__: object) -> object:
            raise AssertionError(f"inspection called {name}")

        return spy

    monkeypatch.setattr(Workspace, "writer_lock", forbid("writer_lock"))
    monkeypatch.setattr(Workspace, "sync_engine", forbid("sync_engine"))
    monkeypatch.setattr(initialized.repositories, "upsert", forbid("repositories.upsert"))
    monkeypatch.setattr(initialized.files, "upsert", forbid("files.upsert"))
    monkeypatch.setattr(initialized.files, "delete_many", forbid("files.delete_many"))
    monkeypatch.setattr(initialized.knowledge, "add_edges", forbid("knowledge.add_edges"))
    monkeypatch.setattr(
        initialized.knowledge, "replace_file_facts", forbid("knowledge.replace_file_facts")
    )
    monkeypatch.setattr(initialized.knowledge, "intern_entity", forbid("knowledge.intern_entity"))

    service = _service(initialized)
    service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)))
    service.inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG), allow_worktree=False)
    )


def test_the_indexed_facts_are_unchanged_by_inspecting(initialized: Workspace) -> None:
    """A logical snapshot of every fact table, not a file mtime."""
    from tests.conftest import fact_snapshot

    service = _service(initialized)
    before = fact_snapshot(initialized)

    service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)))
    service.inspect(
        InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=_symbol_key(initialized)))
    )
    service.inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG), allow_worktree=False)
    )

    assert fact_snapshot(initialized) == before


# ---------------------------------------------------------------------------
# Model hygiene
# ---------------------------------------------------------------------------


def _keys_anywhere(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= _keys_anywhere(item)
    elif isinstance(value, list):
        for item in value:
            found |= _keys_anywhere(item)
    return found


def test_no_storage_row_id_or_host_path_reaches_the_response(initialized: Workspace) -> None:
    """Row ids are allocation order; a reindex renumbers them and a stored one lies."""
    service = _service(initialized)
    for request in (
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)),
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG), allow_worktree=False),
        InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=_symbol_key(initialized))),
    ):
        payload = json.loads(service.inspect(request).model_dump_json())
        keys = _keys_anywhere(payload)
        for banned in ("file_id", "entity_id", "chunk_id", "repo_id", "rowid", "id_", "score"):
            assert banned not in keys, banned
        assert str(initialized.root) not in json.dumps(payload)


def test_the_response_carries_no_bare_content_field(initialized: Workspace) -> None:
    """Everything citable travels as evidence, so everything citable is ledgered.

    A raw ``content`` string beside the segments would be text an agent can read
    and quote with no evidence id behind it — which is exactly the shape of an
    uncheckable citation.
    """
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )
    assert "content" not in type(response).model_fields
    assert "text" not in type(response).model_fields
