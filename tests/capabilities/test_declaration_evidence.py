"""Declared-entity evidence is a claim about a file, with an identity of its own.

Two defects, one cause. Search supplies an entity it names by the declaration the
indexer recorded for it --- ``def load_config()``, ``class ServiceConfig``, a
configuration key's dotted path --- and that text was treated as though it were
the bytes of a source span.

**A false line claim.** A one-line entity with a one-line declaration passed the
line arithmetic that proves what a chunk covers: ``service.host`` is one line of
text and ``pillar.sls:2`` is one line of source, so the evidence said line 2
*reads* ``service.host``. It reads ``host: service.internal.example``, and
grounding accepted a line citation resting on the declaration anyway. Every
one-line declaration in the fixture ecosystem was affected.

**Evidence superseded on arrival.** A multi-line declaration already failed the
arithmetic and was supplied as a claim about its file --- correctly --- but every
such claim in one file shared one ledger span. Two declarations from one file were
then two versions of the same bytes, and the second superseded the first:
``search("ServiceConfig load_config")`` returned its rank-1 exact answer already
``superseded``, and a citation to it could never ground.

The repair: a declaration never offers a line range (the arithmetic itself is
unchanged), and the ledger identifies a declaration by the entity it declares as
well as its span. Text read from the file --- and every rule about when different
bytes supersede it --- is keyed exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.evidence_shaping import provable_span
from repoatlas.capabilities.ledger import (
    EvidenceLedger,
    LedgerCapacityError,
    evidence_identity,
)
from repoatlas.capabilities.search import offered_lines, provable_line_span
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import EvidenceItem
from repoatlas.models.evidence import EvidenceCandidate, EvidenceTier, MatchClass
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceRef, EvidenceStatus, ProposedEvidence
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFindingCode,
    GroundingVerdict,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: Two declarations in one file, both named by the question. The reported case.
CONFIG = ("python-service", "src/config.py")
BOTH_IN_CONFIG = "ServiceConfig load_config"
#: Two one-line configuration keys in one file.
PILLAR = ("deployment", "service/pillar.sls")
BOTH_KEYS = "service.host service.port"


def _search(
    service: CapabilityService, question: str, context: CapabilityContext = GLOBAL
) -> SearchResponse:
    return service.search(SearchRequest(question=question, context=context))


def _key(workspace: Workspace, where: tuple[str, str], declaration: str) -> str:
    """The natural key of the entity declared as ``declaration`` in one file.

    Looked up rather than spelled out: a ``sym:`` key embeds the line its
    definition sits on, and a test that hard-coded one would fail the next time a
    fixture gained a line.
    """
    slug, path = where
    row = workspace.database.execute(
        "SELECT e.key FROM entities e "
        "JOIN files f ON f.id = e.file_id JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.slug = ? AND f.rel_path = ? AND e.signature = ? ORDER BY e.key",
        (slug, path, declaration),
    ).fetchone()
    assert row is not None, (slug, path, declaration)
    return str(row["key"])


def _declaration_of(response: SearchResponse, key: str) -> EvidenceItem:
    supplied = [
        item
        for item in response.evidence
        if item.declaration and item.entity is not None and item.entity.key == key
    ]
    assert supplied, f"{key} was not supplied: {[i.location for i in response.evidence]}"
    return supplied[0]


def _verify(
    service: CapabilityService, investigation: str, citation: AgentCitation
) -> VerifyGroundingResponse:
    return service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=investigation,
            answer=AgentAnswer(
                summary="what the evidence shows",
                claims=(AgentClaim(statement="as cited", citations=(citation,)),),
            ),
        )
    )


def _resync(workspace: Workspace, root: Path) -> None:
    from repoatlas.services.sync_service import sync

    sync(workspace, workspace.resolver().resolve(root))


def _one_line_declarations(workspace: Workspace) -> list[tuple[str, str, str, str, int]]:
    """Every entity whose span is one line and whose declaration is one line.

    The population the arithmetic could not protect. Discovered, so each one the
    fixtures hold is covered and a new one is covered without an edit here.
    """
    rows = workspace.database.execute(
        "SELECT e.key, e.signature, r.slug, f.rel_path, e.line_start "
        "FROM entities e "
        "JOIN files f ON f.id = e.file_id JOIN repositories r ON r.id = f.repo_id "
        "WHERE e.signature IS NOT NULL AND e.signature <> '' "
        "AND e.line_start = e.line_end AND instr(e.signature, char(10)) = 0 "
        "ORDER BY e.key"
    ).fetchall()
    return [
        (str(r["key"]), str(r["signature"]), str(r["slug"]), str(r["rel_path"]), r["line_start"])
        for r in rows
    ]


# -- the false line claim ----------------------------------------------------------


def test_the_arithmetic_alone_would_have_licensed_a_false_line(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Why the flag has to be stated: the line count cannot tell the difference.

    Not a test of the repair. It keeps the ones below from passing vacuously, by
    proving the fixtures hold one-line declarations that the arithmetic accepts and
    the file contradicts.
    """
    found = _one_line_declarations(initialized)
    assert found, "the fixtures hold no one-line declaration to test with"
    contradicted = 0
    for _entity, declaration, slug, path, line in found:
        assert provable_span(declaration, line, line) == (line, line)
        source = (workspace_root / slug / path).read_text(encoding="utf-8").splitlines()
        if source[line - 1].rstrip("\r") != declaration:
            contradicted += 1
    assert contradicted, "every one-line declaration matched its source line"


def test_a_one_line_declaration_claims_its_file_and_no_line(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    key = _key(initialized, PILLAR, "service.host")

    item = _declaration_of(_search(service, "service.host"), key)

    assert item.text == "service.host"
    assert item.ref.line_start is None and item.ref.line_end is None
    assert item.location == "/".join(PILLAR)
    # Exact precedence is untouched: it is still the entity the question named.
    assert item.tier is EvidenceTier.DETERMINISTIC
    assert item.match is MatchClass.EXACT_NAME


def test_no_one_line_declaration_is_supplied_with_a_line(initialized: Workspace) -> None:
    """Every one of them, asked for by name."""
    service = CapabilityService(initialized)
    supplied = 0
    for key, declaration, _slug, _path, _line in _one_line_declarations(initialized):
        response = _search(service, declaration)
        for item in response.evidence:
            if item.entity is not None and item.entity.key == key and item.text == declaration:
                supplied += 1
                assert item.declaration, key
                assert item.ref.line_start is None and item.ref.line_end is None, key
    assert supplied, "no one-line declaration was supplied, so nothing was checked"


def test_a_line_citation_cannot_rest_on_declaration_text(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    key = _key(initialized, PILLAR, "service.host")
    response = _search(service, "service.host")
    item = _declaration_of(response, key)
    line = int(key.rsplit(":", 1)[1])

    narrowed = _verify(
        service,
        response.investigation_id,
        AgentCitation(evidence_id=item.id, line_start=line, line_end=line),
    )

    assert narrowed.verdict is not GroundingVerdict.GROUNDED
    assert narrowed.citations[0].grounded is False
    assert narrowed.citations[0].code is GroundingFindingCode.CITATION_WITHOUT_SUPPLIED_LINES

    # What it does establish -- that the entity is declared in that file -- still
    # grounds. It is evidence, just not evidence about a line.
    whole = _verify(service, response.investigation_id, AgentCitation(evidence_id=item.id))
    assert whole.verdict is GroundingVerdict.GROUNDED


def test_a_declaration_is_the_recorded_declaration_and_never_a_body(
    initialized: Workspace,
) -> None:
    """Nothing here expands a method body: the text is exactly what was recorded."""
    service = CapabilityService(initialized)
    key = _key(initialized, CONFIG, "def load_config()")
    stored = initialized.database.execute(
        "SELECT signature FROM entities WHERE key = ?", (key,)
    ).fetchone()

    item = _declaration_of(_search(service, BOTH_IN_CONFIG), key)

    assert item.text == stored["signature"]
    assert "\n" not in item.text


def test_every_line_claim_in_a_response_rests_on_text_read_from_the_file(
    initialized: Workspace,
) -> None:
    """Both directions, across responses that mix declarations and source."""
    service = CapabilityService(initialized)
    for question in (BOTH_IN_CONFIG, BOTH_KEYS, "name version", "Application start main"):
        response = _search(service, question)
        assert any(item.declaration for item in response.evidence), question
        for item in response.evidence:
            if item.declaration:
                assert item.ref.line_start is None, (question, item.location)
            if item.ref.line_start is not None:
                assert not item.declaration, (question, item.location)


# -- declarations are not superseded by their neighbours ---------------------------


def test_the_exact_answer_is_not_superseded_on_arrival(initialized: Workspace) -> None:
    """The reported failure, exactly."""
    service = CapabilityService(initialized)
    key = _key(initialized, CONFIG, "def load_config()")

    response = _search(service, BOTH_IN_CONFIG)
    item = _declaration_of(response, key)

    assert item.status is EvidenceStatus.VALID
    assert item.match is MatchClass.EXACT_NAME
    cited = _verify(service, response.investigation_id, AgentCitation(evidence_id=item.id))
    assert cited.verdict is GroundingVerdict.GROUNDED


def test_two_declarations_in_one_file_are_two_live_ids(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    load = _key(initialized, CONFIG, "def load_config()")
    model = _key(initialized, CONFIG, "class ServiceConfig")

    response = _search(service, BOTH_IN_CONFIG)
    first, second = _declaration_of(response, load), _declaration_of(response, model)

    assert first.id != second.id
    assert first.ref.span_key == second.ref.span_key, "both claim the file and no line"
    assert first.status is EvidenceStatus.VALID
    assert second.status is EvidenceStatus.VALID
    ledger = service.sessions.get(response.investigation_id).ledger
    for evidence_id, key in ((first.id, load), (second.id, model)):
        entry = ledger.get(evidence_id)
        assert entry is not None
        assert entry.status is EvidenceStatus.VALID
        assert entry.superseded_by is None
        assert entry.declares == key


def test_one_line_keys_in_one_file_neither_collide_nor_displace_the_file(
    initialized: Workspace,
) -> None:
    """The case the line-claim repair would have widened, had identity not moved.

    Two keys from one file are now both line-less, and a filename or graph hit for
    the same file is line-less too. Three claims on one file, three live ids.
    """
    service = CapabilityService(initialized)
    host = _key(initialized, PILLAR, "service.host")
    port = _key(initialized, PILLAR, "service.port")

    response = _search(service, BOTH_KEYS)

    on_file = {item.id for item in response.evidence if item.ref.span_key[:2] == PILLAR}
    assert {_declaration_of(response, host).id, _declaration_of(response, port).id} <= on_file
    assert all(item.status is EvidenceStatus.VALID for item in response.evidence)
    assert len({item.id for item in response.evidence}) == len(response.evidence)


def test_a_repository_scoped_search_supplies_only_its_own_declarations(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized)
    scoped = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=PILLAR[0])

    response = _search(service, BOTH_KEYS, scoped)

    declared = [item for item in response.evidence if item.declaration]
    assert declared
    assert {item.ref.repository_slug for item in declared} == {PILLAR[0]}
    assert all(item.status is EvidenceStatus.VALID for item in declared)


# -- inspection supplies the source, and the declaration stays what it was ---------


def test_inspecting_the_declared_entity_supplies_its_real_lines(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    key = _key(initialized, CONFIG, "def load_config()")
    response = _search(service, BOTH_IN_CONFIG)
    declared = _declaration_of(response, key)
    assert declared.entity is not None and declared.entity.key == key

    inspected = service.inspect(
        InspectRequest(
            context=GLOBAL,
            investigation_id=response.investigation_id,
            target=InspectTarget(entity_key=key),
        )
    )

    assert inspected.segments
    source = inspected.segments[0]
    assert source.declaration is False
    assert source.ref.line_start is not None and source.ref.line_end is not None
    assert "def load_config" in source.text
    assert source.id != declared.id

    # The source grounds a line citation; the declaration still does not.
    line = source.ref.line_start
    on_source = _verify(
        service,
        response.investigation_id,
        AgentCitation(evidence_id=source.id, line_start=line, line_end=line),
    )
    assert on_source.verdict is GroundingVerdict.GROUNDED
    on_declaration = _verify(
        service,
        response.investigation_id,
        AgentCitation(evidence_id=declared.id, line_start=line, line_end=line),
    )
    assert on_declaration.citations[0].grounded is False

    # And reading the source did not retire the declaration. They are different
    # claims, and neither is a newer version of the other.
    entry = service.sessions.get(response.investigation_id).ledger.get(declared.id)
    assert entry is not None and entry.status is EvidenceStatus.VALID


# -- freshness still applies ---------------------------------------------------------


def test_a_declaration_goes_stale_when_its_file_changes(
    initialized: Workspace, workspace_root: Path
) -> None:
    """A separate identity is not an exemption from revalidation."""
    service = CapabilityService(initialized)
    key = _key(initialized, CONFIG, "def load_config()")
    response = _search(service, BOTH_IN_CONFIG)
    item = _declaration_of(response, key)
    cited = AgentCitation(evidence_id=item.id)
    assert _verify(service, response.investigation_id, cited).verdict is GroundingVerdict.GROUNDED

    edited = workspace_root.joinpath(*CONFIG)
    edited.write_text("# inserted\n" + edited.read_text(encoding="utf-8"), encoding="utf-8")
    _resync(initialized, workspace_root)

    after = _verify(service, response.investigation_id, cited)
    assert after.verdict is GroundingVerdict.UNGROUNDED
    assert {finding.code for finding in after.findings} == {GroundingFindingCode.STALE_EVIDENCE}
    assert after.evidence_statuses[0].status is EvidenceStatus.STALE


# -- the ledger: a separate identity, and the existing rules around it ---------------


SLUG, PATH = CONFIG
LOAD = f"sym:{SLUG}:{PATH}:load_config:16"
MODEL = f"sym:{SLUG}:{PATH}:ServiceConfig:9"


def _file_claim(digest: str, *, declares: str | None = None) -> ProposedEvidence:
    return ProposedEvidence(
        ref=EvidenceRef(
            repository_slug=SLUG,
            path=PATH,
            span_digest=digest,
            file_content_hash="filehash-1",
            indexed_facts_version="facts-1",
        ),
        repository_name="python-service",
        text_length=len(digest),
        declares=declares,
    )


def _ledger(capacity: int = 500) -> EvidenceLedger:
    return EvidenceLedger("investigation-under-test", capacity=capacity)


def test_source_evidence_is_identified_exactly_as_before() -> None:
    ref = _file_claim("a").ref
    assert evidence_identity(ref, None) == (*ref.span_key, None)


def test_declarations_of_two_entities_in_one_file_are_two_identities() -> None:
    ledger = _ledger()
    first, second = ledger.record(
        [_file_claim("def load_config()", declares=LOAD), _file_claim("class X", declares=MODEL)]
    )
    assert first.id != second.id
    assert first.supersedes is None and second.supersedes is None
    assert all(entry.status is EvidenceStatus.VALID for entry in ledger.entries())


def test_the_same_declaration_again_is_the_same_evidence() -> None:
    ledger = _ledger()
    (first,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])
    (again,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])
    assert again.id == first.id
    assert len(ledger) == 1


def test_a_new_declaration_of_the_same_entity_still_supersedes_the_old() -> None:
    """Genuine replacement: the same entity, declared differently."""
    ledger = _ledger()
    (old,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])
    (new,) = ledger.record([_file_claim("def load_config(path)", declares=LOAD)])

    assert new.supersedes == old.id
    replaced = ledger.get(old.id)
    assert replaced is not None
    assert replaced.status is EvidenceStatus.SUPERSEDED
    assert replaced.superseded_by == new.id


def test_file_level_source_text_still_supersedes_under_the_existing_rule() -> None:
    """Not a global exemption for file-level evidence: source keeps its rule."""
    ledger = _ledger()
    (old,) = ledger.record([_file_claim("piece one")])
    (new,) = ledger.record([_file_claim("piece two")])
    assert new.supersedes == old.id
    replaced = ledger.get(old.id)
    assert replaced is not None and replaced.status is EvidenceStatus.SUPERSEDED


def test_a_declaration_and_source_evidence_for_one_file_do_not_supersede() -> None:
    ledger = _ledger()
    identity, declared = ledger.record(
        [_file_claim(""), _file_claim("def load_config()", declares=LOAD)]
    )
    assert identity.id != declared.id
    assert all(entry.status is EvidenceStatus.VALID for entry in ledger.entries())


def test_each_declaration_occupies_capacity_like_any_evidence() -> None:
    ledger = _ledger(capacity=2)
    ledger.record(
        [_file_claim("def load_config()", declares=LOAD), _file_claim("class X", declares=MODEL)]
    )
    before = ledger.entries()

    with pytest.raises(LedgerCapacityError):
        ledger.record([_file_claim("def other()", declares=f"sym:{SLUG}:{PATH}:other:30")])
    assert ledger.entries() == before

    # Re-reading what is already held allocates nothing, even when full.
    (again,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])
    assert again.id == before[0].id


def test_identical_bytes_are_one_evidence_whichever_entity_they_declare() -> None:
    """Sameness is bytes and coordinates; the declared entity only scopes replacement.

    Five ``const identity`` declarations in one file claim the same thing about
    the same file. Minting five live ids for identical bytes at identical
    coordinates would break the ledger's first rule, and shaping already treats
    them as one claim.
    """
    ledger = _ledger()
    (first,) = ledger.record([_file_claim("const identity", declares=LOAD)])
    (again,) = ledger.record([_file_claim("const identity", declares=MODEL)])
    assert again.id == first.id
    assert len(ledger) == 1


def test_a_declaration_already_held_as_bytes_is_that_evidence() -> None:
    """The same bytes, first recorded by another route, are not a new identity."""
    ledger = _ledger()
    (held,) = ledger.record([_file_claim("def load_config()")])
    (declared,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])
    assert declared.id == held.id


def test_superseded_declaration_bytes_do_not_resurrect_the_dead_id() -> None:
    ledger = _ledger()
    (first,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])
    (second,) = ledger.record([_file_claim("def load_config(path)", declares=LOAD)])
    (third,) = ledger.record([_file_claim("def load_config()", declares=LOAD)])

    assert third.id not in {first.id, second.id}
    assert third.supersedes == second.id
    stored = ledger.get(first.id)
    assert stored is not None and stored.status is EvidenceStatus.SUPERSEDED


def test_an_unresolvable_declaration_is_still_supplied_under_its_own_span(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing to name the entity costs the handle, never the evidence or its identity."""
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    monkeypatch.setattr(SqliteKnowledgeStore, "keys_for_entities", lambda self, entity_ids: {})
    service = CapabilityService(initialized)

    response = _search(service, BOTH_IN_CONFIG)

    declared = [
        item for item in response.evidence if item.declaration and item.ref.span_key[:2] == CONFIG
    ]
    assert len(declared) >= 2
    assert len({item.id for item in declared}) == len(declared)
    assert all(item.status is EvidenceStatus.VALID for item in declared)
    assert all(item.entity is None for item in declared)
    ledger = service.sessions.get(response.investigation_id).ledger
    for item in declared:
        entry = ledger.get(item.id)
        assert entry is not None and entry.declares is not None
        assert entry.declares.startswith("lines:")


# -- shaping, and the published surface -----------------------------------------------


def _candidate(*, declaration: bool) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository="deployment",
        repository_slug="deployment",
        path="service/pillar.sls",
        line_start=2,
        line_end=2,
        text="service.host",
        declaration=declaration,
    )


def test_shaping_offers_no_line_range_for_declaration_text() -> None:
    """The arithmetic is unchanged; the declaration simply offers it nothing."""
    assert provable_line_span(_candidate(declaration=False)) == (2, 2)
    assert provable_line_span(_candidate(declaration=True)) is None
    assert offered_lines(_candidate(declaration=True)) == (None, None)
    assert offered_lines(_candidate(declaration=False)) == (2, 2)


def test_the_candidate_flag_survives_fusion_and_stays_off_the_v1_payload() -> None:
    """Fusion copies its representative; V1's ``locate --json`` shape does not move."""
    candidate = _candidate(declaration=True)
    assert candidate.model_copy(update={"score": 1.0}).declaration is True
    assert "declaration" not in candidate.model_dump()


def test_the_published_field_is_described_and_defaults_to_source() -> None:
    field = SearchResponse.model_json_schema()["$defs"]["EvidenceItem"]["properties"]["declaration"]
    assert field["default"] is False
    assert "no line range" in field["description"]
