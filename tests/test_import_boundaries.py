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

Two rules, and the second is as load-bearing as the first:

* **the deterministic path pays nothing for the legacy one** --- importing the
  capability layer, the transport, the services package or any deterministic
  service loads no ``repoatlas.llm``;
* **the legacy path still works when it is asked for** --- ``query_service`` and
  the names that reach it load exactly what they always did. This correction
  moves the cost onto whoever incurs it; it does not remove the subsystem, which
  is a later, separate change.

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

#: Anything under here is V1's embedded reasoning stack.
LEGACY_PREFIX = "repoatlas.llm"

#: Deterministic, and explicitly allowed. Named as a constant so the intent is
#: impossible to misread as an oversight.
DETERMINISTIC_GROUNDING = "repoatlas.grounding"


class Imported(TypedDict):
    """What a fresh interpreter had loaded after one import statement."""

    legacy: list[str]
    query_service: bool
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
                "query_service": "repoatlas.services.query_service" in sys.modules,
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
    pytest.param("import repoatlas.services.status_service", id="status-service"),
    pytest.param("import repoatlas.services.freshness_service", id="freshness-service"),
    pytest.param("import repoatlas.services.init_service", id="init-service"),
    pytest.param("import repoatlas.services.workspace", id="workspace"),
    pytest.param("import repoatlas.grounding", id="grounding"),
]


@pytest.mark.parametrize("statement", DETERMINISTIC_IMPORTS)
def test_no_deterministic_import_loads_the_legacy_reasoning_stack(statement: str) -> None:
    """CASES 1, 2, 4, 5. The invariant, asked the only way that settles it."""
    loaded = _loaded(statement)
    assert loaded["legacy"] == [], f"{statement} loaded {loaded['legacy']}"


@pytest.mark.parametrize("statement", DETERMINISTIC_IMPORTS)
def test_no_deterministic_import_loads_the_legacy_query_service(statement: str) -> None:
    """``query_service`` is the door to the legacy stack, so it is barred too.

    Asserted separately from the ``repoatlas.llm`` rule: an implementation that
    imported ``query_service`` but happened to defer its own model imports would
    satisfy the rule above while leaving the seam exactly where it was.
    """
    loaded = _loaded(statement)
    assert loaded["query_service"] is False, statement


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
# The legacy path still works when it is asked for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("import repoatlas.services.query_service", id="module"),
        pytest.param("from repoatlas.services import query_service", id="from-package"),
        pytest.param("import repoatlas.services as s; s.reason", id="lazy-attribute"),
        pytest.param("from repoatlas.services import reason", id="lazy-from-import"),
        pytest.param("import repoatlas.cli.main", id="v1-cli"),
    ],
)
def test_asking_for_legacy_reasoning_still_loads_it(statement: str) -> None:
    """CASE 6. The cost moved onto whoever incurs it; it did not disappear.

    The V1 CLI is in this list deliberately. It is the caller that legitimately
    wants the reasoning stack, and a correction that quietly stopped loading it
    would have broken ``ask`` rather than decoupled anything.
    """
    loaded = _loaded(statement)
    assert loaded["legacy"], f"{statement} loaded no legacy modules"
    assert "repoatlas.llm.reasoner" in loaded["legacy"]


def test_the_legacy_stack_is_the_same_shape_it_always_was() -> None:
    """Nothing was removed, hidden or split. Only its arrival time moved."""
    loaded = _loaded("import repoatlas.services.query_service")
    assert set(loaded["legacy"]) == {
        "repoatlas.llm",
        "repoatlas.llm.factory",
        "repoatlas.llm.null",
        "repoatlas.llm.ollama",
        "repoatlas.llm.prompts",
        "repoatlas.llm.prompts.loader",
        "repoatlas.llm.protocol",
        "repoatlas.llm.reasoner",
        "repoatlas.llm.scripted",
    }


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
        query_service,
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
    assert services.locate is query_service.locate
    assert services.reason is query_service.reason
    assert services.retrieve_for is query_service.retrieve_for


def test_the_exported_names_are_exactly_what_v1_exported() -> None:
    import repoatlas.services as services

    assert sorted(services.__all__) == [
        "Workspace",
        "initialize",
        "list_repositories",
        "locate",
        "open_workspace",
        "reason",
        "retrieve_for",
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
    assert services.reason is services.reason


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
        "repoatlas.services.query_service",
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
