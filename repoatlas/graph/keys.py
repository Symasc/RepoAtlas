"""Natural keys for graph entities (ARCHITECTURE.md §3).

A key is globally unique and deterministic, which is what makes interning and
cross-file joins exact rather than heuristic.

Every repository-scoped key is built from the repository's **identity** — its
canonical root-relative path — and never from its display name. The distinction
is the whole point: two managed repositories may both be called ``common``, and
``repo:common``, ``file:common:index.ts`` and ``sym:common:index.ts:X:1`` then
name two different things at once, which is not a key.
"""

from __future__ import annotations

from repoatlas.models import http
from repoatlas.models.parsing import EntityType

#: The one key shape that embeds a source position.
#:
#: Only :func:`symbol_key` builds a key containing a line number, so only these
#: keys stop resolving when a definition moves. Every other prefix names something
#: interned by identity — a variable, a module, a package, a file — and survives a
#: sync that moves things around inside it.
POSITIONAL_KEY_PREFIX = "sym:"


def is_positional(key: str) -> bool:
    """Whether a key moves with the definition it names.

    Decided here, beside the constructors that decide the format, rather than by
    the entity's type — which does not answer the question. A ``function`` reached
    as a file-scoped definition has a positional key; the same type interned
    globally would not. What settles it is which constructor built the key.

    Published because a caller holding a handle needs to know which ones are safe
    to keep across a sync, rather than discovering it from a lookup that returns
    nothing.
    """
    return key.startswith(POSITIONAL_KEY_PREFIX)


def repository_key(repo_slug: str) -> str:
    return f"repo:{repo_slug}"


def file_key(repo_slug: str, rel_path: str) -> str:
    return f"file:{repo_slug}:{rel_path}"


def symbol_key(repo_slug: str, rel_path: str, qualified_name: str, line: int | None) -> str:
    """Identity of a file-scoped symbol.

    ``line`` is absent only for the whole-file symbol of an empty file, which has
    no lines to name. It renders as ``-`` rather than as ``None`` so the key stays
    readable, and it is a single fixed token so the key stays stable across syncs.
    """
    return f"sym:{repo_slug}:{rel_path}:{qualified_name}:{line if line is not None else '-'}"


def env_key(name: str) -> str:
    return f"env:{name}"


def module_key(name: str) -> str:
    return f"module:{name}"


def package_key(ecosystem: str, name: str) -> str:
    return f"pkg:{ecosystem}:{name}"


def command_key(name: str) -> str:
    return f"cmd:{name}"


def config_key(name: str) -> str:
    return f"cfgkey:{name}"


def global_key(kind: EntityType, target: str) -> str:
    """Key for an interned target of the given kind."""
    match kind:
        case EntityType.ENV_VAR:
            return env_key(target)
        case EntityType.MODULE:
            return module_key(target)
        case EntityType.COMMAND:
            return command_key(target)
        case EntityType.CONFIG:
            return config_key(target)
        case EntityType.REPOSITORY:
            return repository_key(target)
        case EntityType.HTTP_ROUTE:
            return http.route_key(target)
        case EntityType.HTTP_DEPLOYMENT:
            return http.deployment_key(target)
        case EntityType.HTTP_ENDPOINT:
            return http.endpoint_key(target)
        case _:
            return f"{kind.value}:{target}"
