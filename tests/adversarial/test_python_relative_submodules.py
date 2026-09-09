"""``from . import helper`` names a module, not just its package (V1_SPEC §7.1).

Python writes one import two ways::

    from .helper import assist      # names the module
    from . import helper           # names the package, then the module in it

The second form is where ``ast`` puts nothing in ``ImportFrom.module``: the
module being imported is in ``names``, alongside ordinary symbol imports it is
indistinguishable from by syntax alone. RepoAtlas recorded ``node.module`` and
the level, which for this form is the bare prefix ``"."``, resolved that to the
package's ``__init__.py``, and kept ``helper`` only as a string in ``meta``.

The consequence was not a wrong edge — it was a **missing** one. ``pkg/sub/helper.py``
had no inbound ``IMPORTS`` at all, so "who depends on helper?" answered nothing
about the file that imports it on line 1, and graph expansion from that file
reached the package's ``__init__.py`` instead of the module.

**Restoring a missing edge is the dangerous direction.** The obvious repair —
treat every name after ``from . import`` as a module — trades an omission for a
fabrication, because that list also holds constants, functions and classes that
live in ``__init__.py`` and correspond to no file at all. So the parser only
proposes ``"." * level + name`` as a *candidate*, and the resolution index emits
an edge when a module of that name exists in that package and nothing when it
does not. Roughly half of this file is the negative half: the cases that must
still resolve to nothing.

The candidate carries one extra condition the written form does not: the dots
must themselves resolve to a package. `from . import helper` in a file that is
in no package is an ``ImportError`` at runtime, and without that check a
``helper.py`` sitting beside it would look like the answer.
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import ReposSettings, Settings
from repoatlas.models.parsing import EdgeType, ParsedFile
from repoatlas.parsers.python import PythonParser
from repoatlas.services.init_service import initialize
from repoatlas.services.query_service import locate
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.sync.planner import facts_version
from tests.conftest import git, init_git_repository

pytestmark = pytest.mark.adversarial


# -- harness ------------------------------------------------------------------


def parse(source: str, name: str = "worker.py") -> ParsedFile:
    return PythonParser().parse(Path(name), source)


def imports(source: str) -> dict[str, dict[str, str]]:
    """Every ``IMPORTS`` target the parser records, by target key."""
    return {
        reference.target_key: dict(reference.meta)
        for reference in parse(source).references
        if reference.edge_type is EdgeType.IMPORTS
    }


def import_lines(source: str) -> dict[str, int]:
    return {
        reference.target_key: reference.line_start
        for reference in parse(source).references
        if reference.edge_type is EdgeType.IMPORTS
    }


#: The package from §1, verbatim. Every end-to-end test starts from this shape.
PACKAGE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/app.py": "def run():\n    return 1\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/helper.py": "def assist():\n    return 2\n",
    "pkg/sub/worker.py": "from . import helper\nfrom .. import app\n",
}


def settings_for(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 64},
        }
    )


def open_at(settings: Settings, root: Path) -> Workspace:
    """A workspace whose managed root is ``root``.

    ``initialize`` persists the root into a config file; reopening from the same
    ``Settings`` object does not read it back, so a later ``sync`` would resolve
    no context at all.
    """
    return open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )


def make_repo(root: Path, name: str, tree: dict[str, str]) -> Path:
    repo = root / name
    for rel, content in tree.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def rewrite(repo: Path, rel: str, source: str) -> None:
    (repo / rel).write_text(source, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "rewrite")


def config_for(tmp_path: Path) -> Path:
    """Where ``initialize`` persists the managed root.

    Passed explicitly at every call. Without it ``initialize`` writes the
    operator's real ``~/.config/repoatlas/config.yaml`` — which
    ``test_no_test_touches_real_repoatlas_state`` exists to catch, and did.
    """
    return tmp_path / "config.yaml"


def indexed(tmp_path: Path, tree: dict[str, str], name: str = "demo") -> Workspace:
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    make_repo(root, name, tree)
    settings = settings_for(tmp_path)
    workspace = open_at(settings, root)
    initialize(workspace, root, config_path=config_for(tmp_path))
    return workspace


#: The Python parser version that recorded `from . import helper` as the bare
#: package prefix. An index written by it is what this pass has to upgrade.
LOSSY_PYTHON_VERSION = "2"


def previous_facts_version(workspace: Workspace) -> str:
    """The facts version an index written by :data:`LOSSY_PYTHON_VERSION` carries.

    Only the Python component is rewritten, and it is rewritten to a **literal**
    rather than to "one less than the current version". The difference matters:
    a relative offset would move with the parser and always differ from the live
    version, so the upgrade test would pass even with the bump deleted — which is
    precisely the failure it exists to catch. Everything else in the string is
    taken from the live registries, so this stays correct as the schema, the
    other parsers and the chunking rules move.
    """
    live = facts_version(workspace.registry, workspace.dependency_registry)
    current = f"{PythonParser.name}:{PythonParser.version}"
    assert current in live, live
    return live.replace(current, f"{PythonParser.name}:{LOSSY_PYTHON_VERSION}")


def rows(workspace: Workspace, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return list(workspace.database.execute(query, params).fetchall())


def importers_of(workspace: Workspace, repo: str, rel_path: str) -> set[tuple[str, int]]:
    """Every file that imports ``rel_path``, with the line it does so on.

    The product question — "who depends on this?" — asked of the graph exactly as
    retrieval asks it.
    """
    return {
        (row["src_path"], row["line_start"])
        for row in rows(
            workspace,
            "SELECT sf.rel_path AS src_path, e.line_start FROM edges e "
            "JOIN entities dst ON dst.id = e.dst_id "
            "JOIN files sf ON sf.id = e.file_id "
            "WHERE e.type = 'IMPORTS' AND dst.key = ?",
            (f"file:{repo}:{rel_path}",),
        )
    }


def imported_files(workspace: Workspace, rel_path: str) -> set[str]:
    """Every repository file ``rel_path`` imports."""
    return {
        row["dst_path"]
        for row in rows(
            workspace,
            "SELECT df.rel_path AS dst_path FROM edges e "
            "JOIN entities dst ON dst.id = e.dst_id "
            "JOIN files df ON df.id = dst.file_id "
            "JOIN files sf ON sf.id = e.file_id "
            "WHERE e.type = 'IMPORTS' AND dst.type = 'file' AND sf.rel_path = ?",
            (rel_path,),
        )
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    space = indexed(tmp_path, PACKAGE)
    yield space
    space.close()


def context_in(workspace: Workspace, tmp_path: Path, name: str = "demo"):
    """The repository context a command run inside the repository would resolve."""
    return workspace.resolver().resolve(tmp_path / "work" / name)


# == 1. the reproduction =====================================================


def test_an_imported_submodule_gets_the_inbound_import_edge(workspace: Workspace) -> None:
    """The finding: both files used to receive nothing at all."""
    assert importers_of(workspace, "demo", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
    assert importers_of(workspace, "demo", "pkg/app.py") == {("pkg/sub/worker.py", 2)}


def test_the_package_import_is_still_recorded(workspace: Workspace) -> None:
    """Python imports the package *and* the module in it, so both are facts.

    The repair adds the module; it does not replace the package. ``__init__.py``
    really is executed by ``from . import helper``, and a change that dropped
    that edge would fix an omission by creating another.
    """
    assert imported_files(workspace, "pkg/sub/worker.py") == {
        "pkg/sub/__init__.py",
        "pkg/sub/helper.py",
        "pkg/__init__.py",
        "pkg/app.py",
    }


# == 2. what `ast` actually says =============================================


@pytest.mark.parametrize(
    "source,module,level,names",
    [
        ("from . import helper", None, 1, [("helper", None)]),
        ("from .. import app", None, 2, [("app", None)]),
        ("from . import helper, utils", None, 1, [("helper", None), ("utils", None)]),
        ("from . import helper as h", None, 1, [("helper", "h")]),
        ("from .helper import X", "helper", 1, [("X", None)]),
        ("from ..app import X", "app", 2, [("X", None)]),
        ("from pkg.sub import helper", "pkg.sub", 0, [("helper", None)]),
        ("from . import *", None, 1, [("*", None)]),
    ],
)
def test_the_ast_shape_this_rests_on(
    source: str, module: str | None, level: int, names: list[tuple[str, str | None]]
) -> None:
    """``module`` is ``None`` for the broken form — that is the whole ambiguity.

    Pinned because the fix is a claim about ``ast``, not about RepoAtlas: if a
    future Python moved the submodule into ``module``, the parser would be
    reading the wrong field and every other test here would still pass.
    """
    node = ast.parse(source).body[0]
    assert isinstance(node, ast.ImportFrom)
    assert node.module == module
    assert node.level == level
    assert [(a.name, a.asname) for a in node.names] == names


# == 3. the parser's output ==================================================


def test_the_submodule_becomes_a_candidate_with_its_package(_: None = None) -> None:
    """``.helper`` — the same spelling ``from .helper import X`` produces."""
    found = imports("from . import helper\n")
    assert set(found) == {".", ".helper"}
    assert found[".helper"]["package"] == "."
    assert found[".helper"]["level"] == "1"


def test_a_deeper_level_keeps_all_of_its_dots() -> None:
    found = imports("from .. import app\n")
    assert set(found) == {"..", "..app"}
    assert found["..app"]["package"] == ".."
    assert found["..app"]["level"] == "2"


def test_every_name_becomes_its_own_candidate() -> None:
    """§5: one fact per module. Keeping only the first was never on the table,
    but neither was concatenating them into one unresolvable key."""
    found = imports("from . import helper, utils, other\n")
    assert set(found) == {".", ".helper", ".utils", ".other"}


def test_an_alias_does_not_change_the_module_identity() -> None:
    """§6: ``h`` is the local binding. The module is still ``helper``."""
    found = imports("from . import helper as h\n")
    assert set(found) == {".", ".helper"}
    assert found[".helper"]["alias"] == "h"


def test_a_star_import_names_no_module() -> None:
    """§21: there is no module called ``*``, and inventing one would be exactly
    the fabrication this change exists to avoid."""
    assert set(imports("from . import *\n")) == {"."}
    assert set(imports("from .. import *\n")) == {".."}


def test_the_package_reference_drops_names_only_in_this_form() -> None:
    """``names`` on the package reference is what the linker uses to decide which
    local bindings came through which module. In this form they are not names
    *of* the package — they are the candidates — so leaving them there would
    point a later ``helper.assist()`` at ``__init__.py``."""
    assert "names" not in imports("from . import helper\n")["."]
    assert imports("from .helper import X\n")[".helper"]["names"] == "X"
    assert imports("from pkg.sub import helper\n")["pkg.sub"]["names"] == "helper"


# == 4. the forms that already worked ========================================


@pytest.mark.parametrize(
    "source,expected",
    [
        ("from .helper import X\n", {".helper": {"level": "1", "names": "X"}}),
        ("from ..app import X\n", {"..app": {"level": "2", "names": "X"}}),
        ("from pkg.sub import helper\n", {"pkg.sub": {"level": "0", "names": "helper"}}),
        ("import pkg.sub.helper\n", {"pkg.sub.helper": {}}),
        ("import os\n", {"os": {}}),
        ("import numpy as np\n", {"numpy": {"alias": "np"}}),
        (
            "from __future__ import annotations\n",
            {"__future__": {"level": "0", "names": "annotations"}},
        ),
    ],
)
def test_existing_import_forms_are_untouched(
    source: str, expected: dict[str, dict[str, str]]
) -> None:
    """§7: the controls, asserted whole rather than by absence of failure."""
    assert imports(source) == expected


def test_the_written_module_form_still_reaches_the_same_file(tmp_path: Path) -> None:
    """``from .helper import assist`` and ``from . import helper`` are one import
    written two ways, and now produce the same file edge."""
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = "from .helper import assist\n"
    space = indexed(tmp_path, tree)
    try:
        assert importers_of(space, "demo", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
    finally:
        space.close()


# == 5. the negative half ====================================================


def test_a_name_defined_only_in_the_package_is_not_a_module(tmp_path: Path) -> None:
    """§8, the most important control in this file.

    ``CONSTANT`` lives in ``__init__.py`` and there is no ``CONSTANT.py``. The
    import is real, the name is real, and there is no module — so there is no
    module edge. A repair that read every name after ``from . import`` as a
    module would invent a file reference here.
    """
    tree = dict(PACKAGE)
    tree["pkg/sub/__init__.py"] = "CONSTANT = 3\n"
    tree["pkg/sub/worker.py"] = "from . import CONSTANT\n"
    space = indexed(tmp_path, tree)
    try:
        # The package import stands: that is where CONSTANT actually comes from.
        assert imported_files(space, "pkg/sub/worker.py") == {"pkg/sub/__init__.py"}
        assert not any("CONSTANT" in path for path in imported_files(space, "pkg/sub/worker.py"))
    finally:
        space.close()


def test_a_missing_submodule_resolves_to_nothing(tmp_path: Path) -> None:
    """§17: the occurrence is kept, the claim is kept, the edge is not invented."""
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = "from . import missing\n"
    space = indexed(tmp_path, tree)
    try:
        assert imported_files(space, "pkg/sub/worker.py") == {"pkg/sub/__init__.py"}
        claims = {
            row["target_key"] for row in rows(space, "SELECT target_key FROM unresolved_refs")
        }
        assert ".missing" in claims, "the import itself must still be on record"
    finally:
        space.close()


def test_a_file_in_no_package_resolves_no_relative_submodule(tmp_path: Path) -> None:
    """§13: ``from . import helper`` at the top level is an ``ImportError``.

    There is a ``helper.py`` right beside it, which is exactly why this test
    exists: proximity is not package membership, and the runtime would refuse
    this import outright.
    """
    space = indexed(
        tmp_path,
        {
            "helper.py": "def assist():\n    return 1\n",
            "worker.py": "from . import helper\n",
        },
    )
    try:
        assert importers_of(space, "demo", "helper.py") == set()
    finally:
        space.close()


def test_more_dots_than_package_resolves_to_nothing(tmp_path: Path) -> None:
    """§12: the dots are not stripped until something happens to exist.

    ``pkg/sub/worker.py`` has two packages above it and the import asks for
    three, so it climbs out of the tree RepoAtlas can reason about — even though
    a ``stray.py`` is sitting at the repository root.
    """
    tree = dict(PACKAGE)
    tree["stray.py"] = "value = 1\n"
    tree["pkg/sub/worker.py"] = "from ... import stray\n"
    space = indexed(tmp_path, tree)
    try:
        assert importers_of(space, "demo", "stray.py") == set()
    finally:
        space.close()


def test_a_same_named_module_elsewhere_is_never_the_target(tmp_path: Path) -> None:
    """§15: resolution is package-relative, so a basename match is not a match."""
    tree = dict(PACKAGE)
    tree["other/helper.py"] = "def assist():\n    return 9\n"
    tree["other/__init__.py"] = ""
    space = indexed(tmp_path, tree)
    try:
        assert importers_of(space, "demo", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
        assert importers_of(space, "demo", "other/helper.py") == set()
    finally:
        space.close()


def test_an_identical_package_in_another_repository_is_never_the_target(tmp_path: Path) -> None:
    """§16: two repositories with the same layout stay separate.

    Module resolution is scoped to the origin's repository, so identical
    normalized names in ``repo-b`` cannot attract ``repo-a``'s import. A
    cross-repository import edge would need evidence RepoAtlas does not have.
    """
    root = tmp_path / "work"
    root.mkdir(parents=True)
    make_repo(root, "repo-a", PACKAGE)
    make_repo(root, "repo-b", PACKAGE)
    settings = settings_for(tmp_path)
    space = open_at(settings, root)
    try:
        initialize(space, root, config_path=config_for(tmp_path))
        assert importers_of(space, "repo-a", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
        assert importers_of(space, "repo-b", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
        crossing = rows(
            space,
            "SELECT e.id FROM edges e "
            "JOIN entities dst ON dst.id = e.dst_id "
            "JOIN files df ON df.id = dst.file_id "
            "WHERE e.type = 'IMPORTS' AND dst.type = 'file' AND df.repo_id != e.repo_id",
        )
        assert crossing == []
    finally:
        space.close()


# == 6. package shapes =======================================================


def test_a_submodule_that_is_a_package_directory_resolves_to_its_init(
    tmp_path: Path,
) -> None:
    """§10: ``pkg/sub/helper/__init__.py`` and no ``helper.py``.

    Pinned because "which file is the package?" is a convention, and the answer
    has to be the same one ``from .helper import X`` already gets.
    """
    tree = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/helper/__init__.py": "def assist():\n    return 2\n",
        "pkg/sub/worker.py": "from . import helper\n",
    }
    space = indexed(tmp_path, tree)
    try:
        assert importers_of(space, "demo", "pkg/sub/helper/__init__.py") == {
            ("pkg/sub/worker.py", 1)
        }
    finally:
        space.close()


def test_a_module_file_wins_over_a_package_directory_of_the_same_name(
    tmp_path: Path,
) -> None:
    """§11: both ``helper.py`` and ``helper/__init__.py`` exist.

    Python's own loader prefers the package, and RepoAtlas prefers the module
    file — the order in ``_candidates``. The two disagree, and this test records
    that rather than claiming a resolution the static model cannot prove: a tree
    holding both is a packaging mistake, and reimplementing the loader's search
    order is not what this pass is for. What matters is that exactly one edge is
    produced and it is deterministic.
    """
    tree = {
        "pkg/__init__.py": "",
        "pkg/sub/__init__.py": "",
        "pkg/sub/helper.py": "def assist():\n    return 1\n",
        "pkg/sub/helper/__init__.py": "def assist():\n    return 2\n",
        "pkg/sub/worker.py": "from . import helper\n",
    }
    space = indexed(tmp_path, tree)
    try:
        targets = imported_files(space, "pkg/sub/worker.py")
        assert targets == {"pkg/sub/__init__.py", "pkg/sub/helper.py"}
    finally:
        space.close()


def test_a_repository_that_is_itself_the_package_resolves(tmp_path: Path) -> None:
    """The layout where the repository root *is* the package directory.

    This one found a real hole. The package prefix ``.`` resolves to the
    repository root, whose path is the empty string, and the candidate list
    joined that into ``/__init__.py`` — an absolute path that can never match a
    repository-relative one. So the root package was invisible, and the new rule
    would have refused every relative import in a repository shaped this way
    while ``from .helper import X`` beside it resolved fine.
    """
    space = indexed(
        tmp_path,
        {
            "__init__.py": "",
            "helper.py": "def assist():\n    return 1\n",
            "worker.py": "from . import helper\n",
        },
    )
    try:
        assert importers_of(space, "demo", "helper.py") == {("worker.py", 1)}
        assert imported_files(space, "worker.py") == {"__init__.py", "helper.py"}
    finally:
        space.close()


def test_a_directory_without_an_init_is_not_a_package(tmp_path: Path) -> None:
    """§14: RepoAtlas identifies a package by ``__init__.py`` and this preserves
    that. Namespace packages are not recognized here and are not recognized
    anywhere else either; adding them would be a different change."""
    tree = {
        "pkg/sub/helper.py": "def assist():\n    return 2\n",
        "pkg/sub/worker.py": "from . import helper\n",
    }
    space = indexed(tmp_path, tree)
    try:
        assert importers_of(space, "demo", "pkg/sub/helper.py") == set()
    finally:
        space.close()


def test_a_submodule_shadowed_by_a_package_attribute_still_resolves(
    tmp_path: Path,
) -> None:
    """§9: ``__init__.py`` binds ``helper`` *and* ``helper.py`` exists.

    At runtime which one wins depends on import order, which RepoAtlas does not
    model. The V1 rule is the conservative one stated in the module docstring: a
    concrete module file is present, so the module reference is emitted. The
    package edge is there too, so both readings are represented and neither is
    asserted to be the only one.
    """
    tree = dict(PACKAGE)
    tree["pkg/sub/__init__.py"] = "helper = object()\n"
    tree["pkg/sub/worker.py"] = "from . import helper\n"
    space = indexed(tmp_path, tree)
    try:
        assert imported_files(space, "pkg/sub/worker.py") == {
            "pkg/sub/__init__.py",
            "pkg/sub/helper.py",
        }
    finally:
        space.close()


# == 7. control flow is not analysed =========================================


@pytest.mark.parametrize(
    "source",
    [
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import helper\n",
        "try:\n    from . import helper\nexcept ImportError:\n    helper = None\n",
    ],
    ids=["type-checking", "try-except"],
)
def test_imports_are_recorded_regardless_of_control_flow(tmp_path: Path, source: str) -> None:
    """§19/§20: the parser records imports wherever they appear, and this change
    does not turn that into conditional-import analysis."""
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = source
    space = indexed(tmp_path, tree)
    try:
        assert ("pkg/sub/worker.py", source.count("\n", 0, source.index("from . import")) + 1) in (
            importers_of(space, "demo", "pkg/sub/helper.py")
        )
    finally:
        space.close()


# == 8. provenance ===========================================================


def test_the_edge_carries_the_line_the_import_is_written_on(tmp_path: Path) -> None:
    """§22: no line drift. The candidate is a fact about a statement, and the
    statement has a position."""
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = (
        '"""Doc."""\n\nimport os\n\nfrom . import helper\nfrom .. import app\n'
    )
    space = indexed(tmp_path, tree)
    try:
        assert importers_of(space, "demo", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 5)}
        assert importers_of(space, "demo", "pkg/app.py") == {("pkg/sub/worker.py", 6)}
    finally:
        space.close()


def test_a_multi_line_import_reports_its_first_line() -> None:
    lines = import_lines("x = 1\nfrom . import (\n    helper,\n    utils,\n)\n")
    assert lines[".helper"] == 2
    assert lines[".utils"] == 2


def test_the_edge_names_the_python_parser(workspace: Workspace) -> None:
    """The global pass owns the file edge; the parser identity rides on the claim."""
    parsers = {
        row["parser"]
        for row in rows(
            workspace,
            "SELECT e.parser FROM edges e JOIN entities dst ON dst.id = e.dst_id "
            "JOIN files df ON df.id = dst.file_id "
            "WHERE e.type = 'IMPORTS' AND df.rel_path = 'pkg/sub/helper.py'",
        )
    }
    assert parsers == {"linker:global"}
    claim = rows(
        workspace,
        "SELECT meta_json FROM unresolved_refs WHERE target_key = '.helper'",
    )
    assert '"parser": "python-ast"' in claim[0]["meta_json"]


# == 9. the alias survives to symbol resolution ==============================


def test_a_call_through_an_imported_submodule_reaches_its_definition(
    tmp_path: Path,
) -> None:
    """§23: end to end, and the reason ``names`` had to move.

    ``helper.assist()`` is resolved through the file's own import graph. While
    ``helper`` mapped to the package prefix, the search looked for ``assist`` in
    ``__init__.py`` and found nothing. It now maps to ``.helper``.
    """
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = (
        "from . import helper\n\n\ndef work():\n    return helper.assist()\n"
    )
    space = indexed(tmp_path, tree)
    try:
        calls = {
            (row["src_path"], row["dst_path"], row["dst_name"])
            for row in rows(
                space,
                "SELECT sf.rel_path AS src_path, df.rel_path AS dst_path, dst.name AS dst_name "
                "FROM edges e JOIN entities dst ON dst.id = e.dst_id "
                "JOIN files df ON df.id = dst.file_id JOIN files sf ON sf.id = e.file_id "
                "WHERE e.type = 'CALLS' AND e.parser = 'linker:global'",
            )
        }
        assert ("pkg/sub/worker.py", "pkg/sub/helper.py", "assist") in calls
    finally:
        space.close()


def test_an_aliased_submodule_call_resolves_through_the_alias(tmp_path: Path) -> None:
    """§6, end to end: ``h`` is bound locally, ``helper`` is the module."""
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = (
        "from . import helper as h\n\n\ndef work():\n    return h.assist()\n"
    )
    space = indexed(tmp_path, tree)
    try:
        calls = {
            (row["dst_path"], row["dst_name"])
            for row in rows(
                space,
                "SELECT df.rel_path AS dst_path, dst.name AS dst_name FROM edges e "
                "JOIN entities dst ON dst.id = e.dst_id JOIN files df ON df.id = dst.file_id "
                "WHERE e.type = 'CALLS' AND e.parser = 'linker:global'",
            )
        }
        assert ("pkg/sub/helper.py", "assist") in calls
    finally:
        space.close()


# == 10. the product question ================================================


def test_who_depends_on_helper_now_names_the_importer(workspace: Workspace, tmp_path: Path) -> None:
    """§27: the user-impact proof, through the retrieval path rather than SQL."""
    evidence = locate(workspace, "pkg/sub/helper.py", context_in(workspace, tmp_path))
    paths = {candidate.path for candidate in evidence.candidates}
    assert "pkg/sub/helper.py" in paths
    assert "pkg/sub/worker.py" in paths, "the importer is reachable from the imported file"


def test_exact_path_ranking_still_puts_the_file_itself_first(
    workspace: Workspace, tmp_path: Path
) -> None:
    """§28: a new inbound edge must not change what a path query is *about*."""
    evidence = locate(workspace, "pkg/sub/helper.py", context_in(workspace, tmp_path))
    assert evidence.candidates[0].path == "pkg/sub/helper.py"


# == 11. an existing index, and changes over time ============================


def test_an_existing_index_gains_the_edge_on_an_ordinary_sync(tmp_path: Path) -> None:
    """§24: the Python file is byte-identical after the fix.

    Neither the content hash nor the file's stat notices anything, so nothing but
    a facts-version change makes ``sync`` reparse it. The parser version bump is
    what turns this from "delete your index" into "run sync".
    """
    root = tmp_path / "work"
    root.mkdir(parents=True)
    repo = make_repo(root, "demo", PACKAGE)
    settings = settings_for(tmp_path)

    space = open_at(settings, root)
    try:
        initialize(space, root, config_path=config_for(tmp_path))
        before = importers_of(space, "demo", "pkg/sub/helper.py")
    finally:
        space.close()
    assert before == {("pkg/sub/worker.py", 1)}

    # Simulate the pre-fix index. The *claims* have to go too, not just the
    # edges: the global pass rebuilds its own output from `unresolved_refs` on
    # every sync, so deleting edges alone would restore them without anything
    # being reparsed, and the test would pass while proving nothing.
    space = open_at(settings, root)
    try:
        space.database.execute(
            "DELETE FROM unresolved_refs WHERE target_key IN ('.helper', '..app')"
        )
        space.database.execute(
            "DELETE FROM edges WHERE id IN ("
            "  SELECT e.id FROM edges e JOIN entities dst ON dst.id = e.dst_id"
            "  JOIN files df ON df.id = dst.file_id"
            "  WHERE df.rel_path IN ('pkg/sub/helper.py', 'pkg/app.py')"
            "    AND e.type = 'IMPORTS')"
        )
        # The Python bytes are identical before and after the fix, so neither the
        # content hash nor the file's stat can notice anything. Only the recorded
        # facts version can, which is what makes the bump load-bearing — so the
        # stored version is the one this index *would* carry with the previous
        # Python parser, not an arbitrary string that would differ either way.
        space.database.execute(
            "UPDATE files SET parser_version = ? WHERE rel_path = 'pkg/sub/worker.py'",
            (previous_facts_version(space),),
        )
        space.database.connection.commit()
        assert importers_of(space, "demo", "pkg/sub/helper.py") == set()
        assert importers_of(space, "demo", "pkg/app.py") == set()
    finally:
        space.close()

    space = open_at(settings, root)
    try:
        report = sync(space, space.resolver().resolve(repo))
        assert report.repositories[0].changed == 1, report
        assert importers_of(space, "demo", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
        assert importers_of(space, "demo", "pkg/app.py") == {("pkg/sub/worker.py", 2)}
    finally:
        space.close()

    space = open_at(settings, root)
    try:
        again = sync(space, space.resolver().resolve(repo))
        assert again.repositories[0].changed == 0, again
        assert importers_of(space, "demo", "pkg/sub/helper.py") == {("pkg/sub/worker.py", 1)}
    finally:
        space.close()


def test_the_parser_version_is_ahead_of_the_one_that_lost_the_submodule() -> None:
    """Pinned so the bump cannot be dropped as an unused-looking constant.

    Stated as an inequality rather than as ``== "3"`` so a later, unrelated bump
    does not have to edit this file to stay true, while removing *this* bump
    still fails here.
    """
    assert int(PythonParser().version) > int(LOSSY_PYTHON_VERSION)


def test_edits_move_the_edge_and_leave_nothing_stale(tmp_path: Path) -> None:
    """§25: written form → candidate form → gone → back."""
    root = tmp_path / "work"
    root.mkdir(parents=True)
    tree = dict(PACKAGE)
    tree["pkg/sub/worker.py"] = "from .helper import assist\n"
    repo = make_repo(root, "demo", tree)
    settings = settings_for(tmp_path)

    def state() -> set[tuple[str, int]]:
        space = open_at(settings, root)
        try:
            return importers_of(space, "demo", "pkg/sub/helper.py")
        finally:
            space.close()

    def resync(source: str) -> None:
        rewrite(repo, "pkg/sub/worker.py", source)
        space = open_at(settings, root)
        try:
            sync(space, space.resolver().resolve(repo))
        finally:
            space.close()

    space = open_at(settings, root)
    try:
        initialize(space, root, config_path=config_for(tmp_path))
    finally:
        space.close()
    assert state() == {("pkg/sub/worker.py", 1)}

    resync("from . import helper\n")
    assert state() == {("pkg/sub/worker.py", 1)}

    resync("from . import missing\n")
    assert state() == set(), "the previous import must not survive its own deletion"

    resync("from . import helper\n")
    assert state() == {("pkg/sub/worker.py", 1)}


def test_a_deleted_submodule_takes_its_inbound_edge_with_it(tmp_path: Path) -> None:
    """The other direction: the import stays, the module goes."""
    root = tmp_path / "work"
    root.mkdir(parents=True)
    repo = make_repo(root, "demo", PACKAGE)
    settings = settings_for(tmp_path)
    space = open_at(settings, root)
    try:
        initialize(space, root, config_path=config_for(tmp_path))
    finally:
        space.close()

    (repo / "pkg" / "sub" / "helper.py").unlink()
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "drop")

    space = open_at(settings, root)
    try:
        sync(space, space.resolver().resolve(repo))
        assert imported_files(space, "pkg/sub/worker.py") == {
            "pkg/sub/__init__.py",
            "pkg/__init__.py",
            "pkg/app.py",
        }
    finally:
        space.close()
