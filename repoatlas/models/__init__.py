"""Pydantic domain models shared across RepoAtlas.

Per ARCHITECTURE.md §1 rule 5, this package imports nothing from other RepoAtlas
packages, so it can be imported anywhere without creating cycles.
"""

from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)
from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.models.reports import RepositorySyncReport, StatusReport, SyncReport
from repoatlas.models.repository import FileState, IndexedFile, Repository, SyncStatus

__all__ = [
    "ContextScope",
    "EdgeType",
    "EntityType",
    "Evidence",
    "EvidenceCandidate",
    "EvidenceSet",
    "FileState",
    "Hypothesis",
    "IndexedFile",
    "ParseDiagnostic",
    "ParsedFile",
    "ParsedReference",
    "ParsedSymbol",
    "RepoAtlasAnswer",
    "Repository",
    "RepositoryContext",
    "RepositorySyncReport",
    "Severity",
    "StatusReport",
    "SyncReport",
    "SyncStatus",
]
