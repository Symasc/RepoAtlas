"""Facts read with the interpreter's own standard library carry that interpreter's version.

``ast`` and ``tomllib`` belong to whichever interpreter runs RepoAtlas, and what they
accept moves with it (:mod:`repoatlas.parsers.interpreter`). An index written by one
interpreter and synced by another must reparse, never keep facts the running one would
not produce.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.dependencies.python import PyProjectParser, SetupPyParser, UvLockParser
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.python_project import PythonProjectParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version
from tests.impact_corpus import build

RUNNING = f"py{sys.version_info.major}.{sys.version_info.minor}"
#: A tag no interpreter running this suite has: the index "another interpreter" wrote.
ELSEWHERE = "py2.7"
#: Every parser that reads with the interpreter's standard library.
TAGGED = (PythonParser, PythonProjectParser, SetupPyParser, PyProjectParser, UvLockParser)


def test_the_tag_is_the_running_major_and_minor() -> None:
    assert interpreter_tag() == RUNNING


@pytest.mark.parametrize("parser", TAGGED)
def test_every_standard_library_parser_carries_the_tag(parser: type) -> None:
    assert parser.version.endswith(f"+{RUNNING}"), parser.version


def test_the_facts_version_moves_with_the_interpreter(workspace: Workspace) -> None:
    live = facts_version(workspace.registry, workspace.dependency_registry)
    for parser in TAGGED:
        assert f"{parser.name}:{parser.version}" in live, parser.name
    assert live.count(f"+{RUNNING}") == len(TAGGED), live


def test_an_index_another_interpreter_wrote_is_reparsed(
    settings: Settings, workspace: Workspace, tmp_path: Path
) -> None:
    """Every file stored under another interpreter's facts version is parsed again.

    The facts version is one string for the whole registry, so every file is, not
    only the Python ones: the granularity every parser version change already has.
    """
    build(
        workspace,
        tmp_path,
        {
            "demo": {
                "pkg/__init__.py": "",
                "pkg/app.py": "def run() -> int:\n    return 1\n",
                "pyproject.toml": '[project]\nname = "demo"\nversion = "1.0"\n',
                "notes.md": "# notes\n",
            }
        },
    )
    live = facts_version(workspace.registry, workspace.dependency_registry)
    workspace.database.execute(
        "UPDATE files SET parser_version = ?", (live.replace(f"+{RUNNING}", f"+{ELSEWHERE}"),)
    )
    workspace.database.connection.commit()

    root = tmp_path / "repos"
    report = sync(workspace, workspace.resolver().resolve(root))
    total = workspace.database.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert report.repositories[0].changed == total, report

    again = sync(workspace, workspace.resolver().resolve(root))
    assert again.repositories[0].changed == 0, again
