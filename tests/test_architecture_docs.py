"""ARCHITECTURE.md describes the product that ships (F-05).

The document outlived a subsystem. When the embedded reasoner was deleted, §11
was rewritten and the package map corrected, but four passages elsewhere went on
describing ``ask``, ``debug`` and an ``llm/`` package as current --- including
the §1 layering diagram, which drew a box for a directory that no longer exists,
and the §13 test matrix, which listed a test layer two paragraphs above prose
saying there was no longer anything to stub.

Stale architecture documentation is not cosmetic. §1 is the description a
newcomer reads to learn what may import what, and it is the only place the
capability and transport layers' position was not written down at all.

The guards here are deliberately narrow, and two of the three are structural
rather than textual:

* the packages named in the §1 diagram must exist on disk --- which is what
  would have caught ``llm/`` the day it was deleted, with no prose to maintain;
* the commands the document presents as available must be the commands the CLI
  actually registers;
* and the specific corrected sentences must not come back.

What is **not** guarded is the word "ask" or the word "debug". English prose
asks questions, §15 is an append-only decision log whose rows describe what was
true when they were written, and the evaluation dataset keeps old labels on
purpose so baselines stay comparable. A guard that forbade the strings outright
would be satisfied by damaging the record.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
PACKAGE = ROOT / "repoatlas"

#: Sections that record history and are correct to mention removed features.
#: §15 is append-only by construction: a row says what was decided at the time,
#: and a later reversal is a new row rather than an edit to the old one.
HISTORICAL_SECTIONS = frozenset({"15"})

#: Things that must never be presented as available functionality.
RETIRED_SURFACES = (
    "repoatlas ask",
    "repoatlas debug",
    "--no-llm",
    "ollama",
)


@pytest.fixture(scope="module")
def document() -> str:
    return ARCHITECTURE.read_text(encoding="utf-8")


def _sections(text: str) -> dict[str, str]:
    """Split on the numbered top-level headings, keyed by section number."""
    sections: dict[str, str] = {}
    current = None
    body: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^## (\d+)\. ", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(body)
            current = heading.group(1)
            body = [line]
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body)
    return sections


def test_the_document_and_its_numbered_sections_are_readable(document: str) -> None:
    sections = _sections(document)
    assert ARCHITECTURE.exists()
    # If the split silently stopped working every other guard here would pass
    # vacuously, which is the failure mode a document test is most prone to.
    assert len(sections) >= 15
    assert "1" in sections and "15" in sections


def test_the_layering_diagram_names_only_packages_that_exist(document: str) -> None:
    """The structural half of F-05, and the one that needs no maintenance.

    ``llm/`` sat in this diagram after the directory was deleted. Nothing caught
    it because nothing was comparing the picture to the tree.
    """
    section = _sections(document)["1"]
    block = re.search(r"```\n(.*?)```", section, re.DOTALL)
    assert block, "§1 still contains a fenced layering diagram"

    named = sorted(set(re.findall(r"\b([a-z][a-z_]*)/", block.group(1))))
    assert named, "the diagram names at least one package"

    missing = [name for name in named if not (PACKAGE / name).is_dir()]
    assert not missing, f"§1 diagram names packages that do not exist: {missing}"


def test_the_layering_diagram_shows_the_layers_an_agent_talks_to(document: str) -> None:
    """The two packages that constitute V2 are in the picture, not only in prose."""
    block = re.search(r"```\n(.*?)```", _sections(document)["1"], re.DOTALL)
    assert block
    diagram = block.group(1)
    for package in ("capabilities/", "mcp/", "api/", "services/", "storage/", "models/"):
        assert package in diagram, f"§1 diagram omits {package}"


def test_no_current_section_presents_a_retired_surface_as_available(document: str) -> None:
    """Scoped to the current-product sections; §15 is exempt and stays truthful."""
    offenders: list[str] = []
    for number, body in _sections(document).items():
        if number in HISTORICAL_SECTIONS:
            continue
        for line_number, line in enumerate(body.splitlines(), start=1):
            lowered = line.lower()
            for surface in RETIRED_SURFACES:
                if surface in lowered:
                    offenders.append(f"§{number} line {line_number}: {line.strip()[:110]}")
    assert not offenders, offenders


def test_the_corrected_passages_do_not_return(document: str) -> None:
    """The exact sentences F-05 replaced, named so a revert is loud."""
    stale = (
        "`locate`, `status`, `repos` and `ask` run",
        '`ask "why does src/config.py call X?"`',
        "**The LLM layer never touches storage.**",
        "Parsers never touch storage and never touch the LLM.",
        "| LLM | `ScriptedBackend` only.",
    )
    present = [passage for passage in stale if passage in document]
    assert not present, present


def test_the_document_describes_the_commands_the_cli_registers(document: str) -> None:
    """A document naming a command that does not exist sends a reader nowhere."""
    from repoatlas.cli.main import app

    registered: set[str] = set()
    for command in app.registered_commands:
        callback = command.callback
        assert callback is not None, "every registered command has a callback"
        registered.add(command.name or callback.__name__)
    assert registered == {"init", "sync", "status", "repos", "locate", "mcp", "serve"}

    # And the reverse direction: nothing removed is offered as a command.
    for retired in ("ask", "debug"):
        assert retired not in registered


def test_the_decision_log_keeps_its_record_of_the_removal(document: str) -> None:
    """The guard above must not be satisfiable by deleting history.

    §15 is where ``ask``, ``debug`` and Ollama are *supposed* to appear: the row
    recording why they went is the part worth keeping, and a future guard that
    tidied it away would be removing the reasoning rather than the feature.
    """
    log = _sections(document)["15"]
    assert "ask" in log and "debug" in log
    assert "ollama" in log.lower()
