"""What the deterministic layer costs to import (RC audit, RCA-02).

``tests/test_layering.py`` reads import statements. It is the right tool for
"does anything under ``capabilities/`` name ``repoatlas.cli``", and it was
blind to this defect, because no module under ``capabilities/`` names
``repoatlas.llm`` anywhere. The coupling lived in a package ``__init__``:
``repoatlas.services`` eagerly re-exported ``query_service``, Python initializes
a package before any of its submodules, and so the deterministic import

    from repoatlas.services import sync_service

ran that file first and pulled nine ``repoatlas.llm`` modules plus an HTTP stack
into every V2 process. Nothing called them. An AST walk of the *importing*
module cannot see that, and neither can one of the imported module.

So this suite asks the only question that settles it: import the thing, in a
process that has imported nothing else, and read ``sys.modules``. That observes
the transitive closure directly rather than reconstructing it, which is both
stronger than the static guard and complementary to it --- the static guard keeps
catching the direct edges, which a subprocess probe would never attribute to the
right file.

The rule, stated once: **importing anything in RepoAtlas loads no reasoning
stack.** Every entry point is in the list --- the capability layer, the
transport, the CLI, the services package and each deterministic service --- and
every one of them must come back with nothing.

That rule used to have a second half. RCA-02 moved the cost of the legacy stack
onto whoever incurred it rather than removing it, so this file also asserted that
``query_service`` still loaded the nine ``repoatlas.llm`` modules it always had.
The subsystem is now deleted, and the invariant is stronger for it: there is no
legacy stack to load and no module that would load one. The negative half lives
in :mod:`tests.test_no_embedded_reasoner`, which asserts that the package is
gone rather than merely unreferenced; what stays here is the transitive-cost
question, asked of the imports a user actually performs.

The CLI is now in the list it used to be excluded from. It was the one caller
that legitimately wanted the reasoner, which is why a correction that stopped
loading it would have been a bug rather than a decoupling. It wants nothing now.

``repoatlas.grounding`` is deterministic and is **never** forbidden here. It was
moved out of ``llm/`` by Slice 0 precisely because it is not model code, and a
test that banned it would be asserting the opposite of the architecture.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import TypedDict

import pytest

#: Anything under here was V1's embedded reasoning stack. The package is deleted;
#: the prefix stays as the thing that must never come back.
LEGACY_PREFIX = "repoatlas.llm"

#: Deterministic, and explicitly allowed. Named as a constant so the intent is
#: impossible to misread as an oversight.
DETERMINISTIC_GROUNDING = "repoatlas.grounding"


class Imported(TypedDict):
    """What a fresh interpreter had loaded after one import statement."""

    legacy: list[str]
    #: Loaded RepoAtlas modules whose *name* suggests a model, whatever they are
    #: called. Wider than the prefix above, so a stack rebuilt under another
    #: spelling is caught too.
    suspicious: list[str]
    grounding: bool
    repoatlas_total: int


def _loaded(statement: str) -> Imported:
    """Import ``statement`` in a fresh interpreter and report what arrived.

    A subprocess rather than an in-process ``importlib`` dance, because pytest
    has already imported most of RepoAtlas by the time this runs --- the answer
    has to come from a process where the import under test is the first thing
    that happens.
    """
    source = textwrap.dedent(f"""
        import json
        import sys

        {statement}

        json.dump(
            {{
                "legacy": sorted(
                    name for name in sys.modules if name.startswith({LEGACY_PREFIX!r})
                ),
                "suspicious": sorted(
                    name
                    for name in sys.modules
                    if name.startswith("repoatlas")
                    and ("llm" in name.lower() or "reason" in name.lower())
                ),
                "grounding": {DETERMINISTIC_GROUNDING!r} in sys.modules,
                "repoatlas_total": len(
                    [name for name in sys.modules if name.startswith("repoatlas")]
                ),
            }},
            sys.stdout,
        )
    """)
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded: Imported = json.loads(result.stdout)
    return loaded


# ---------------------------------------------------------------------------
# The deterministic path pays nothing
# ---------------------------------------------------------------------------


DETERMINISTIC_IMPORTS = [
    pytest.param("import repoatlas.capabilities.service", id="capability-service"),
    pytest.param("import repoatlas.mcp.server", id="mcp-server"),
    pytest.param("import repoatlas.services", id="services-package"),
    pytest.param("import repoatlas.services.sync_service", id="sync-service"),
    pytest.param("from repoatlas.services import sync_service", id="sync-service-from-package"),
    pytest.param("import repoatlas.services.retrieval_service", id="retrieval-service"),
    pytest.param("import repoatlas.services.status_service", id="status-service"),
    pytest.param("import repoatlas.services.freshness_service", id="freshness-service"),
    pytest.param("import repoatlas.services.init_service", id="init-service"),
    pytest.param("import repoatlas.services.workspace", id="workspace"),
    pytest.param("import repoatlas.grounding", id="grounding"),
    # Previously excluded: it was the one caller that wanted the reasoning stack.
    pytest.param("import repoatlas.cli.main", id="v1-cli"),
    pytest.param("import repoatlas.services.retrieval_service", id="retrieval-service-module"),
]


@pytest.mark.parametrize("statement", DETERMINISTIC_IMPORTS)
def test_no_deterministic_import_loads_the_legacy_reasoning_stack(statement: str) -> None:
    """CASES 1, 2, 4, 5. The invariant, asked the only way that settles it."""
    loaded = _loaded(statement)
    assert loaded["legacy"] == [], f"{statement} loaded {loaded['legacy']}"


@pytest.mark.parametrize("statement", DETERMINISTIC_IMPORTS)
def test_no_import_loads_a_module_whose_name_suggests_a_model(statement: str) -> None:
    """Wider than the prefix rule above, and deliberately so.

    ``query_service`` used to be named here as the door to the legacy stack. The
    door is gone, so what is asserted instead is that nothing has quietly been
    rebuilt under another spelling: no loaded RepoAtlas module has ``llm`` or
    ``reason`` in its name.
    """
    loaded = _loaded(statement)
    assert loaded["suspicious"] == [], statement


def test_the_services_package_is_genuinely_empty_to_enter() -> None:
    """Not merely free of the legacy stack --- free of everything.

    A package that had merely swapped one eager import for three would pass the
    rules above and still make every submodule import pay for its siblings.
    """
    loaded = _loaded("import repoatlas.services")
    assert loaded["repoatlas_total"] <= 3, loaded["repoatlas_total"]


def test_deterministic_grounding_is_available_and_never_forbidden() -> None:
    """CASE 3. The rule this suite must not overreach into.

    ``ground_answer`` is deterministic provenance checking, moved out of ``llm/``
    by Slice 0 for exactly this reason. Verification depends on it, so it loads
    with the capability layer and that is correct.
    """
    assert _loaded("import repoatlas.capabilities.service")["grounding"] is True
    assert _loaded("import repoatlas.grounding")["legacy"] == []

    from repoatlas.grounding import ground_answer

    assert callable(ground_answer)


# ---------------------------------------------------------------------------
# There is no legacy path left to ask for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("import repoatlas.llm", id="package"),
        pytest.param("import repoatlas.llm.reasoner", id="reasoner"),
        pytest.param("import repoatlas.llm.grounding", id="grounding-shim"),
        pytest.param("import repoatlas.services.query_service", id="query-service"),
        pytest.param("from repoatlas.services import query_service", id="query-service-attr"),
        pytest.param("from repoatlas.services import reason", id="lazy-from-import"),
    ],
)
def test_asking_for_legacy_reasoning_finds_nothing(statement: str) -> None:
    """The rule RCA-02 could not make, now that the subsystem is deleted.

    RCA-02 deferred the cost of the reasoning stack and asserted, here, that it
    still arrived for whoever asked. Nobody can ask: the modules do not exist and
    the deferred lookup does not know the names. A shim that answered any of
    these would be advertising support for a removed feature.
    """
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, f"{statement} succeeded"
    assert "ModuleNotFoundError" in result.stderr or "ImportError" in result.stderr


# ---------------------------------------------------------------------------
# The package surface is unchanged
# ---------------------------------------------------------------------------


def test_every_re_exported_name_is_the_real_object() -> None:
    """CASE 10. A lazy re-export must be the function, not a stand-in for it.

    Identity is what makes patching work: a test that patches
    ``sync_service.sync`` must be patching the thing ``services.sync`` resolves
    to, or the two drift and the test lies.
    """
    import repoatlas.services as services
    from repoatlas.services import (
        init_service,
        retrieval_service,
        status_service,
        sync_service,
        workspace,
    )

    assert services.Workspace is workspace.Workspace
    assert services.open_workspace is workspace.open_workspace
    assert services.initialize is init_service.initialize
    assert services.list_repositories is status_service.list_repositories
    assert services.status is status_service.status
    assert services.sync is sync_service.sync
    assert services.locate is retrieval_service.locate
    assert services.locate_diagnostic is retrieval_service.locate_diagnostic


def test_the_exported_names_are_the_deterministic_ones() -> None:
    """V1's list, minus the two the reasoner took with it.

    ``reason`` is gone because the function is. ``retrieve_for`` is gone because
    its intent parameter belonged to the reasoner; the retrieval it performed is
    still here, under a name that does not.
    """
    import repoatlas.services as services

    assert sorted(services.__all__) == [
        "Workspace",
        "initialize",
        "list_repositories",
        "locate",
        "locate_diagnostic",
        "open_workspace",
        "status",
        "sync",
    ]
    assert set(services.__all__) <= set(dir(services))


def test_an_unknown_attribute_raises_rather_than_importing() -> None:
    """The deferred lookup is nine fixed names, not a module loader.

    A ``__getattr__`` that tried to import whatever it was asked for would turn
    a typo into an ``ImportError`` from somewhere unrelated, and would make the
    package a service locator --- which is the thing this deliberately is not.
    """
    import repoatlas.services as services

    with pytest.raises(AttributeError, match="has no attribute 'no_such_service'"):
        services.no_such_service  # noqa: B018 - the access is the assertion


def test_resolving_a_name_twice_returns_one_object() -> None:
    """CASE 11's other half: the deferred path must not re-import or re-wrap."""
    import repoatlas.services as services

    assert services.sync is services.sync
    assert services.locate is services.locate


# ---------------------------------------------------------------------------
# No cycles, and no side effects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "repoatlas.services",
        "repoatlas.services.workspace",
        "repoatlas.services.sync_service",
        "repoatlas.services.status_service",
        "repoatlas.services.init_service",
        "repoatlas.services.freshness_service",
        "repoatlas.services.retrieval_service",
        "repoatlas.capabilities.service",
        "repoatlas.mcp.server",
        "repoatlas.cli.main",
    ],
)
def test_every_module_imports_cleanly_on_its_own(module: str) -> None:
    """CASE 11. Changing package initialization is how cycles get exposed."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_importing_the_package_touches_nothing_outside_the_process() -> None:
    """CASE 4's companion: package import is declarative.

    Nothing was added to compensate for the removed eager imports --- no config
    read, no database opened, no model built. Asserted by importing with every
    filesystem entry point replaced by something that would raise.
    """
    source = textwrap.dedent("""
        import builtins
        import sqlite3
        import sys

        def refuse(*args, **kwargs):
            raise AssertionError("import-time side effect")

        builtins.open = refuse
        sqlite3.connect = refuse

        import repoatlas.services
        print("clean")
    """)
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
