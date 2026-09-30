"""Wiring for a RepoAtlas workspace (ARCHITECTURE.md §1).

One place assembles stores, parsers, retrieval and context resolution. Commands
receive an assembled workspace; none of them constructs storage itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from repoatlas.config.settings import Settings, database_path, load_settings
from repoatlas.context.resolver import RepositoryContextResolver
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.embeddings.factory import build_embedding_model
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable
from repoatlas.errors import NotInitializedError
from repoatlas.gitstate.client import GitClient
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.retrieval.retriever import RetrievalLimits, Retriever
from repoatlas.storage.sqlite.database import Database, open_database
from repoatlas.storage.sqlite.search import SqliteSearchIndex
from repoatlas.storage.sqlite.stores import (
    SqliteDependencyStore,
    SqliteFileStore,
    SqliteKnowledgeStore,
    SqliteRepositoryStore,
)
from repoatlas.storage.sqlite.topology import SqliteTopologyStore
from repoatlas.storage.sqlite.vectors import SqliteVectorIndex
from repoatlas.sync.engine import SyncEngine
from repoatlas.sync.lock import writer_lock


@dataclass(slots=True)
class Workspace:
    """Everything a command needs, assembled once."""

    settings: Settings
    database: Database
    repositories: SqliteRepositoryStore
    files: SqliteFileStore
    knowledge: SqliteKnowledgeStore
    dependencies: SqliteDependencyStore
    search: SqliteSearchIndex
    vectors: SqliteVectorIndex
    registry: ParserRegistry
    dependency_registry: DependencyRegistry
    git: GitClient
    _embeddings: EmbeddingModel | None = None
    _embeddings_built: bool = False

    @property
    def root(self) -> Path | None:
        return self.settings.root

    def resolver(self) -> RepositoryContextResolver:
        return RepositoryContextResolver(self.root, self.repositories.list_all())

    def embeddings(self) -> EmbeddingModel | None:
        """The configured embedding backend, built once and cached.

        Returns ``None`` when embeddings are disabled or misconfigured. That is a
        supported state, not an error: exact search is the mandatory half of
        retrieval and semantic search is the aid (V1_SPEC §14), so every
        deterministic command keeps working either way.
        """
        if not self._embeddings_built:
            self._embeddings_built = True
            try:
                self._embeddings = build_embedding_model(self.settings.embeddings)
            except EmbeddingUnavailable:
                self._embeddings = None
        return self._embeddings

    @contextmanager
    def writer_lock(self) -> Iterator[Path]:
        """Hold the exclusive writer lock for this workspace's state database.

        Every mutating command takes it; no read command does. The scope is the
        database rather than a repository, because a single-repository sync still
        rebuilds the one global graph they all share (ARCHITECTURE.md §9).
        """
        with writer_lock(
            self.database.path, timeout=self.settings.storage.lock_timeout_seconds
        ) as path:
            yield path

    def sync_engine(self) -> SyncEngine:
        return SyncEngine(
            repositories=self.repositories,
            files=self.files,
            knowledge=self.knowledge,
            dependencies=self.dependencies,
            vectors=self.vectors,
            registry=self.registry,
            dependency_registry=self.dependency_registry,
            embeddings=self.embeddings(),
            settings=self.settings,
            git=self.git,
        )

    def retriever(self, limits: RetrievalLimits | None = None) -> Retriever:
        return Retriever(
            search=self.search,
            knowledge=self.knowledge,
            vectors=self.vectors,
            embeddings=self.embeddings(),
            limits=limits or RetrievalLimits.from_settings(self.settings.retrieval),
        )

    def topology(self) -> SqliteTopologyStore:
        """The set-based reads the topology capabilities make. Stateless and cheap."""
        return SqliteTopologyStore(self.database)

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
        dependencies=SqliteDependencyStore(database),
        search=SqliteSearchIndex(database),
        vectors=SqliteVectorIndex(database),
        registry=ParserRegistry(),
        dependency_registry=DependencyRegistry(),
        git=GitClient(),
    )
