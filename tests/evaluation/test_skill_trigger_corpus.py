"""The skill-trigger corpus: which RepoAtlas skill a request should load.

A client picks a skill by reading five descriptions, so whether the right one loads is a
property of those descriptions and of the model reading them. The second half needs a
model and is measured by the live harness in the bench, which reads this same corpus.
What can be checked without one is checked here: the corpus is well formed, it covers the
boundaries a careless router would cross, and each skill's description still carries the
vocabulary its cases route on. A description edited away from its trigger fails here
before anyone pays to find out.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.mcp.tools import TOOLS
from tests.skills.conftest import SKILL_NAMES, frontmatter, skill_paths

CORPUS_PATH = Path(__file__).parent / "skill_trigger_cases.yaml"
TOOL_NAMES = frozenset(spec.name for spec in TOOLS)
NONE = "none"

#: The boundaries a request is most often routed across. Each needs a case on it.
BOUNDARIES = (
    ("repoatlas-change-impact", "repoatlas-investigation"),
    ("repoatlas-change-impact", "repoatlas-dependencies"),
    ("repoatlas-change-impact", "repoatlas-http"),
    ("repoatlas-investigation", "repoatlas-http"),
    ("repoatlas-investigation", "repoatlas-dependencies"),
    ("repoatlas-index", "repoatlas-investigation"),
)


def load() -> dict[str, Any]:
    return yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))


CORPUS = load()
CASES: list[dict[str, Any]] = CORPUS["cases"]


def test_the_corpus_routes_to_exactly_the_five_skills() -> None:
    assert tuple(CORPUS["skills"]) == SKILL_NAMES
    assert {case["expect"] for case in CASES} == {*SKILL_NAMES, NONE}


def test_case_ids_and_prompts_are_unique() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)), ids
    prompts = [" ".join(case["prompt"].split()) for case in CASES]
    assert len(prompts) == len(set(prompts))


def test_every_skill_has_enough_cases_and_some_requests_need_none() -> None:
    for name in SKILL_NAMES:
        count = sum(case["expect"] == name for case in CASES)
        assert count >= 4, (name, count)
    assert sum(case["expect"] == NONE for case in CASES) >= 2


def test_every_confusable_boundary_has_a_case() -> None:
    crossed = {
        frozenset((case["expect"], other))
        for case in CASES
        for other in case.get("confusable_with", ())
    }
    for boundary in BOUNDARIES:
        assert frozenset(boundary) in crossed, boundary


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_each_case_is_well_formed(case: dict[str, Any]) -> None:
    assert set(case) <= {"id", "prompt", "expect", "confusable_with", "first_tool", "forbid_tools"}
    for other in case.get("confusable_with", ()):
        assert other in SKILL_NAMES and other != case["expect"], other
    for tool in (*case.get("first_tool", ()), *case.get("forbid_tools", ())):
        assert tool in TOOL_NAMES, tool
    assert not set(case.get("first_tool", ())) & set(case.get("forbid_tools", ()))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_no_prompt_names_a_skill_or_a_tool(case: dict[str, Any]) -> None:
    """A request that says which skill to use measures reading, not routing."""
    prompt = case["prompt"]
    assert not re.search(r"repoatlas[-_][a-z]", prompt, flags=re.IGNORECASE), prompt


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_each_description_carries_the_vocabulary_its_cases_route_on(name: str) -> None:
    description = frontmatter(skill_paths()[name].read_text(encoding="utf-8"))["description"]
    lowered = description.lower()
    for term in CORPUS["skills"][name]["description_terms"]:
        assert term in lowered, (name, term)


def test_the_corpus_consults_no_model() -> None:
    """Checked on imports, as the policy corpus beside it is."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(name.startswith(("anthropic", "openai", "repoatlas.llm")) for name in imported)
