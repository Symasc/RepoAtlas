"""No agent-facing text sends change impact anywhere but the impact tool.

``relationships`` stays the walk from one exact entity. What this pins is which
workflow an agent is told to use when the question is what a change affects. Every
surface an agent reads before or while choosing a tool is read here, sentence by
sentence: the server instructions, each tool's description and advertised input
schema, all five task skills and their shared contract reference, the integration
document and the README. A sentence about change impact must not recommend a
relationships walk for it, and the places an agent reads first must each name the impact
tool.
"""

from __future__ import annotations

import re
from pathlib import Path

from repoatlas.mcp.server import INSTRUCTIONS, tool_definitions

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SKILL_NAMES = (
    "repoatlas-change-impact",
    "repoatlas-code-trace",
    "repoatlas-dependencies",
    "repoatlas-http",
    "repoatlas-index",
)
CONTRACTS = SKILLS / "repoatlas-code-trace" / "references" / "contracts.md"

#: A sentence about what a change affects.
ABOUT_IMPACT = re.compile(r"change[- ]impact|what a change\b[^.|]*\baffects?|what depends on", re.I)
#: Another workflow for it, in any of its spellings.
OTHER_WORKFLOW = re.compile(r"relationships|\brows\b|\bhops\b|incoming", re.IGNORECASE)
#: Naming the impact tool.
NAMES_IMPACT = re.compile(r"repoatlas_impact", re.I)


def surfaces() -> dict[str, str]:
    texts = {
        "instructions": INSTRUCTIONS,
        **{name: (SKILLS / name / "SKILL.md").read_text(encoding="utf-8") for name in SKILL_NAMES},
        "contracts": CONTRACTS.read_text(encoding="utf-8"),
        "integration": (ROOT / "docs" / "agent-integration.md").read_text(encoding="utf-8"),
        "readme": (ROOT / "README.md").read_text(encoding="utf-8"),
    }
    for tool in tool_definitions():
        texts[f"{tool.name} description"] = tool.description or ""
        texts[f"{tool.name} schema"] = " ".join(_strings(tool.inputSchema))
    return texts


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def sentences(text: str) -> list[str]:
    """Clauses: sentences, table rows and list items, and within a sentence that lists
    tools, each tool's own clause -- "`a` for this, `b` for that" gives each tool a job
    and recommends neither for the other's."""
    found = []
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            found.extend(c for c in re.split(r";\s+|,\s+(?=`repoatlas_)", sentence) if c)
    return found


def about_impact() -> list[tuple[str, str]]:
    return [
        (name, sentence)
        for name, text in surfaces().items()
        for sentence in sentences(text)
        if ABOUT_IMPACT.search(sentence)
    ]


def test_no_sentence_about_change_impact_recommends_another_workflow() -> None:
    offending = [
        (name, sentence) for name, sentence in about_impact() if OTHER_WORKFLOW.search(sentence)
    ]
    assert offending == []


def test_where_an_agent_reads_first_the_impact_tool_is_named() -> None:
    found = about_impact()
    for surface in (
        "instructions",
        "repoatlas-change-impact",
        "repoatlas-code-trace",
        "contracts",
    ):
        assert any(name == surface and NAMES_IMPACT.search(sentence) for name, sentence in found), (
            surface
        )


def test_a_relationships_walk_for_change_impact_would_fail_it() -> None:
    walk = "For change impact, walk `repoatlas_relationships` incoming, two hops."
    assert ABOUT_IMPACT.search(walk) and OTHER_WORKFLOW.search(walk)
