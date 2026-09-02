"""Grounded reasoning over a bounded evidence set (ARCHITECTURE.md §11).

    EvidenceSet
       -> prompt construction (evidence labelled with repo/path:line)
       -> ReasoningModel.complete(response_schema=RepoAtlasAnswer)
       -> Pydantic validation --fail--> one repair attempt --fail--> INSUFFICIENT
       -> citation verification   <- the enforcement point
       -> Renderer

This layer never touches storage. It receives evidence already retrieved and
bounded, and returns a validated, verified answer.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from repoatlas.llm.grounding import INSUFFICIENT_EVIDENCE, GroundingReport, verify_citations
from repoatlas.llm.prompts.loader import render
from repoatlas.llm.protocol import Message, ModelUnavailable, ReasoningModel
from repoatlas.models.evidence import EvidenceSet, RepoAtlasAnswer

#: Characters of a single evidence block passed to the model. The retriever has
#: already applied the overall budget; this bounds one pathological chunk.
_MAX_BLOCK_CHARS = 2000

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class QueryIntent(enum.StrEnum):
    """Which prompt and rendering a question gets (V1_SPEC §16)."""

    ASK = "ask"
    DEBUG = "debug"


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    """The verified answer plus what verification had to change."""

    answer: RepoAtlasAnswer
    grounding: GroundingReport
    repaired: bool = False
    model: str = ""

    @property
    def is_insufficient(self) -> bool:
        return not self.answer.findings


def format_evidence(evidence: EvidenceSet) -> str:
    """Render candidates as labelled, citable blocks."""
    if not evidence.candidates:
        return "(no evidence was retrieved)"

    blocks: list[str] = []
    for index, candidate in enumerate(evidence.candidates, start=1):
        body = candidate.text.strip()
        if len(body) > _MAX_BLOCK_CHARS:
            body = f"{body[:_MAX_BLOCK_CHARS]}\n... (truncated)"
        header = (
            f"[E{index}] repository={candidate.repository} path={candidate.path} "
            f"lines={candidate.line_start}-{candidate.line_end}"
        )
        if candidate.description:
            header = f"{header} ({candidate.description})"
        blocks.append(f"{header}\n{body}" if body else header)
    return "\n\n".join(blocks)


def _scope_note(evidence: EvidenceSet) -> str:
    if evidence.primary_repository:
        return (
            f"The question was asked inside repository {evidence.primary_repository!r}, "
            "so prefer explanations grounded there, but follow the evidence wherever "
            "it leads."
        )
    return "The question was asked across all managed repositories."


def build_messages(question: str, evidence: EvidenceSet, intent: QueryIntent) -> list[Message]:
    """Construct the prompt for one question."""
    user = render(
        str(intent),
        question=question,
        scope=_scope_note(evidence),
        evidence=format_evidence(evidence),
    )
    return [
        Message(role="system", content=render("system")),
        Message(role="user", content=user),
    ]


def _extract_json(content: str) -> str:
    """Recover the JSON object from a response that wrapped it in prose."""
    fenced = _FENCE.search(content)
    if fenced:
        return fenced.group(1).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        return content[start : end + 1]
    return content.strip()


def _parse(content: str) -> RepoAtlasAnswer:
    return RepoAtlasAnswer.model_validate_json(_extract_json(content))


class Reasoner:
    """Turns evidence into a validated, citation-verified answer."""

    def __init__(self, model: ReasoningModel) -> None:
        self._model = model

    async def reason(
        self, question: str, evidence: EvidenceSet, intent: QueryIntent
    ) -> ReasoningResult:
        messages = build_messages(question, evidence, intent)

        response = await self._model.complete(messages, response_schema=RepoAtlasAnswer)
        repaired = False

        try:
            answer = _parse(response.content)
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            # One repair attempt, then give up rather than render unvalidated
            # output (V1_SPEC §15).
            repaired = True
            repair = [
                *messages,
                Message(role="assistant", content=response.content),
                Message(role="user", content=render("repair", error=str(first_error)[:800])),
            ]
            try:
                retried = await self._model.complete(repair, response_schema=RepoAtlasAnswer)
                answer = _parse(retried.content)
                response = retried
            except (ValidationError, ValueError, json.JSONDecodeError, ModelUnavailable):
                return self._insufficient(
                    "The reasoning model did not return a valid answer.", response.model, repaired
                )

        grounding = verify_citations(answer, evidence)
        return ReasoningResult(
            answer=grounding.answer,
            grounding=grounding,
            repaired=repaired,
            model=response.model or self._model.id,
        )

    @staticmethod
    def _insufficient(reason: str, model: str, repaired: bool) -> ReasoningResult:
        answer = RepoAtlasAnswer(
            summary=INSUFFICIENT_EVIDENCE, findings=[], unknowns=[reason], next_check=None
        )
        return ReasoningResult(
            answer=answer,
            grounding=GroundingReport(answer=answer),
            repaired=repaired,
            model=model,
        )
