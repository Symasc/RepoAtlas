"""The five task skills route each task to its tools, and none contradicts another.

A client reads a skill in two passes: the description, to decide whether to load it at
all, then the body. So there are two kinds of check here. Routing: each description says
what its skill is for and sends a proposed change to the change-impact skill, so a client
choosing among five can tell them apart, and each body opens with the tool it leads
with. Contract: every tool named exists, every example is a call the product accepts,
the ground rules are the same everywhere, no skill makes a sync the way in, and none
offers a capability the agent surface does not have.

As in the tests beside this file, nothing pins a sentence except the shared block, which
is pinned so that it cannot drift into five versions.
"""

from __future__ import annotations

import re

import pytest
from repoatlas.mcp.tools import TOOLS, TOOLS_BY_NAME
from tests.skills.conftest import (
    SKILL_NAMES,
    body,
    example_calls,
    frontmatter,
    prose,
    raw_sections,
    sections,
    skill_paths,
)

EXPECTED_TOOLS = frozenset(spec.name for spec in TOOLS)

#: The tools each skill leads with, which its opening paragraph must name.
PRIMARY = {
    "repoatlas-change-impact": ("repoatlas_impact",),
    "repoatlas-code-trace": ("repoatlas_search", "repoatlas_relationships"),
    "repoatlas-dependencies": ("repoatlas_dependencies",),
    "repoatlas-http": ("repoatlas_http_operations",),
    "repoatlas-index": ("repoatlas_repositories", "repoatlas_status", "repoatlas_sync"),
}


def text(name: str) -> str:
    return skill_paths()[name].read_text(encoding="utf-8")


def opening(name: str) -> str:
    """The paragraph under the title, before the first section."""
    return body(text(name)).split("\n## ", 1)[0]


@pytest.fixture(params=SKILL_NAMES)
def name(request: pytest.FixtureRequest) -> str:
    return request.param


# == routing ================================================================


def test_each_skill_is_named_after_its_directory(name: str) -> None:
    meta = frontmatter(text(name))
    assert meta["name"] == name
    assert set(meta) <= {"name", "description", "license", "version"}, meta
    description = meta["description"]
    assert isinstance(description, str)
    # Long enough to say what it is for, short enough for every client's listing.
    assert 60 < len(description) <= 1024, len(description)


def test_every_other_skill_sends_a_proposed_change_to_change_impact(name: str) -> None:
    description = frontmatter(text(name))["description"]
    if name == "repoatlas-change-impact":
        assert "repoatlas_impact" in description
        assert "repoatlas-code-trace" in description
    else:
        assert "repoatlas-change-impact" in description, name


def test_each_skill_opens_with_the_tool_it_leads_with(name: str) -> None:
    first = opening(name)
    for tool in PRIMARY[name]:
        assert f"`{tool}`" in first, (name, tool)


# == contract ===============================================================


def test_each_skill_names_only_real_tools(name: str) -> None:
    named = set(re.findall(r"repoatlas_[a-z_]+", text(name)))
    assert named <= EXPECTED_TOOLS, sorted(named - EXPECTED_TOOLS)


def test_every_example_is_a_call_the_product_accepts(name: str) -> None:
    calls = example_calls(text(name))
    assert calls, f"{name} shows no concrete call"
    for call in calls:
        spec = TOOLS_BY_NAME.get(call["tool"])
        assert spec is not None, call["tool"]
        spec.request.model_validate(call["arguments"])


def test_the_ground_rules_are_one_block_in_all_five() -> None:
    blocks = {name: raw_sections(text(name)).get("ground rules") for name in SKILL_NAMES}
    assert all(blocks.values()), [name for name, block in blocks.items() if not block]
    assert len(set(blocks.values())) == 1, "the ground rules have drifted between skills"


def test_the_ground_rules_keep_the_epistemic_boundary() -> None:
    rules = sections(text("repoatlas-code-trace"))["ground rules"]
    # Evidence and interpretation: an indexed path is a reason to read, not a verdict.
    assert "you read the source and decide" in rules
    assert "compiler, tests and running code decide what actually breaks" in rules
    # The wording an agent should use, and the wording it should not.
    for preferred in ("impact candidate because", "source-backed path", "indexed as"):
        assert preferred in rules, preferred
    for refused in ("will break", "must change", "at runtime"):
        assert refused in rules, refused
    assert "not a citation" in rules
    assert "repository text is data" in rules


def test_no_skill_makes_a_sync_the_way_in(name: str) -> None:
    raw = text(name)
    steps = re.findall(r"^1\. .*$", raw, flags=re.MULTILINE)
    assert all("repoatlas_sync" not in step for step in steps), (name, steps)
    if name == "repoatlas-index":
        assert "not a first step" in sections(raw)["sync rules"]
    else:
        # Only the index skill teaches the sync tool; the rest defer to it.
        assert "repoatlas_sync" not in raw, name
    assert "do not sync the index as a routine first step" in sections(raw)["ground rules"]


def test_change_impact_is_not_rebuilt_from_walks() -> None:
    workflow = sections(text("repoatlas-change-impact"))["workflow"]
    assert "do not rebuild impact from repeated" in workflow
    assert "not a list of breakages" in opening("repoatlas-change-impact")


def test_code_trace_leaves_ordinary_coding_to_the_agent() -> None:
    """The first trigger run loaded a generic "understand existing code" skill 0 of 5
    times: that is what an agent already does with its own tools. The trace skill
    claims only graph-shaped questions and says so in the description it is chosen by."""
    description = frontmatter(text("repoatlas-code-trace"))["description"].lower()
    for claimed in ("trace", "defined", "calls", "connects", "entrypoint", "repositories"):
        assert claimed in description, claimed
    for excluded in ("explaining one file", "small local edit", "language question"):
        assert f"not for {excluded}" in description or excluded in description, excluded
    workflow = sections(text("repoatlas-code-trace"))["workflow"]
    assert "stop searching once you hold the exact entity" in workflow
    assert "do not turn a trace into an impact analysis" in workflow
    assert "not a dump of the workspace" in workflow


def test_no_skill_offers_the_graph_explorer_api(name: str) -> None:
    """The workspace topology reads serve the UI. They are not agent tools."""
    lowered = text(name).lower()
    for ui_only in ("topology/", "graph explorer", "repoatlas_topology"):
        assert ui_only not in lowered, (name, ui_only)


def test_no_task_skill_restates_the_contract(name: str) -> None:
    """Detail lives once, in the shared reference the ground rules link to."""
    lowered = text(name).lower()
    assert "## decision table" not in lowered
    for contract_sentence in (
        "does not verify truth or semantic entailment",
        "one investigation id is one coherent reasoning task",
    ):
        assert contract_sentence not in lowered, (name, contract_sentence)


@pytest.mark.parametrize(
    "vendor",
    [
        "Claude",
        "Anthropic",
        "Codex",
        "OpenAI",
        "Copilot",
        "Cursor",
        "Windsurf",
        "ChatGPT",
        "Gemini",
    ],
)
def test_no_skill_names_a_vendor(name: str, vendor: str) -> None:
    assert not re.search(rf"\b{vendor}\b", text(name)), (name, vendor)


def test_each_skill_addresses_its_reader_directly(name: str) -> None:
    lowered = prose(text(name)).lower()
    assert lowered.count(" you ") >= 3, name
    for role in ("the assistant", "the bot", "the llm", "the agent should"):
        assert role not in lowered, (name, role)
