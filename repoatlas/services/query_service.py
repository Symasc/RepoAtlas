"""Query commands: ``locate``, ``ask`` and ``debug`` (V1_SPEC §16).

``locate`` stops at the evidence set, which is why it needs no reasoning model.
``ask`` and ``debug`` add exactly one step: reasoning over that same evidence.
"""

from __future__ import annotations

import asyncio

from repoatlas.errors import ReasoningModelUnavailableError
from repoatlas.llm.factory import build_model
from repoatlas.llm.protocol import ModelError, ReasoningModel
from repoatlas.llm.reasoner import QueryIntent, Reasoner, ReasoningResult
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceSet
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS
from repoatlas.services.workspace import Workspace


def locate(workspace: Workspace, question: str, context: RepositoryContext) -> EvidenceSet:
    """Retrieve file-backed evidence for a question. No LLM involved."""
    return workspace.retriever().retrieve(question, context)


def retrieve_for(
    workspace: Workspace, question: str, context: RepositoryContext, intent: QueryIntent
) -> EvidenceSet:
    """Retrieve with the bias appropriate to the command (V1_SPEC §16)."""
    if intent is QueryIntent.DEBUG:
        # Bias toward exact error strings: the whole phrase is searched
        # literally, which matches log and exception text embedded in source.
        return workspace.retriever(limits=DIAGNOSTIC_LIMITS).retrieve(
            question, context, literal_phrase=True
        )
    return workspace.retriever().retrieve(question, context)


def reason(
    workspace: Workspace,
    question: str,
    context: RepositoryContext,
    intent: QueryIntent,
    *,
    model: ReasoningModel | None = None,
) -> tuple[EvidenceSet, ReasoningResult]:
    """Retrieve evidence, then reason about it.

    Availability is checked before any work is wasted, so an absent model
    produces the documented exit-4 message rather than a traceback.
    """
    backend = model if model is not None else _build(workspace)

    if not asyncio.run(backend.available()):
        raise ReasoningModelUnavailableError(
            "No reasoning model is available.",
            detail=f"{backend.availability_detail()}\n\n"
            'Evidence-only mode needs no model:\n\n  repoatlas locate "<question>"',
        )

    evidence = retrieve_for(workspace, question, context, intent)

    try:
        result = asyncio.run(Reasoner(backend).reason(question, evidence, intent))
    except ModelError as exc:
        # Every backend failure is exit 4, but the message is the backend's own
        # account of which layer failed — never a generic reachability claim
        # (ARCHITECTURE.md §11).
        raise ReasoningModelUnavailableError(exc.message, detail=exc.detail) from exc

    return evidence, result


def _build(workspace: Workspace) -> ReasoningModel:
    try:
        return build_model(workspace.settings.llm)
    except ModelError as exc:
        # Every backend failure is exit 4, but the message is the backend's own
        # account of which layer failed — never a generic reachability claim
        # (ARCHITECTURE.md §11).
        raise ReasoningModelUnavailableError(exc.message, detail=exc.detail) from exc
