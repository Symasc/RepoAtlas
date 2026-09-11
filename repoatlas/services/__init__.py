"""The V1 service layer, as a package that costs nothing to enter.

Every name V1 exposed here is still exposed here, and still with the same
spelling. What changed is *when* the module behind it is imported.

Why this package has a ``__getattr__``
--------------------------------------
``repoatlas.services`` holds two unrelated things. Most of it is deterministic —
opening a workspace, syncing, reading status. One module, ``query_service``, is
the entry point to V1's embedded reasoning, and it reaches ``repoatlas.llm``,
which reaches an HTTP client for a local model server.

Eagerly re-exporting all of them made those unrelated. Python initializes a
package before any of its submodules, so ``from repoatlas.services import
sync_service`` — a purely deterministic import, and the one
:class:`~repoatlas.capabilities.service.CapabilityService` uses — ran this file
first and pulled nine ``repoatlas.llm`` modules and an HTTP stack into every V2
process. Nothing called them. They were resident because of a line in an
``__init__``.

The RC audit found this by importing the capability layer in a clean interpreter
and reading ``sys.modules``. The static layering guards could not: they read
import statements, and no module under ``capabilities/`` names ``repoatlas.llm``
at all. The coupling was entirely in package initialization, which is the one
place an AST walk of the importing module cannot see.

So this file imports nothing. A name is resolved the first time it is *used*,
which is what the deferred lookup below does, and after that it is an ordinary
module global — :pep:`562` consults ``__getattr__`` only when normal attribute
lookup fails, so the cost is paid once and the legacy stack is loaded only by
whoever actually asked for a legacy name.

What this is not
----------------
Not a service locator, and not lazy loading as a strategy. It is nine fixed names
bound to five fixed modules, spelled out in a dict, with no dynamic construction
and no registry to register with. Submodule imports do not go through it at all:
``from repoatlas.services import sync_service`` resolves as a submodule by the
import machinery, so it never reaches this function.

It is also transitional. The dedicated legacy-LLM removal deletes
``query_service``'s reasoning half, and when it does, three of these entries go
with it.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

#: Re-exported name, and the submodule that defines it. Nothing else is deferred
#: and nothing else may be: an unknown name raises, so a typo is an error here
#: rather than an import attempt for a module that does not exist.
_EXPORTS: dict[str, str] = {
    "Workspace": "workspace",
    "initialize": "init_service",
    "list_repositories": "status_service",
    "locate": "query_service",
    "open_workspace": "workspace",
    "reason": "query_service",
    "retrieve_for": "query_service",
    "status": "status_service",
    "sync": "sync_service",
}

__all__ = [
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

if TYPE_CHECKING:
    # For a reader and for a type checker only. At runtime these names arrive
    # through `__getattr__`, so nothing here is imported when the package is.
    from repoatlas.services.init_service import initialize
    from repoatlas.services.query_service import locate, reason, retrieve_for
    from repoatlas.services.status_service import list_repositories, status
    from repoatlas.services.sync_service import sync
    from repoatlas.services.workspace import Workspace, open_workspace


def __getattr__(name: str) -> Any:
    """Resolve one re-exported name, importing its module the first time.

    The real object is returned, never a wrapper: ``repoatlas.services.sync`` and
    ``repoatlas.services.sync_service.sync`` are the same function, so patching
    either is patching one thing.

    Recursion-safe without a guard. A circular import cannot re-enter this
    function for the same name, because by then ``repoatlas.services`` is already
    in ``sys.modules`` and the partially-initialized submodule is too — the
    failure mode is a plain ``AttributeError`` naming the attribute, which is what
    a reader needs to see.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    # Bind it as an ordinary global. `__getattr__` is the fallback path, so this
    # is the last time it is consulted for this name.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """The re-exports, whether or not they have been resolved yet."""
    return sorted({*globals(), *__all__})
