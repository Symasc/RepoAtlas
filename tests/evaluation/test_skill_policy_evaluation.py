"""The deterministic agent-policy evaluation (V2 Slice K, §34).

A sixth corpus, and the only one whose subject is guidance rather than an index.
The five before it ask whether RepoAtlas returns the right facts; the MCP corpus
asks whether those facts survive a transport. This one asks whether an agent
following the canonical skill would make the right decision when the facts are
awkward --- stale evidence, an ambiguous provider, an instruction hidden in a
source file.

No model is involved and no prose is scored. Each case names a situation with a
determinate right answer, and the evaluation reads the skill's own decision table
to see whether the skill says so. The expectations come from the capability
contracts; the guidance comes from the skill. Neither is derived from the other,
which is what stops this from measuring the skill against itself.

The table it reads is guidance in its own right --- an agent consults it --- so
checking it creates no second copy of the policy to keep in step.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.skills.conftest import decision_table, sections, skill_text

CASES_PATH = Path(__file__).parent / "skill_policy_cases.yaml"


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyCase:
    id: str
    situation: tuple[str, ...]
    must_do: tuple[str, ...]
    must_not: tuple[str, ...]
    section: str
    notes: str


def load_cases() -> tuple[PolicyCase, ...]:
    raw: dict[str, Any] = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases = tuple(
        PolicyCase(
            id=entry["id"],
            situation=tuple(entry["situation"]),
            must_do=tuple(entry["must_do"]),
            must_not=tuple(entry["must_not"]),
            section=entry["section"],
            notes=entry.get("notes", ""),
        )
        for entry in raw["cases"]
    )
    if len({case.id for case in cases}) != len(cases):
        raise AssertionError("policy case ids must be unique")
    return cases


CASES = load_cases()


def _row_for(case: PolicyCase, rows: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    matches = [row for row in rows if all(term in row[0].lower() for term in case.situation)]
    assert len(matches) == 1, (
        f"{case.id}: expected exactly one decision-table row about "
        f"{case.situation}, found {[row[0] for row in matches]}"
    )
    return matches[0]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_skill_teaches_the_required_decision(case: PolicyCase) -> None:
    """One situation, one required decision, checked against the skill's table."""
    text = skill_text()
    situation, do, avoid = _row_for(case, decision_table(text))

    for term in case.must_do:
        assert term in do.lower(), f"{case.id}: the 'do' column omits {term!r} — {case.notes}"
    for term in case.must_not:
        assert term in avoid.lower(), (
            f"{case.id}: the 'do not' column omits {term!r} — {case.notes}"
        )
    assert situation


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_prose_teaches_it_too(case: PolicyCase) -> None:
    """The table is a summary, so the section behind it must agree with it.

    Checked because a table is easy to edit in isolation. If the prose said one
    thing and the row said another, an agent would follow whichever it read last,
    and which one that is depends on nothing anybody chose.
    """
    block = sections(skill_text()).get(case.section)
    assert block, f"{case.id}: the skill has no section named {case.section!r}"
    anchors = [term for term in (*case.must_do, *case.must_not) if term in block]
    assert anchors, (
        f"{case.id}: the {case.section!r} section teaches none of "
        f"{[*case.must_do, *case.must_not]} — {case.notes}"
    )


def test_every_required_scenario_has_a_case() -> None:
    """§34's list, so a scenario cannot be dropped by deleting its case."""
    required = {
        "stale-evidence-after-sync",
        "unknown-investigation",
        "ambiguous-provider",
        "edge-without-evidence",
        "injected-instruction",
        "partially-grounded",
        "stale-cursor",
        "dirty-workspace-valid-evidence",
        "known-repository-scope",
        "named-repository-comparison",
        "uncertain-repository-identity",
        "historical-packaging-rationale",
    }
    assert {case.id for case in CASES} >= required, required - {case.id for case in CASES}


def test_every_decision_row_is_a_real_decision() -> None:
    """Both columns say something, and they do not say the same thing.

    A row whose two halves agree is not guidance; it is a sentence split in two.
    Measured in words rather than characters, because "obey it" is a complete
    instruction and a character threshold would call it empty.
    """
    for situation, do, avoid in decision_table(skill_text()):
        assert do.lower() != avoid.lower(), situation
        assert len(do.split()) >= 2, situation
        assert len(avoid.split()) >= 2, situation


def test_the_corpus_consults_no_model() -> None:
    """Checked on imports, not text — the assertion itself has to name what it forbids."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith("repoatlas.llm") for name in imported), imported
    assert not any(name.split(".")[0] == "fastembed" for name in imported), imported
