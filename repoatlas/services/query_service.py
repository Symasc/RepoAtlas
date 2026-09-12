"""V1's reasoning commands: ``ask`` and ``debug`` (V1_SPEC §16).

Both add exactly one step to deterministic retrieval: reasoning over the evidence
set that :mod:`repoatlas.services.retrieval_service` produced. The retrieval half
used to live here too, which is why a deterministic caller had to import a module
that reaches an HTTP client; it now has its own home and this module calls it.
"""

from __future__ import annotations

import asyncio

from repoatlas.errors import ReasoningModelUnavailableError
from repoatlas.llm.factory import build_model
from repoatlas.llm.protocol import ModelError, ReasoningModel
from repoatlas.llm.reasoner import QueryIntent, Reasoner, ReasoningResult
from repoatlas.models.context import RepositoryContext
from repoatlas.models.evidence import EvidenceSet
from repoatlas.services.retrieval_service import locate, locate_diagnostic
from repoatlas.services.workspace import Workspace


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

    evidence = (
        locate_diagnostic(workspace, question, context)
        if intent is QueryIntent.DEBUG
        else locate(workspace, question, context)
    )

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
