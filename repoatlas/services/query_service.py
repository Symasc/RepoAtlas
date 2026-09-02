"""``repoatlas locate`` and the retrieval half of ``ask``/``debug`` (V1_SPEC §16).

``locate`` stops at the evidence set, which is why it needs no reasoning model.
"""

from __future__ import annotations

from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceSet
from repoatlas.services.workspace import Workspace


def locate(workspace: Workspace, question: str, context: RepositoryContext) -> EvidenceSet:
    """Retrieve file-backed evidence for a question. No LLM involved."""
    return workspace.retriever().retrieve(question, context)
