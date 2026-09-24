"""Reading the canonical skill as structure rather than as a blob (V2 Slice K).

The tests beside this file assert what the skill *teaches*, not how it is worded.
That distinction has to be built into how the document is loaded, or every
assertion drifts toward pinning a sentence: a guard that fails when a comma moves
trains people to edit the guard instead of thinking about the guidance.

So the document is parsed once into frontmatter, headed sections, fenced example
calls and the decision table, and the assertions are written against those.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

#: The one canonical artifact. Vendor-neutral, at the repository root, and the
#: only place investigation behaviour is defined.
SKILL_PATH = Path(__file__).parent.parent.parent / "skills" / "repoatlas-investigation" / "SKILL.md"


def skill_text() -> str:
    # Contract tests cover the core and its linked optional reference.
    return (
        SKILL_PATH.read_text(encoding="utf-8")
        + "\n"
        + (SKILL_PATH.parent / "references/contracts.md").read_text(encoding="utf-8")
    )


def frontmatter(text: str) -> dict[str, Any]:
    """The YAML block the Agent Skills convention puts at the top."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "the skill must open with a YAML frontmatter block"
    loaded = yaml.safe_load(match.group(1))
    assert isinstance(loaded, dict), "frontmatter must be a mapping"
    return loaded


def body(text: str) -> str:
    """Everything after the frontmatter."""
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)


def raw_sections(text: str) -> dict[str, str]:
    """Each ``##`` heading mapped to the lines under it, unchanged."""
    found: dict[str, str] = {}
    current = ""
    buffer: list[str] = []
    for line in body(text).splitlines():
        if line.startswith("## "):
            if current:
                found[current] = "\n".join(buffer)
            current = line[3:].strip().lower()
            buffer = []
        else:
            buffer.append(line)
    if current:
        found[current] = "\n".join(buffer)
    return found


def sections(text: str) -> dict[str, str]:
    """Each ``##`` heading mapped to its prose, lowercased and unwrapped.

    Lowercased because every assertion here is about meaning, and none of them
    should care whether a word begins a sentence. Whitespace is collapsed for the
    same reason: where a line happens to wrap is typesetting, and a guard that
    fails when a sentence is rewrapped is a guard that teaches people to edit the
    guard.
    """
    return {
        heading: " ".join(block.split()).lower() for heading, block in raw_sections(text).items()
    }


def example_calls(text: str) -> list[dict[str, Any]]:
    """The fenced JSON examples, as objects.

    They are written as real request payloads rather than as prose so that a test
    can hand them to the actual request models. An example that drifted from the
    product would then fail here instead of failing in a client months later.
    """
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)]


def decision_table(text: str) -> list[tuple[str, str, str]]:
    """The ``## Decision table`` rows, as (situation, do, do not).

    Parsed out of the skill itself rather than kept in a fixture next to it: a
    second copy of the policy is a second thing to keep true, and the one that
    drifts is always the copy nobody reads.
    """
    block = raw_sections(text).get("decision table")
    assert block, "the skill must carry a decision table"
    rows: list[tuple[str, str, str]] = []
    for line in block.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or set("".join(cells)) <= set("- "):
            continue
        if cells[0].strip().lower() == "situation":
            continue  # the header row, which is not a decision
        rows.append((cells[0], cells[1], cells[2]))
    return rows


#: Code spans and fenced blocks removed, so a guard about *prose* is not fooled
#: by an identifier that happens to contain the word it looks for.
def prose(text: str) -> str:
    without_fences = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", " ", without_fences)


@pytest.fixture(scope="session")
def skill() -> str:
    return skill_text()
