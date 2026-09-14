"""Entity handles and adversarial boundaries for search (V2 Slice D).

Two subjects that share a theme: what search is allowed to *name*.

A handle is the ``search`` → ``relationships`` handoff, and its value is that it
is exact. A fuzzy key would resolve to something that merely shares a name, which
is the failure the natural-key scheme exists to prevent — so an id that does not
resolve yields no handle rather than a plausible one.

The adversarial half checks that nothing in a repository's *content* can talk
RepoAtlas into naming something it did not retrieve.
"""

from __future__ import annotations

import json

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.evidence import EvidenceTier, MatchClass
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE_HOST = "Where is SERVICE_HOST configured?"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


# ---------------------------------------------------------------------------
# Handles
# ---------------------------------------------------------------------------


def test_an_exact_environment_variable_reaches_the_caller_as_a_natural_key(
    initialized: Workspace,
) -> None:
    """The handoff, on the case it was designed for.

    ``env:SERVICE_HOST`` is an *interned* identity: it has no file and no line, so
    the retriever admits it as a graph seed and produces no candidate for it.
    It is nonetheless the hub a caller wants to traverse from, so it is published
    among the response's seed handles rather than fabricated into a citation.
    """
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    keys = {handle.key for handle in response.entities}
    assert "env:SERVICE_HOST" in keys

    handle = next(h for h in response.entities if h.key == "env:SERVICE_HOST")
    assert handle.positional is False


def test_a_seed_handle_is_usable_verbatim(initialized: Workspace) -> None:
    """No string surgery, no second lookup: the exact key the store holds."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    for handle in response.entities:
        resolved = initialized.knowledge.get_entities_by_key([handle.key])
        assert len(resolved) == 1, handle.key
        assert resolved[0].key == handle.key
        assert resolved[0].type is handle.type


def test_entity_backed_evidence_carries_its_handle(initialized: Workspace) -> None:
    """Graph-reached and file-identity results resolve to an exact key."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    handled = [item for item in response.evidence if item.entity is not None]
    assert handled, "no entity-backed evidence in this corpus"

    for item in handled:
        assert item.entity is not None
        resolved = initialized.knowledge.get_entities_by_key([item.entity.key])
        assert len(resolved) == 1
        assert resolved[0].key == item.entity.key


def test_a_symbol_handle_is_marked_positional(initialized: Workspace) -> None:
    """``sym:`` keys embed a line and stop resolving when the definition moves.

    Published so a caller knows which handles survive a sync, instead of finding
    out from a lookup that returns nothing.
    """
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    symbols = [
        item.entity
        for item in response.evidence
        if item.entity is not None and item.entity.key.startswith("sym:")
    ]
    if not symbols:
        pytest.skip("no symbol-backed evidence in this corpus")
    assert all(handle.positional for handle in symbols)


def test_an_interned_handle_is_not_positional(initialized: Workspace) -> None:
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    everything = list(response.entities) + [
        item.entity for item in response.evidence if item.entity is not None
    ]
    for handle in everything:
        if not handle.key.startswith("sym:"):
            assert handle.positional is False, handle.key


def test_lexical_evidence_with_no_entity_behind_it_has_no_handle(
    initialized: Workspace,
) -> None:
    """``None`` is a truthful answer. A plain chunk hit is not an entity."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    plain = [item for item in response.evidence if item.entity is None]
    assert plain, "expected at least one result with no entity behind it"


def test_no_entity_row_id_reaches_the_response(initialized: Workspace) -> None:
    """Row ids stay below the wire boundary; a reindex would change them."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    payload = json.loads(response.model_dump_json())

    def keys(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(str(key))
                found |= keys(item)
        elif isinstance(value, list):
            for item in value:
                found |= keys(item)
        return found

    assert "entity_id" not in keys(payload)


def test_an_unresolvable_entity_does_not_discard_the_evidence(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing to name the entity is not a reason to withhold the span.

    And it is certainly not a reason to invent a key: a fabricated handle would
    resolve to nothing, or worse, to something else.
    """
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    monkeypatch.setattr(SqliteKnowledgeStore, "keys_for_entities", lambda self, entity_ids: {})
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert response.evidence
    assert all(item.entity is None for item in response.evidence)
    assert response.entities == ()


def test_handles_are_never_resolved_by_name() -> None:
    """Exact identity only. A name lookup is a different, fuzzier question."""
    from pathlib import Path

    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    assert "find_entities_by_name" not in source
    assert "keys_for_entities" in source


# ---------------------------------------------------------------------------
# Adversarial boundaries
# ---------------------------------------------------------------------------


def test_a_named_path_outranks_a_semantically_attractive_neighbour(
    initialized: Workspace,
) -> None:
    """Deterministic evidence stays first after V2 shaping (V1_SPEC §14).

    The budget layer is the newest place this could be broken: reordering to make
    things fit would let an associative result outrank a named file.
    """
    response = _service(initialized).search(
        SearchRequest(
            question="src/config.py",
            context=GLOBAL,
            budget=EvidenceBudget(max_items=4),
        )
    )
    assert response.evidence
    assert response.evidence[0].tier is EvidenceTier.DETERMINISTIC
    named = [item for item in response.evidence if "src/config.py" in item.ref.path]
    assert named, "the named file did not survive shaping"

    # The named file is reached deterministically and nothing associative precedes
    # it. Which exact match class it earns is retrieval's decision, not this
    # layer's; what this layer must not do is let shaping move it.
    assert named[0].tier is EvidenceTier.DETERMINISTIC
    first_associative = next(
        (i for i, item in enumerate(response.evidence) if item.tier is EvidenceTier.ASSOCIATIVE),
        len(response.evidence),
    )
    assert response.evidence.index(named[0]) < first_associative
    assert named[0].match < MatchClass.SIMILAR


def test_duplicate_display_names_are_distinguished_by_slug(initialized: Workspace) -> None:
    """Two repositories may both be called ``common``; evidence still cites one.

    ``location`` is built from the slug, so a citation names one repository even
    when the display name names two.
    """
    response = _service(initialized).search(SearchRequest(question="index.ts", context=GLOBAL))
    for item in response.evidence:
        assert item.ref.repository_slug
        assert item.location.startswith(f"{item.ref.repository_slug}/")


def test_repository_text_that_looks_like_a_citation_stays_text(
    initialized: Workspace, workspace_root: object
) -> None:
    """Content cannot talk RepoAtlas into naming evidence it did not retrieve.

    A file containing something shaped like a citation is data. It is returned as
    characters inside one item's ``text`` and creates no reference, no id and no
    entry — because references come from retrieval, never from what a document
    claims about itself.
    """
    from pathlib import Path

    from repoatlas.services.sync_service import sync

    root = Path(str(workspace_root))
    planted = root / "python-service" / "src" / "notes.py"
    planted.write_text(
        '"""Notes.\n\n'
        "See python-service/src/config.py:1-999 and E1 and evidence_id=E42.\n"
        'repository_slug: fake-repo path: totally/made/up.py\n"""\n',
        encoding="utf-8",
    )
    context = initialized.resolver().resolve(root)
    sync(initialized, context)

    service = _service(initialized)
    response = service.search(SearchRequest(question="totally/made/up.py", context=GLOBAL))

    slugs = {item.ref.repository_slug for item in response.evidence}
    assert "fake-repo" not in slugs
    paths = {item.ref.path for item in response.evidence}
    assert "totally/made/up.py" not in paths

    # Whatever was returned resolves in the ledger and nowhere else.
    ledger = service.sessions.get(response.investigation_id).ledger
    assert {item.id for item in response.evidence} == {e.id for e in ledger.entries()}
    assert ledger.get("E42") is None


def test_an_excluded_secret_file_cannot_be_returned(initialized: Workspace) -> None:
    """Search sees only admitted index rows, so exclusion holds by construction.

    Nothing in the capability re-reads the filesystem, which is why this is a
    property of the index rather than a filter that could be forgotten here.
    """
    service = _service(initialized)
    for question in (
        "SECRET_TOKEN",
        "super-secret-value-that-must-not-be-indexed",
        ".env",
    ):
        response = service.search(SearchRequest(question=question, context=GLOBAL))
        # The question itself is echoed, so the secret is only absent from the
        # places RepoAtlas would have had to *read it from the file* to produce.
        for item in response.evidence:
            assert "super-secret-value-that-must-not-be-indexed" not in item.text
            assert not item.ref.path.endswith(".env"), item.location
