"""Wiring for a RepoAtlas workspace (ARCHITECTURE.md §1).

One place assembles stores, parsers, retrieval and context resolution. Commands
receive an assembled workspace; none of them constructs storage itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repoatlas.config.settings import Settings, database_path, load_settings
from repoatlas.context.resolver import RepositoryContextResolver
from repoatlas.errors import NotInitializedError
from repoatlas.gitstate.client import GitClient
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.retrieval.retriever import Retriever
from repoatlas.storage.sqlite.database import Database, open_database
from repoatlas.storage.sqlite.search import SqliteSearchIndex
from repoatlas.storage.sqlite.stores import (
    SqliteFileStore,
    SqliteKnowledgeStore,
    SqliteRepositoryStore,
)
from repoatlas.sync.engine import SyncEngine


@dataclass(slots=True)
class Workspace:
    """Everything a command needs, assembled once."""

    settings: Settings
    database: Database
    repositories: SqliteRepositoryStore
    files: SqliteFileStore
    knowledge: SqliteKnowledgeStore
    search: SqliteSearchIndex
    registry: ParserRegistry
    git: GitClient

    @property
    def root(self) -> Path | None:
        return self.settings.root

    def resolver(self) -> RepositoryContextResolver:
        return RepositoryContextResolver(self.root, self.repositories.list_all())

    def sync_engine(self) -> SyncEngine:
        return SyncEngine(
            repositories=self.repositories,
            files=self.files,
            knowledge=self.knowledge,
            registry=self.registry,
            settings=self.settings,
            git=self.git,
        )

    def retriever(self) -> Retriever:
        return Retriever(search=self.search, knowledge=self.knowledge)

    def close(self) -> None:
        self.database.close()


def open_workspace(*, settings: Settings | None = None, require_root: bool = True) -> Workspace:
    """Open the configured workspace, or fail with exit code 3."""
    resolved = settings or load_settings()
    if require_root and resolved.root is None:
        raise NotInitializedError()

    database = open_database(database_path(resolved))
    return Workspace(
        settings=resolved,
        database=database,
        repositories=SqliteRepositoryStore(database),
        files=SqliteFileStore(database),
        knowledge=SqliteKnowledgeStore(database),
        search=SqliteSearchIndex(database),
        registry=ParserRegistry(),
        git=GitClient(),
    )
