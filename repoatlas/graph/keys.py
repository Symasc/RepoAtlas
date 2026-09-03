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

from repoatlas.models.parsing import EntityType


def repository_key(repo_slug: str) -> str:
    return f"repo:{repo_slug}"


def file_key(repo_slug: str, rel_path: str) -> str:
    return f"file:{repo_slug}:{rel_path}"


def symbol_key(repo_slug: str, rel_path: str, qualified_name: str, line: int) -> str:
    return f"sym:{repo_slug}:{rel_path}:{qualified_name}:{line}"


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
        case _:
            return f"{kind.value}:{target}"
