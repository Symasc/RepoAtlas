"""What a ``setup.cfg`` records about its project (repoatlas.parsers.setuptools_project).

The shapes are the ones real setuptools-era providers write (plan §2): a ``src/`` layout by
``find:`` and a one-line or multi-line ``package_dir``, ``py_modules`` under ``src/``,
``find_namespace:``, a root ``find:`` with ``exclude``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.dependencies.python import SetupCfgParser
from repoatlas.models.parsing import EntityType
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_SETUPTOOLS
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.parsers.setuptools_project import SetuptoolsProjectParser, cfg_places
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build


def facts(content: str) -> dict[str, object] | None:
    parsed = SetuptoolsProjectParser().parse(Path("setup.cfg"), content)
    assert not parsed.symbols and not parsed.diagnostics
    if not parsed.references:
        return None
    [reference] = parsed.references
    assert reference.target_kind is EntityType.CONFIG
    assert reference.target_key == f"module-resolution:{PYTHON_SETUPTOOLS}"
    assert reference.meta[MODULE_RESOLUTION] == PYTHON_SETUPTOOLS
    return json.loads(reference.meta["facts"])


def test_a_src_layout_found_where_package_dir_roots_it() -> None:
    content = (
        "[metadata]\nname = Fixture-Auth\nversion = attr: fixture_auth.__version__\n\n"
        "[options]\npackages = find:\npackage_dir = = src\n\n"
        "[options.packages.find]\nwhere = src\n\n[flake8]\nmax-line-length = 80\n"
    )
    assert facts(content) == {
        "metadata": True,
        "name": "Fixture-Auth",
        "layout": {"root": "src", "packages": "find", "include": [], "exclude": []},
    }


def test_a_multi_line_package_dir_and_a_namespace_scan() -> None:
    content = (
        "[metadata]\nname = fixture-auth\n\n[options]\npackage_dir =\n    = src\n"
        "packages = find_namespace:\n\n[options.packages.find]\nwhere = src\n"
    )
    assert facts(content)["layout"] == {  # type: ignore[index]
        "root": "src",
        "packages": "find_namespace",
        "include": [],
        "exclude": [],
    }


def test_modules_under_src() -> None:
    content = (
        "[metadata]\nname = fixture-mail\n\n[options]\npackage_dir =\n    = src\n"
        "py_modules =\n    fixture_mail\n"
    )
    assert facts(content)["layout"] == {"root": "src", "py_modules": ["fixture_mail"]}  # type: ignore[index]


def test_a_root_scan_with_exclusions_and_a_package_list() -> None:
    scan = (
        "[metadata]\nname = Zebra-Tools\n\n[options]\npackages = find:\n\n"
        "[options.packages.find]\nexclude =\n    tests\n    tests.*\n"
    )
    assert facts(scan)["layout"] == {  # type: ignore[index]
        "root": "",
        "packages": "find",
        "include": [],
        "exclude": ["tests", "tests.*"],
    }
    listed = "[metadata]\nname = x\n[options]\npackages = alpha, alpha.sub\n"
    assert facts(listed)["layout"] == {"root": "", "packages": ["alpha", "alpha.sub"]}  # type: ignore[index]


def test_a_name_alone_states_no_layout() -> None:
    assert facts("[metadata]\nname = fixture-auth\n") == {"metadata": True, "name": "fixture-auth"}


@pytest.mark.parametrize(
    ("content", "unread"),
    [
        ("[metadata]\nName = fixture-auth\n", ["metadata.name"]),  # setuptools would lowercase it
        ("[metadata]\nname = %(x)s\n", ["metadata.name"]),
        ("[metadata]\nname = attr: pkg.NAME\n", ["metadata.name"]),
        ("[options]\npackage-dir = = src\npackages = find:\n", ["options.layout"]),
        ("[options]\npackage_dir = pkg = lib\npackages = find:\n", ["options.layout"]),
        ("[options]\npackage_dir = = ../elsewhere\npy_modules = m\n", ["options.layout"]),
        # where must be the package_dir root, as setuptools' own documentation requires
        ("[options]\npackage_dir = = src\npackages = find:\n", ["options.layout"]),
        (
            "[options]\npackages = find:\n[options.packages.find]\nwhere = src\n",
            ["options.layout"],
        ),
        ("[options]\npy_modules = pkg.mod\n", ["options.layout"]),
        ("[options]\npackages = attr: pkg.PACKAGES\n", ["options.layout"]),
    ],
)
def test_a_field_in_a_shape_not_read_is_named_not_guessed(content: str, unread: list[str]) -> None:
    found = facts(content)
    assert found is not None
    assert found.get("unread") == unread
    assert "name" not in found or unread != ["metadata.name"]
    assert "layout" not in found or unread != ["options.layout"]


def test_tool_settings_alone_are_no_metadata() -> None:
    assert facts("[flake8]\nmax-line-length = 80\n[tool:pytest]\ntestpaths = tests\n") is None
    assert facts("[build_sphinx]\nsource-dir = docs/\n") is None


def test_an_invalid_file_records_nothing() -> None:
    assert facts("name = orphan\n[metadata]\n") is None
    assert facts("[metadata]\nname = a\nname = b\n") is None  # strict, as setuptools reads it


def test_the_registry_routes_only_setup_cfg_to_it() -> None:
    registry = ParserRegistry()
    assert isinstance(registry.for_path(Path("svc/setup.cfg")), SetuptoolsProjectParser)
    assert not isinstance(registry.for_path(Path("tox.ini")), SetuptoolsProjectParser)


def test_the_places_follow_configparser_not_the_text() -> None:
    content = (
        "# name = fixture-auth, the old spelling\n[tool:pytest]\nname = fixture-auth\n"
        "[metadata]\nversion = 1\n  name = fixture-auth\n; a comment\nname = fixture-auth\n"
    )
    places = cfg_places(content)
    assert places[("tool:pytest", "name")] == 3
    assert places[("metadata", "name")] == 8  # line 6 continues `version`


def test_the_provided_name_is_located_where_the_option_is_written() -> None:
    """SetupCfgParser 3: no text search -- a comment or another section spelling the same
    `name = ...` never moves the line (SM11)."""
    content = (
        "# name = Fixture-Auth\n[tool:pytest]\nname = Fixture-Auth\n\n"
        "[metadata]\nversion = 1.0\nname = Fixture-Auth\n"
    )
    report = SetupCfgParser().parse(Path("setup.cfg"), content)
    [provided] = report.provides
    assert (provided.name, provided.line) == ("Fixture-Auth", 7)
    tight = SetupCfgParser().parse(Path("setup.cfg"), "[metadata]\nversion = 1\nname=tight\n")
    assert [(p.name, p.line) for p in tight.provides] == [("tight", 3)]
    inherited = SetupCfgParser().parse(Path("setup.cfg"), "[DEFAULT]\nname = shared\n[metadata]\n")
    assert [(p.name, p.line) for p in inherited.provides] == [("shared", 2)]


def test_indexing_records_the_fact_and_nothing_a_reader_sees_otherwise(
    settings: Settings, workspace: Workspace, tmp_path: Path
) -> None:
    """One claim, no symbols, no new language for the repository, and chunks as before."""
    build(
        workspace,
        tmp_path,
        {
            "demo": {
                "setup.cfg": "[metadata]\nname = demo\n\n[options]\npackages = find:\n",
                "demo/__init__.py": "",
            }
        },
    )
    claims = workspace.knowledge.list_module_facts()
    assert [json.loads(c.meta_json or "{}")[MODULE_RESOLUTION] for c in claims] == [
        PYTHON_SETUPTOOLS
    ]
    entities = workspace.database.execute(
        "SELECT e.type FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE f.rel_path = 'setup.cfg'"
    ).fetchall()
    assert [row["type"] for row in entities] == ["file"]
    languages = workspace.database.execute("SELECT languages_json FROM repositories").fetchone()[0]
    assert json.loads(languages) == ["Python"]
    chunks = workspace.database.execute(
        "SELECT c.text FROM chunks c JOIN files f ON f.id = c.file_id "
        "WHERE f.rel_path = 'setup.cfg'"
    ).fetchall()
    assert "".join(row[0] for row in chunks).count("name = demo") == 1
