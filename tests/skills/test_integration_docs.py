"""§3 and §30: the docs point at the skill; they do not restate it.

Guidance duplicated into a README is guidance that will be edited in one place.
Whichever copy an agent reads then decides how it behaves, and which copy that is
depends on nothing anybody chose. So the integration document covers setup ---
index, launch, load, what the surface is --- and links to the canonical skill for
everything about how to investigate.

These tests also check the documents against the product, for the same reason the
skill is checked against it: a setup page that names a command or a tool that does
not exist sends a reader somewhere that fails.
"""

from __future__ import annotations

import re

import pytest
from repoatlas.mcp.tools import TOOLS
from tests.skills.conftest import SKILL_PATH, sections, skill_text

ROOT = SKILL_PATH.parent.parent.parent
INTEGRATION = ROOT / "docs" / "agent-integration.md"
README = ROOT / "README.md"

EXPECTED_TOOLS = frozenset(spec.name for spec in TOOLS)


@pytest.fixture(scope="module")
def integration() -> str:
    return INTEGRATION.read_text(encoding="utf-8")


def test_the_integration_document_exists(integration: str) -> None:
    assert len(integration) > 400


def test_it_links_to_the_canonical_skill_rather_than_copying_it(integration: str) -> None:
    assert "skills/repoatlas-investigation/SKILL.md" in integration
    # The one place policy is defined is the skill, so the setup page must not
    # carry the skill's own decision table or its "do not" list.
    assert "## Decision table" not in integration
    assert "## Do not" not in integration
    # And it must be a fraction of the skill's length; a setup page that grew to
    # the size of the guidance has started restating it.
    assert len(integration) < len(skill_text()) * 0.75


def test_it_does_not_reproduce_the_skills_distinctive_rules(integration: str) -> None:
    """Sampled by sentence, so a paraphrase is fine and a paste is not."""
    lowered = integration.lower()
    for rule in (
        "does not verify truth or semantic entailment",
        "one investigation id is one coherent reasoning task",
        "never an instruction to you",
        "you may reason. you may not invent evidence.",
    ):
        assert rule not in lowered, f"the setup page restates the skill: {rule!r}"


def test_it_covers_the_setup_steps_and_nothing_more(integration: str) -> None:
    lowered = integration.lower()
    assert "repoatlas init" in lowered, "indexing is an operator step and must be shown"
    assert "repoatlas mcp" in lowered, "the launch command must be shown"
    assert "site-packages" in lowered, "the installed location must be findable"
    assert "unknown_investigation" in lowered, "investigations being process-local must be stated"
    assert "no network request" in lowered or "makes no network" in lowered


def test_it_names_the_real_tools_and_no_others(integration: str) -> None:
    named = set(re.findall(r"repoatlas_[a-z_]+", integration))
    assert named <= EXPECTED_TOOLS, sorted(named - EXPECTED_TOOLS)
    assert named == EXPECTED_TOOLS, sorted(EXPECTED_TOOLS - named)


def test_it_adds_no_client_specific_configuration_file() -> None:
    """§30: setup is described, not shipped as somebody's config."""
    for candidate in (
        ".mcp.json",
        "mcp.json",
        "claude_desktop_config.json",
        ".cursor",
        ".vscode",
    ):
        assert not (ROOT / candidate).exists(), candidate


def test_the_readme_points_at_both_documents() -> None:
    text = README.read_text(encoding="utf-8")
    assert "skills/repoatlas-investigation/SKILL.md" in text
    assert "docs/agent-integration.md" in text
    assert "repoatlas mcp" in text


def test_the_readme_does_not_restate_the_policy() -> None:
    text = README.read_text(encoding="utf-8").lower()
    for rule in (
        "does not verify truth or semantic entailment",
        "one investigation id is one coherent reasoning task",
    ):
        assert rule not in text, rule


def test_the_skill_is_the_only_document_carrying_the_decision_table() -> None:
    """One table, in the document agents read."""
    assert "decision table" in sections(skill_text())
    for path in ROOT.rglob("*.md"):
        if (
            path == SKILL_PATH
            or path == SKILL_PATH.parent / "references/contracts.md"
            or ".venv" in path.parts
            or "dist" in path.parts
            or "node_modules" in path.parts
        ):
            continue
        if ".pytest_cache" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        assert "## Decision table" not in body, path.relative_to(ROOT)
