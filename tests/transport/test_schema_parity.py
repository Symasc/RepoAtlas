"""§10 and §30: the advertised schema is the V2 request model, not a copy of it.

The failure this suite exists to catch is drift, and drift here is silent in the
worst way: the protocol goes on advertising a field the model stopped accepting,
or a bound the model no longer enforces, and a caller that satisfies the schema is
refused by the thing behind it. So the comparison is exact --- the advertised
object must *be* ``model_json_schema()`` --- and the assertions below say in
domain terms what that identity is protecting, so a failure names the property
that broke rather than printing two large dictionaries.
"""

from __future__ import annotations

from typing import Any

import pytest
from repoatlas.mcp.tools import TOOLS, ToolSpec
from tests.transport.conftest import over_protocol

from mcp import ClientSession

CASES = pytest.mark.parametrize("spec", TOOLS, ids=[spec.name for spec in TOOLS])


def advertised(capability_service: Any) -> dict[str, dict[str, Any]]:
    async def body(session: ClientSession) -> dict[str, dict[str, Any]]:
        listed = await session.list_tools()
        return {tool.name: tool.inputSchema for tool in listed.tools}

    return over_protocol(capability_service, body)


@CASES
def test_the_advertised_schema_is_the_request_models_own(service: Any, spec: ToolSpec) -> None:
    """Identity, not resemblance.

    Anything weaker would pass while a field was quietly retyped. This is the
    snapshot test §30 asks for, and its snapshot is the model itself rather than a
    checked-in copy that would have to be regenerated --- and would therefore be
    regenerated on the day it caught something.
    """
    assert advertised(service)[spec.name] == spec.request.model_json_schema()


@CASES
def test_no_field_is_invented_by_the_transport(service: Any, spec: ToolSpec) -> None:
    """Every advertised property is a field the request model actually accepts.

    A transport that advertised an argument the model would reject is worse than
    one that advertised too few: the caller supplies it, satisfies the schema, and
    is refused anyway.
    """
    schema = advertised(service)[spec.name]
    assert set(schema["properties"]) == set(spec.request.model_fields)


@CASES
def test_required_and_optional_are_the_models_own(service: Any, spec: ToolSpec) -> None:
    """A field optional in V2 stays optional here, and required stays required.

    Marking an optional field required is the mutation that breaks the
    investigation contract in particular: ``investigation_id`` is omitted to begin
    an investigation, so requiring it would make a first call impossible.
    """
    schema = advertised(service)[spec.name]
    required = set(schema.get("required", ()))
    expected = {name for name, field in spec.request.model_fields.items() if field.is_required()}
    assert required == expected


@CASES
def test_the_transport_adds_no_ambient_scope_field(service: Any, spec: ToolSpec) -> None:
    """§13: no schema offers a working directory, a root, or a client workspace.

    The protocol has a roots concept and a client that advertises one. A field
    here that accepted it would make RepoAtlas's scope depend on which transport
    asked, which is the ambient default the capability layer exists without.
    """
    schema = advertised(service)[spec.name]
    banned = {"cwd", "root", "roots", "workspace", "directory", "path_prefix", "base_dir"}
    assert not (set(schema["properties"]) & banned)


def test_every_enum_member_survives_the_transport(service: Any) -> None:
    """Enum values are the vocabulary a caller has to use, spelled exactly.

    Checked at the string level because that is what crosses: a renamed member
    would still be a valid Python enum and would still schematize, and the only
    party that notices is the caller whose literal stopped being accepted.
    """
    schemas = advertised(service)
    scopes = schemas["repoatlas_search"]["$defs"]["CapabilityScope"]["enum"]
    assert sorted(scopes) == ["global", "repository"]
    assert sorted(schemas["repoatlas_search"]["$defs"]["SearchMode"]["enum"]) == [
        "diagnostic",
        "lookup",
    ]
    assert sorted(schemas["repoatlas_status"]["$defs"]["FreshnessDepth"]["enum"]) == [
        "live",
        "stored",
    ]
    directions = schemas["repoatlas_relationships"]["$defs"]["RelationshipDirection"]["enum"]
    assert sorted(directions) == ["both", "incoming", "outgoing"]


def test_numeric_bounds_survive_the_transport(service: Any) -> None:
    """A bound that did not travel would be discovered only as a refusal."""
    relationships = advertised(service)["repoatlas_relationships"]["properties"]
    assert relationships["hops"]["minimum"] == 1
    assert relationships["hops"]["maximum"] == 2

    inspect = advertised(service)["repoatlas_inspect"]["properties"]
    # line_start/line_end are optional, so the bound sits inside the union arm.
    starts = [arm for arm in inspect["line_start"]["anyOf"] if arm.get("type") == "integer"]
    assert starts and starts[0]["minimum"] == 1


def test_defaults_survive_the_transport(service: Any) -> None:
    """The defaults an agent relies on when it omits an argument."""
    schemas = advertised(service)
    assert schemas["repoatlas_sync"]["properties"]["full"]["default"] is False
    assert schemas["repoatlas_sync"]["properties"]["depth"]["default"] == "live"
    assert schemas["repoatlas_search"]["properties"]["depth"]["default"] == "stored"
    assert schemas["repoatlas_search"]["properties"]["include_trace"]["default"] is False
    assert schemas["repoatlas_inspect"]["properties"]["allow_worktree"]["default"] is True
    assert schemas["repoatlas_dependencies"]["properties"]["include_provenance"]["default"] is True
    operations = schemas["repoatlas_http_operations"]["properties"]
    assert operations["include_provenance"]["default"] is False
    assert operations["role"]["default"] == "both"


def test_investigation_semantics_survive_the_transport(service: Any) -> None:
    """§7: optional everywhere it is optional, required where a ledger is named.

    The whole investigation contract is visible in these two facts. Search may
    mint one, so its id is optional; verification has nothing to check against
    without one, so its id is required and must be non-empty.
    """
    schemas = advertised(service)
    for name in (
        "repoatlas_search",
        "repoatlas_inspect",
        "repoatlas_relationships",
        "repoatlas_dependencies",
        "repoatlas_http_operations",
    ):
        assert "investigation_id" not in schemas[name].get("required", ())
        assert "investigation_id" in schemas[name]["properties"]

    verify = schemas["repoatlas_verify_grounding"]
    assert "investigation_id" in verify["required"]
    assert verify["properties"]["investigation_id"]["minLength"] == 1

    # And the four that never touch an investigation do not offer one.
    for name in (
        "repoatlas_repositories",
        "repoatlas_status",
        "repoatlas_deployment_targets",
        "repoatlas_sync",
    ):
        assert "investigation_id" not in schemas[name]["properties"]


def test_the_exactly_one_target_rule_is_still_enforced_behind_the_schema(service: Any) -> None:
    """§10: a cross-field rule JSON Schema cannot express still holds.

    ``InspectTarget`` accepts exactly one of path, entity or evidence. JSON Schema
    derived from a Pydantic model does not carry the ``model_validator`` that says
    so, which is exactly why the model itself is the validation authority here
    rather than the schema. Proved by calling, not by reading the schema.
    """
    from tests.transport.conftest import refusal

    async def body(session: ClientSession) -> object:
        return await session.call_tool(
            "repoatlas_inspect",
            {
                "context": {"scope": "repository", "repository": "python-service"},
                "target": {"path": "src/config.py", "entity_key": "env:SERVICE_HOST"},
            },
        )

    error = refusal(over_protocol(service, body))
    assert error["code"] == "INVALID_REQUEST"
    assert error["category"] == "invalid_request"
    assert any("target" in item["field"] for item in error["context"]["fields"])


# ---------------------------------------------------------------------------
# RC audit, RCA-07: the tool list stays derived, and stays input-only
# ---------------------------------------------------------------------------


def test_no_tool_advertises_an_output_schema(service: Any) -> None:
    """Recorded as a decision, and pinned so it stays one.

    Output schemas are omitted deliberately: the nine response models come to
    several times the size of the inputs, every byte of it lands in the tool list
    an agent reads before asking anything, and the SDK skips output validation for
    a handler that returns a result object directly --- which this one must, to
    carry a structured error. So declaring them would spend a large part of a
    caller's context advertising a contract nothing enforces.

    Adding them later is a legitimate decision for somebody to take deliberately.
    This exists so it cannot happen by accident, on the way to something else.
    """

    async def body(session: ClientSession) -> list[Any]:
        listed = await session.list_tools()
        return [tool.model_dump() for tool in listed.tools]

    advertised_tools = over_protocol(service, body)

    assert len(advertised_tools) == len(TOOLS)
    for tool in advertised_tools:
        assert tool.get("outputSchema") is None, tool["name"]


def test_no_schema_is_written_by_hand_anywhere_in_the_transport() -> None:
    """The size of the tool list is the price of a derived contract, not of prose.

    Any future attempt to shrink it has one tempting shape --- a trimmed copy of a
    schema, kept by hand --- and that is the one shape that reintroduces silent
    drift between what is advertised and what is accepted.
    """
    from pathlib import Path

    for module in (Path("repoatlas/mcp/server.py"), Path("repoatlas/mcp/tools.py")):
        source = module.read_text(encoding="utf-8")
        for forbidden in ('"properties"', "'properties'", "outputSchema=", '"$defs"'):
            assert forbidden not in source, f"{module}: {forbidden}"
