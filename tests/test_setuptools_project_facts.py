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
from repoatlas.parsers.python import PythonParser
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


def declared_lines(content: str) -> list[tuple[str, str, int]]:
    report = SetupCfgParser().parse(Path("setup.cfg"), content)
    return [(d.name, d.group, d.line) for d in report.declared]


def test_each_requirement_is_located_on_its_own_value_line() -> None:
    """SetupCfgParser 4 (D2): the entry under `install_requires`, never an earlier mention of
    the same text in `description`, `keywords` or a comment (CM3)."""
    content = (
        "[metadata]\nname = fixture-client\ndescription = fixture-auth integration\n"
        "keywords = fixture-auth\n# fixture-auth>=1 was here\n\n"
        "[options]\npackages = find:\ninstall_requires =\n    fixture-auth>=1\n"
    )
    assert declared_lines(content) == [("fixture-auth", "main", 10)]


def test_overlapping_names_each_keep_their_own_line() -> None:
    """D3: `fixture-auth` is inside `fixture-auth-extra`; each row is its own line."""
    content = (
        "[options]\ninstall_requires =\n    fixture-auth-extra\n    fixture-auth\n"
        "tests_require =\n    fixture-auth\n"
    )
    assert declared_lines(content) == [
        ("fixture-auth-extra", "main", 3),
        ("fixture-auth", "main", 4),
        ("fixture-auth", "test", 6),
    ]


def test_comments_blank_lines_tabs_and_a_one_line_value_keep_exact_lines() -> None:
    """D20: full-line comments and blank lines inside the list are skipped as configparser
    skips them, a tab indents like spaces, and a one-line value is on its option's line."""
    content = (
        "[options]\ninstall_requires =\n    # the web framework\n    fixture-web>=2\n\n"
        "\tfixture-auth ; python_version < '3.12'\n    ; retired\n    fixture-db\n"
        "setup_requires = fixture-build\n"
    )
    assert declared_lines(content) == [
        ("fixture-web", "main", 4),
        ("fixture-auth", "main", 6),
        ("fixture-db", "main", 8),
        ("fixture-build", "build", 9),
    ]


def test_a_value_the_syntax_does_not_give_verbatim_is_placed_on_its_option() -> None:
    """Where configparser's value differs from the written lines (an interpolation), each
    row takes the option's own line: still where it is written, never a text search."""
    content = (
        "[metadata]\nname = fixture-auth\n\n[options]\nbase = fixture-auth\n"
        "install_requires =\n    %(base)s>=1\n    fixture-db\n"
    )
    assert declared_lines(content) == [("fixture-auth", "main", 6), ("fixture-db", "main", 6)]


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


# -- setup.py: its one static setup() call, read with ast and never run (C2) ------------


def py_facts(content: str, name: str = "setup.py") -> dict[str, object] | None:
    parsed = PythonParser().parse(Path(name), content)
    found = [r for r in parsed.references if r.meta.get(MODULE_RESOLUTION) == PYTHON_SETUPTOOLS]
    if not found:
        return None
    [reference] = found
    return json.loads(reference.meta["facts"])


SETUP = "from setuptools import setup, find_packages\n\n"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ('setup(name="x")', {"name": "x"}),
        (
            'setup(name="x", package_dir={"": "src"})',
            {"name": "x", "layout": {"root": "src"}},
        ),
        ('setup(py_modules=["x"])', {"layout": {"root": "", "py_modules": ["x"]}}),
        (
            'setup(packages=["x", "x.sub"])',
            {"layout": {"root": "", "packages": ["x", "x.sub"]}},
        ),
        (
            'setup(package_dir={"": "src"}, packages=find_packages("src"))',
            {"layout": {"root": "src", "packages": "find", "include": [], "exclude": []}},
        ),
        (
            'setup(package_dir={"": "src"}, packages=find_packages(where="src", exclude=["t*"]))',
            {"layout": {"root": "src", "packages": "find", "include": [], "exclude": ["t*"]}},
        ),
        ("setup()", {}),  # the shim: a setup.cfg states the project
    ],
)
def test_a_literal_setup_call_is_read(call: str, expected: dict[str, object]) -> None:
    assert py_facts(SETUP + call + "\n") == {"metadata": True, **expected}


@pytest.mark.parametrize(
    ("source", "unread"),
    [
        ('NAME = "x"\nsetup(name=NAME)', ["setup.name"]),  # no constant propagation
        ('suffix = "a"\nsetup(name=f"x-{suffix}")', ["setup.name"]),
        ("setup(name=get_name())", ["setup.name"]),
        ("config = {}\nsetup(**config)", ["setup.layout", "setup.name"]),
        ('setup(name="x", package_dir=get_package_dir())', ["setup.layout"]),
        ('EXCL = ["t"]\nsetup(name="x", packages=find_packages(exclude=EXCL))', ["setup.layout"]),
        # find_packages("src") with no package_dir: the build would look for them at the root
        ('setup(name="x", packages=find_packages("src"))', ["setup.layout"]),
        ('setup(name="x", package_dir={"pkg": "lib"}, packages=["pkg"])', ["setup.layout"]),
        ('if cond:\n    setup(name="x")', ["setup.name"]),
        ('if __name__ == "__main__":\n    setup(name="x")', ["setup.name"]),
        ('setup(name="x")\nsetup(name="y")', ["setup.name"]),
    ],
)
def test_anything_setup_would_compute_is_unread(source: str, unread: list[str]) -> None:
    found = py_facts(SETUP + source + "\n")
    assert found is not None and found.get("unread") == unread
    assert ("name" not in found and "layout" not in found) or unread == ["setup.layout"]


def test_the_attribute_form_and_a_setup_inside_a_function() -> None:
    assert py_facts("import setuptools\n\nsetuptools.setup()\n") == {"metadata": True}
    # markupsafe: setup() with only build keywords, inside a function, states nothing
    source = SETUP + "def run(binary):\n    setup(cmdclass={}, ext_modules=[])\n\nrun(True)\n"
    assert py_facts(source) == {"metadata": True}
    aliased = 'import setuptools as st\n\nst.setup(name="x", py_modules=["x"])\n'
    assert py_facts(aliased) == {
        "metadata": True,
        "name": "x",
        "layout": {"root": "", "py_modules": ["x"]},
    }


def test_no_setuptools_setup_is_no_metadata() -> None:
    assert py_facts("import django\n\ndjango.setup()\n") is None
    assert py_facts('print("setup")\n') is None
    assert py_facts(SETUP + 'setup(name="x")\n', name="build.py") is None


def test_an_unbound_setup_stating_a_name_makes_the_bound_one_unreadable() -> None:
    source = SETUP + 'setup(py_modules=["x"])\nother.setup(name="y")\n'
    assert py_facts(source) == {"metadata": True, "unread": ["setup.layout", "setup.name"]}
