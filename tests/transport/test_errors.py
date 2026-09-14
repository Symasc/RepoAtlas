"""§16-§19 and §29: a refusal keeps its machine meaning across the transport.

Two rules decide everything in this file.

**A refusal to operate is a tool error.** It comes back with ``isError`` set and
an envelope holding the code, the category, the retryable flag, the detail and
the structured context --- the same envelope a direct caller gets, because it *is*
the same envelope. Flattening any of that into a sentence would leave a machine
client with nothing to branch on but prose, and parsing prose is how a retryable
contention becomes an abandoned investigation.

**A capability that answered is not an error, however unwelcome the answer.** A
search that matched nothing, a walk with no edges, an inventory with no packages
and a verdict of ungrounded are all successful calls. This is the distinction
§17 calls mandatory, and it is the one an adapter is most likely to get wrong,
because "ungrounded" reads like a failure and is not one.

Parity is asserted by building the same error twice: once by letting the
capability raise into :func:`capability_error`, and once by calling the same tool
over the protocol. The two envelopes must be equal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.models.verification import AgentAnswer, VerifyGroundingRequest
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.sync.lock import writer_lock
from tests.conftest import init_git_repository
from tests.transport.conftest import GLOBAL, over_protocol, refusal, repository, structured

from mcp import ClientSession

CONTEXT = CapabilityContext(scope=CapabilityScope.GLOBAL)
QUESTION = "Where is SERVICE_HOST configured?"


def call(
    capability_service: CapabilityService, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One tool call over the protocol, returned as its error envelope."""

    async def body(session: ClientSession) -> Any:
        return await session.call_tool(tool, arguments)

    return refusal(over_protocol(capability_service, body))


def raised_by(call_direct: Any) -> dict[str, Any]:
    """The envelope a direct caller would receive from the same condition."""
    with pytest.raises(Exception) as caught:
        call_direct()
    return capability_error(caught.value).model_dump(mode="json")


# ---------------------------------------------------------------------------
# §29: parity, condition by condition
# ---------------------------------------------------------------------------


def test_repository_not_found_is_the_same_refusal_both_ways(service: CapabilityService) -> None:
    over_mcp = call(service, "repoatlas_repositories", {"context": repository("no-such-repo")})
    direct = raised_by(
        lambda: service.repositories(
            RepositoriesRequest(
                context=CapabilityContext(
                    scope=CapabilityScope.REPOSITORY, repository="no-such-repo"
                )
            )
        )
    )
    assert over_mcp == direct
    assert over_mcp["code"] == "REPOSITORY_NOT_FOUND"
    assert over_mcp["retryable"] is False


def test_repository_ambiguous_keeps_its_candidates(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """The structured part matters most here: the remedy is to pick a candidate.

    A flattened error would leave the caller with a sentence naming two slugs and
    no way to choose between them that was not string surgery.
    """
    workspace, root, _ = indexed
    for team in ("team-a", "team-b"):
        twin = root / team / "shared-name"
        twin.mkdir(parents=True)
        (twin / "twin.py").write_text(f"TEAM = {team!r}\n", encoding="utf-8")
        init_git_repository(twin)

    service = CapabilityService(workspace)
    service.sync(SyncRequest(context=CONTEXT))

    over_mcp = call(service, "repoatlas_status", {"context": repository("shared-name")})
    assert over_mcp["code"] == "REPOSITORY_AMBIGUOUS"
    assert over_mcp["category"] == "context"
    assert sorted(over_mcp["context"]["candidates"]) == [
        "team-a/shared-name",
        "team-b/shared-name",
    ]


def test_unknown_investigation_is_the_same_refusal_both_ways(service: CapabilityService) -> None:
    """§7: an id this server did not issue is refused, never quietly created.

    A silently created empty ledger would accept the id and then report every
    citation into it as fabricated, which reads to the caller exactly like its own
    evidence having been invented.
    """
    arguments = {"context": GLOBAL, "question": QUESTION, "investigation_id": "not-a-real-id"}
    over_mcp = call(service, "repoatlas_search", arguments)
    direct = raised_by(
        lambda: service.search(
            SearchRequest(question=QUESTION, context=CONTEXT, investigation_id="not-a-real-id")
        )
    )
    assert over_mcp == direct
    assert over_mcp["code"] == "UNKNOWN_INVESTIGATION"
    assert over_mcp["category"] == "session"
    assert len(service.sessions) == 0, "a refused id must not have created an investigation"


def test_ledger_full_is_the_same_refusal_both_ways(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    workspace, _, _ = indexed
    service = CapabilityService(workspace, ledger_capacity=1)
    over_mcp = call(service, "repoatlas_search", {"question": QUESTION, "context": GLOBAL})
    direct = raised_by(lambda: service.search(SearchRequest(question=QUESTION, context=CONTEXT)))
    assert over_mcp["code"] == direct["code"] == "LEDGER_FULL"
    assert over_mcp["category"] == "session"
    assert over_mcp["retryable"] is False
    # The headroom that makes the refusal actionable survived.
    assert set(over_mcp["context"]) == {"required", "available", "capacity"}
    service.close()


def test_entity_not_found_is_the_same_refusal_both_ways(service: CapabilityService) -> None:
    over_mcp = call(
        service, "repoatlas_relationships", {"context": GLOBAL, "target": "env:NOT_A_REAL_ENTITY"}
    )
    direct = raised_by(
        lambda: service.relationships(
            RelationshipsRequest(context=CONTEXT, target="env:NOT_A_REAL_ENTITY")
        )
    )
    assert over_mcp == direct
    assert over_mcp["code"] == "ENTITY_NOT_FOUND"
    assert over_mcp["category"] == "not_found"


def test_invalid_cursor_is_the_same_refusal_both_ways(service: CapabilityService) -> None:
    over_mcp = call(service, "repoatlas_dependencies", {"context": GLOBAL, "cursor": "nonsense"})
    direct = raised_by(
        lambda: service.dependencies(DependenciesRequest(context=CONTEXT, cursor="nonsense"))
    )
    assert over_mcp == direct
    assert over_mcp["code"] == "INVALID_CURSOR"
    assert over_mcp["category"] == "invalid_request"


def test_stale_cursor_is_a_different_refusal_from_a_wrong_one(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """The index moved under a walk. A different condition and a different remedy.

    Worth carrying separately across the transport precisely because both are
    about a cursor: a client that saw one code for both would retry a walk that
    can never resume.
    """
    workspace, _, _ = indexed
    service = CapabilityService(workspace)

    async def body(session: ClientSession) -> Any:
        first = structured(
            await session.call_tool("repoatlas_dependencies", {"context": GLOBAL, "limit": 2})
        )
        assert first["next_cursor"] is not None
        workspace.database.execute(
            "UPDATE dependencies SET constraint_text = '>=99' WHERE role = 'declared'"
        )
        workspace.database.connection.commit()
        return await session.call_tool(
            "repoatlas_dependencies",
            {"context": GLOBAL, "limit": 2, "cursor": first["next_cursor"]},
        )

    error = refusal(over_protocol(service, body))
    assert error["code"] == "STALE_CURSOR"
    assert error["category"] == "session"
    assert error["code"] != "INVALID_CURSOR"
    service.close()


def test_invalid_grounding_request_is_the_same_refusal_both_ways(
    service: CapabilityService,
) -> None:
    """An answer with nothing in it. Refused before any investigation is touched."""
    supplied = service.search(SearchRequest(question=QUESTION, context=CONTEXT))
    arguments = {"investigation_id": supplied.investigation_id, "answer": {}}
    over_mcp = call(service, "repoatlas_verify_grounding", arguments)
    direct = raised_by(
        lambda: service.verify_grounding(
            VerifyGroundingRequest(investigation_id=supplied.investigation_id, answer=AgentAnswer())
        )
    )
    assert over_mcp == direct
    assert over_mcp["code"] == "INVALID_GROUNDING_REQUEST"
    assert over_mcp["context"]["field"] == "answer"


def test_not_initialized_is_the_same_refusal_both_ways(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    service = CapabilityService(workspace)
    try:
        over_mcp = call(service, "repoatlas_sync", {"context": GLOBAL})
        direct = raised_by(lambda: service.sync(SyncRequest(context=CONTEXT)))
        assert over_mcp == direct
        assert over_mcp["code"] == "NOT_INITIALIZED"
        assert over_mcp["category"] == "index"
        assert workspace.root is None, "a refusal must not have initialized anything"
    finally:
        service.close()


# ---------------------------------------------------------------------------
# §18: the two sync failures, which must not be confused with each other
# ---------------------------------------------------------------------------


def test_writer_contention_stays_retryable_across_the_transport(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """The one condition where repeating the identical call is the right move.

    If this arrived as a generic failure, an agent would abandon a sync that
    another process was two seconds from releasing.
    """
    workspace, _, _ = indexed
    workspace.settings = workspace.settings.model_copy(
        update={
            "storage": workspace.settings.storage.model_copy(update={"lock_timeout_seconds": 0.0})
        }
    )
    service = CapabilityService(workspace)
    with writer_lock(Path(workspace.database.path), timeout=0.0):
        error = call(service, "repoatlas_sync", {"context": GLOBAL})
    assert error["code"] == "SYNC_IN_PROGRESS"
    assert error["category"] == "contention"
    assert error["retryable"] is True
    service.close()


def test_a_failed_sync_keeps_index_may_have_changed(
    indexed: tuple[Workspace, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18: the detail a caller holding evidence has to act on.

    V1's sync is deliberately not all-or-nothing, so a failure part-way has
    already moved the index. An error that lost that flag would tell an agent its
    citations were as good as before, and some of them would not be.
    """
    workspace, root, _ = indexed
    (root / "python-service" / "src" / "config.py").write_text(
        "SERVICE_HOST = 'changed-by-the-transport-test'\n", encoding="utf-8"
    )
    service = CapabilityService(workspace)

    def failing_commit(self: ReferenceLinker, plan: object) -> object:
        raise RuntimeError("injected failure inside the final transaction")

    monkeypatch.setattr(ReferenceLinker, "commit_global", failing_commit)
    error = call(service, "repoatlas_sync", {"context": GLOBAL})
    monkeypatch.undo()

    assert error["code"] == "SYNC_FAILED"
    assert error["category"] == "internal"
    assert error["retryable"] is False
    assert error["context"]["index_may_have_changed"] is True
    assert error["context"]["cause"] == "INTERNAL_ERROR"
    assert error["context"]["revalidation"]["status"] == "completed"
    service.close()


def test_a_committed_sync_with_a_failed_recheck_is_a_successful_tool_call(
    indexed: tuple[Workspace, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18: the case an adapter is most tempted to turn into an error.

    The sync happened. The index moved. Reporting that as a tool failure would
    tell the agent nothing changed, and the agent would go on citing evidence
    against a snapshot that no longer exists. So it is a success carrying a
    failed revalidation, and the response says both.
    """
    workspace, _, _ = indexed
    service = CapabilityService(workspace)
    service.search(SearchRequest(question=QUESTION, context=CONTEXT))

    def unreadable(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected failure while re-checking evidence")

    # Precisely the recheck, not the sync: the two have to be able to fail apart.
    monkeypatch.setattr(CapabilityService, "_ledger_snapshot", unreadable)

    async def body(session: ClientSession) -> Any:
        return await session.call_tool("repoatlas_sync", {"context": GLOBAL})

    result = over_protocol(service, body)
    monkeypatch.undo()

    assert result.isError is False, "a committed sync is not a failed tool call"
    payload = dict(result.structuredContent or {})
    assert payload["status"] == "completed"
    assert payload["evidence_revalidation"]["status"] == "failed"
    assert payload["evidence_revalidation"]["detail"]
    service.close()


# ---------------------------------------------------------------------------
# §17: grounding outcomes are results, never protocol errors
# ---------------------------------------------------------------------------


def test_an_ungrounded_verdict_is_a_successful_tool_call(service: CapabilityService) -> None:
    """The mandatory distinction. A verdict is an answer; only a refusal is an error."""
    supplied = service.search(SearchRequest(question=QUESTION, context=CONTEXT))

    async def body(session: ClientSession) -> Any:
        return await session.call_tool(
            "repoatlas_verify_grounding",
            {
                "investigation_id": supplied.investigation_id,
                "answer": {
                    "summary": "made up",
                    "claims": [
                        {
                            "statement": "Something nobody supplied.",
                            "citations": [{"evidence_id": "E999"}],
                        }
                    ],
                },
            },
        )

    result = over_protocol(service, body)
    assert result.isError is False, "an ungrounded verdict is not a transport failure"
    payload = dict(result.structuredContent or {})
    assert payload["verdict"] == "ungrounded"
    assert payload["findings"], "and the reason came back as a finding"


def test_a_stale_citation_is_a_finding_and_not_an_error(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    workspace, root, _ = indexed
    service = CapabilityService(workspace)
    supplied = service.search(SearchRequest(question=QUESTION, context=CONTEXT))
    cited = supplied.evidence[0]
    target = root / cited.ref.repository_slug / cited.ref.path
    target.write_text(target.read_text(encoding="utf-8") + "\n# moved\n", encoding="utf-8")
    # Evidence is checked against the index, so the edit has to be indexed before
    # the citation can go stale. Editing the tree alone changes nothing it cites.
    service.sync(SyncRequest(context=CONTEXT))

    async def body(session: ClientSession) -> Any:
        return await session.call_tool(
            "repoatlas_verify_grounding",
            {
                "investigation_id": supplied.investigation_id,
                "answer": {
                    "summary": "s",
                    "claims": [
                        {
                            "statement": "A claim about a file that has since changed.",
                            "citations": [{"evidence_id": cited.id}],
                        }
                    ],
                },
            },
        )

    result = over_protocol(service, body)
    assert result.isError is False
    payload = dict(result.structuredContent or {})
    assert payload["verdict"] in {"ungrounded", "partially_grounded"}
    assert any("stale" in finding["code"].lower() for finding in payload["findings"])
    service.close()


@pytest.mark.parametrize(
    ("tool", "arguments", "empty_field"),
    [
        (
            "repoatlas_search",
            {"question": "zzqqxx-nothing-matches-this-token", "context": GLOBAL},
            "evidence",
        ),
        (
            "repoatlas_dependencies",
            {"context": GLOBAL, "name": "no-such-package-anywhere"},
            "packages",
        ),
    ],
)
def test_an_empty_result_is_a_successful_call(
    service: CapabilityService, tool: str, arguments: dict[str, Any], empty_field: str
) -> None:
    """§16: found nothing is an answer. Only a refusal to look is an error."""

    async def body(session: ClientSession) -> Any:
        return await session.call_tool(tool, arguments)

    result = over_protocol(service, body)
    assert result.isError is False
    payload = dict(result.structuredContent or {})
    assert payload[empty_field] == []


# ---------------------------------------------------------------------------
# §19: malformed arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_field"),
    [
        ("repoatlas_relationships", {"context": GLOBAL, "target": "x", "hops": 3}, "hops"),
        (
            "repoatlas_inspect",
            {
                "context": {"scope": "repository", "repository": "python-service"},
                "target": {"path": "src/config.py"},
                "line_start": -1,
            },
            "line_start",
        ),
        ("repoatlas_verify_grounding", {"answer": {"summary": "s"}}, "investigation_id"),
        ("repoatlas_search", {"context": GLOBAL}, "question"),
        ("repoatlas_status", {"context": {"scope": "repository"}}, "context"),
    ],
)
def test_a_malformed_argument_is_a_structured_invalid_request(
    service: CapabilityService, tool: str, arguments: dict[str, Any], expected_field: str
) -> None:
    """Deterministic, field-located, and never a traceback.

    The request model does the refusing, so the bounds, enums and cross-field
    rules are the real ones rather than a second opinion maintained here.
    """
    error = call(service, tool, arguments)
    assert error["code"] == "INVALID_REQUEST"
    assert error["category"] == "invalid_request"
    assert error["retryable"] is False
    assert error["context"]["tool"] == tool
    assert any(expected_field in item["field"] for item in error["context"]["fields"]), error
    # A location and a reason, not a stack.
    assert "Traceback" not in (error["detail"] or "")
    assert "ValidationError" not in (error["detail"] or "")


def test_a_validation_error_does_not_echo_the_offending_input(
    service: CapabilityService,
) -> None:
    """A caller's own argument is never mirrored back into the envelope.

    It is the one part of a validation failure that can be arbitrarily large and
    can hold whatever text the caller pasted in, and no client needs to be told
    what it just sent.
    """
    sentinel = "PASTED-ARGUMENT-THAT-MUST-NOT-COME-BACK"
    error = call(
        service,
        "repoatlas_relationships",
        {"context": GLOBAL, "target": sentinel, "hops": 99},
    )
    assert sentinel not in str(error), error


def test_an_unexpected_failure_still_arrives_as_a_structured_error(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16: nothing reaches the client as a bare exception string.

    The SDK's own fallback would turn any escaping exception into ``str(exc)`` with
    no code and no category. This proves the adapter never gives it the chance.
    """

    def explode(self: object, request: object) -> object:
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(CapabilityService, "status", explode)
    error = call(service, "repoatlas_status", {"context": GLOBAL})
    assert error["code"] == "INTERNAL_ERROR"
    assert error["category"] == "internal"
    assert error["retryable"] is False


def test_no_error_code_collapses_into_one(service: CapabilityService) -> None:
    """§37 mutation O: distinct conditions keep distinct codes over the wire.

    Asserted as a set rather than one at a time, because the failure being guarded
    against is uniform: an adapter that mapped everything to INTERNAL_ERROR would
    pass every single-code test that only checked the call did not succeed.
    """
    codes = {
        call(service, "repoatlas_repositories", {"context": repository("nope")})["code"],
        call(
            service,
            "repoatlas_relationships",
            {"context": GLOBAL, "target": "env:NOPE"},
        )["code"],
        call(service, "repoatlas_dependencies", {"context": GLOBAL, "cursor": "x"})["code"],
        call(
            service,
            "repoatlas_search",
            {"context": GLOBAL, "question": QUESTION, "investigation_id": "nope"},
        )["code"],
        call(service, "repoatlas_relationships", {"context": GLOBAL, "target": "x", "hops": 9})[
            "code"
        ],
        call(service, "repoatlas_nonexistent", {})["code"],
    }
    assert codes == {
        "REPOSITORY_NOT_FOUND",
        "ENTITY_NOT_FOUND",
        "INVALID_CURSOR",
        "UNKNOWN_INVESTIGATION",
        "INVALID_REQUEST",
        "UNKNOWN_TOOL",
    }
