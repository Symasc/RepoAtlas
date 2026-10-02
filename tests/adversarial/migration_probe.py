"""A test-only second migration, so the upgrade path has something to upgrade to.

The committed schema is one migration. The migrator's guarantees — one
transaction per migration, recovery from an interrupted one, refusal of a schema
no migration produces, one winner among concurrent openers — are about moving
*between* versions, and they have to hold for the next real migration the day it
is written. This one is shaped like the kind that broke before: a column, an
index on it, and a data-only tail.

:func:`install` makes it part of the chain for the whole process. A subprocess
calls it before opening anything; in-process tests use :func:`patch` with
pytest's ``monkeypatch`` so it is undone afterwards.
"""

from __future__ import annotations

from typing import Any

from repoatlas.storage.sqlite.migrator import Migration, load_migrations, split_statements

#: The version the probe migration stamps, one past the committed schema.
VERSION = 2

SQL = """
ALTER TABLE repositories ADD COLUMN probe TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX idx_repositories_probe ON repositories (probe) WHERE probe != '';
DELETE FROM entities WHERE type = 'repository' AND repo_id IS NULL;
"""

PROBE = Migration(version=VERSION, statements=tuple(split_statements(SQL)))


def chain() -> list[Migration]:
    """The committed migrations followed by the probe."""
    committed = load_migrations()
    assert committed[-1].version == VERSION - 1, "the probe must follow the committed schema"
    return [*committed, PROBE]


#: Every module that reads the migration list or the schema version at call time.
_TARGETS = (
    ("repoatlas.storage.sqlite.database", "load_migrations"),
    ("repoatlas.storage.sqlite.identity", "load_migrations"),
    ("repoatlas.storage.sqlite.migrator", "load_migrations"),
)


def patch(setattr_: Any) -> None:
    """Install the probe through ``setattr_(module_path, name, value)``."""
    migrations = chain()
    for module, name in _TARGETS:
        setattr_(f"{module}.{name}", lambda: list(migrations))
    setattr_("repoatlas.storage.sqlite.database.SCHEMA_VERSION", VERSION)


def install() -> None:
    """Install the probe for the rest of this process. For subprocesses."""
    import importlib

    def assign(target: str, value: object) -> None:
        module, name = target.rsplit(".", 1)
        setattr(importlib.import_module(module), name, value)

    patch(assign)
