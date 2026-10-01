"""The code-trace trigger corpus: graph-shaped investigation against ordinary coding.

The second trigger benchmark, built after the first measured the generic investigation
skill 0 of 5. It is fresh: no prompt or id comes from the first corpus, which stays
history. The file is pinned by hash from the commit that froze it, before any paid run,
so it cannot be tuned against its own results.

Which skill a real client loads needs a model and is measured by the paid harness. What
can be checked without one is checked here: the corpus is well formed, it holds enough
cases of each kind on the boundaries that matter, no prompt names RepoAtlas, and the
descriptions still carry the vocabulary the cases route on, for and against.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.mcp.tools import TOOLS
from tests.skills.conftest import SKILL_NAMES, frontmatter, skill_paths

HERE = Path(__file__).parent
CORPUS_PATH = HERE / "code_trace_trigger_cases.yaml"
FIRST_CORPUS_PATH = HERE / "skill_trigger_cases.yaml"
TOOL_NAMES = frozenset(spec.name for spec in TOOLS)
TRACE = "repoatlas-code-trace"
NONE = "none"

#: Pinned when the corpus was frozen, before any paid run.
FROZEN_SHA256 = "7954642fe5427030a2cf78e9d99907d63b6b39660876b0686febfd2d94129319"

#: What each kind of case expects.
EXPECT = {
    "trace": {TRACE},
    "local": {NONE},
    "neighbour": {"repoatlas-change-impact", "repoatlas-dependencies", "repoatlas-http"},
}

CASES: list[dict[str, Any]] = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))["cases"]


def of_kind(kind: str) -> list[dict[str, Any]]:
    return [case for case in CASES if case["kind"] == kind]


def normal(prompt: str) -> str:
    return " ".join(prompt.split()).lower()


def test_the_corpus_is_the_one_that_was_frozen() -> None:
    digest = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
    assert digest == FROZEN_SHA256, "this corpus is frozen; build a new one instead"


def test_each_kind_has_enough_cases() -> None:
    assert len(of_kind("trace")) >= 10
    assert len(of_kind("local")) >= 8
    neighbours = of_kind("neighbour")
    for skill in EXPECT["neighbour"]:
        assert sum(case["expect"] == skill for case in neighbours) >= 2, skill


def test_ids_and_prompts_are_unique_and_new() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)), ids
    prompts = [normal(case["prompt"]) for case in CASES]
    assert len(prompts) == len(set(prompts))
    first = yaml.safe_load(FIRST_CORPUS_PATH.read_text(encoding="utf-8"))["cases"]
    assert not set(ids) & {case["id"] for case in first}
    assert not set(prompts) & {normal(case["prompt"]) for case in first}


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_each_case_is_well_formed(case: dict[str, Any]) -> None:
    assert set(case) <= {"id", "kind", "prompt", "expect", "first_tool", "forbid_tools"}
    assert case["expect"] in EXPECT[case["kind"]], case["expect"]
    assert case["expect"] == NONE or case["expect"] in SKILL_NAMES
    for tool in (*case.get("first_tool", ()), *case.get("forbid_tools", ())):
        assert tool in TOOL_NAMES, tool
    assert "repoatlas_sync" in case["forbid_tools"]
    assert "repoatlas_impact" not in case.get("first_tool", ()) or case["kind"] == "neighbour"


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_no_prompt_names_repoatlas(case: dict[str, Any]) -> None:
    """Not the product, a skill or a tool: a request that says which to use measures
    reading, not routing."""
    assert not re.search(r"repo\s*atlas", case["prompt"], flags=re.IGNORECASE), case["prompt"]


def test_the_code_trace_description_claims_the_trace_cases_and_disclaims_the_local_ones() -> None:
    description = frontmatter(skill_paths()[TRACE].read_text(encoding="utf-8"))["description"]
    lowered = description.lower()
    for claimed in (
        "trace",
        "defined",
        "calls",
        "imports",
        "connects",
        "entrypoint",
        "services",
        "repositories",
    ):
        assert claimed in lowered, claimed
    for disclaimed in ("explaining one file", "small local edit", "language question"):
        assert disclaimed in lowered, disclaimed
    for neighbour in sorted(EXPECT["neighbour"]):
        assert neighbour in description, neighbour


def test_the_corpus_consults_no_model() -> None:
    """Checked on imports, as the corpora beside it are."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(name.startswith(("anthropic", "openai", "repoatlas.llm")) for name in imported)
