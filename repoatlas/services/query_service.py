"""Query commands: ``locate``, ``ask`` and ``debug`` (V1_SPEC §16).

``locate`` stops at the evidence set, which is why it needs no reasoning model.
``ask`` and ``debug`` add exactly one step: reasoning over that same evidence.
"""

from __future__ import annotations

import asyncio

from repoatlas.errors import ReasoningModelUnavailableError
from repoatlas.llm.factory import build_model
from repoatlas.llm.protocol import ModelUnavailable, ReasoningModel
from repoatlas.llm.reasoner import QueryIntent, Reasoner, ReasoningResult
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceSet
from repoatlas.retrieval.retriever import RetrievalLimits
from repoatlas.services.workspace import Workspace

#: A diagnosis benefits from more surrounding evidence than a direct lookup, so
#: ``debug`` retrieves a wider set within the same token budget.
_DEBUG_LIMITS = RetrievalLimits(per_source=40, final=18, token_budget=8000)


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
        return workspace.retriever(limits=_DEBUG_LIMITS).retrieve(
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
    except ModelUnavailable as exc:
        raise ReasoningModelUnavailableError(exc.message, detail=exc.detail) from exc

    return evidence, result


def _build(workspace: Workspace) -> ReasoningModel:
    try:
        return build_model(workspace.settings.llm)
    except ModelUnavailable as exc:
        raise ReasoningModelUnavailableError(exc.message, detail=exc.detail) from exc
