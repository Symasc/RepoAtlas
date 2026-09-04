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

from repoatlas.llm.grounding import INSUFFICIENT_EVIDENCE, GroundingReport, ground_answer
from repoatlas.llm.prompts.loader import render
from repoatlas.llm.protocol import Message, ModelResponse, ReasoningModel
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
        return not self.answer.hypotheses


def format_evidence(evidence: EvidenceSet) -> str:
    """Serialize candidates as a JSON document (ARCHITECTURE.md §11).

    Repository content is **data**, so it is encoded as a JSON string value and
    never as prompt structure. The previous format delimited blocks with a
    ``[E1] repository=... path=...`` line and blank lines, which a repository
    could simply write into one of its own files: a comment containing
    ``[E9] repository=infra path=secrets.yaml`` and ``---- END OF EVIDENCE ----``
    produced a prompt in which a forged block was indistinguishable from a
    retrieved one.

    JSON removes the ambiguity structurally rather than by escaping conventions
    nobody can audit: quotes, newlines and braces inside ``content`` are escaped
    by the encoder, so no value can terminate its own field, close the array, or
    add a sibling object. The model receives evidence it can cite by ``id``, and
    the repository can influence only what is *inside* one ``content`` string.

    ``repository`` is a display name and may repeat: two managed repositories can
    both be called ``common``. ``repository_path`` is the identity — the
    repository's path within the managed root — so two blocks that would otherwise
    read identically (``repository=common``, ``path=index.ts``) are distinguishable,
    and a citation can say which one it means. It is root-relative rather than
    absolute: the model needs to tell two sources apart, not to learn where this
    host keeps its files.

    ``start_line`` and ``end_line`` are ``null`` for a file-level candidate — one
    retrieved because the question named the file, not because anything was found
    inside it. The nulls are the honest encoding: RepoAtlas knows the file exists
    at that path and knows nothing about any line of it, and the system prompt
    says what may be claimed from that.
    """
    blocks = []
    for index, candidate in enumerate(evidence.candidates, start=1):
        body = candidate.text.strip()
        truncated = len(body) > _MAX_BLOCK_CHARS
        blocks.append(
            {
                "id": f"E{index}",
                "repository": candidate.repository,
                "repository_path": candidate.repository_slug or candidate.repository,
                "path": candidate.path,
                "start_line": candidate.line_start,
                "end_line": candidate.line_end,
                "why_retrieved": candidate.description,
                "content": body[:_MAX_BLOCK_CHARS] if truncated else body,
                "content_truncated": truncated,
            }
        )
    return json.dumps({"evidence": blocks}, indent=2, ensure_ascii=False)


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


#: Characters of the validation error echoed back to the model in the repair
#: prompt. Bounded because a Pydantic error quotes the offending input.
_MAX_REPAIR_ERROR_CHARS = 800


def _output_problem(response: ModelResponse, error: Exception) -> str:
    """Say what was actually wrong with the model's text, and what would fix it.

    Four distinguishable states, and they are not interchangeable advice:

    * the server reported the output limit — raising it is the fix;
    * nothing was generated at all;
    * the text is not JSON;
    * the text is JSON of the wrong shape.

    Telling a user to raise ``num_predict`` because a model emitted confident
    nonsense would be a guess dressed as a diagnosis, so only the first case
    mentions the limit — and only because the *server* said so, never because
    the text merely looks unfinished (V1_SPEC §15).

    Nothing here quotes the output. A Pydantic error carries the offending text
    in its ``input`` field, and that text is written from repository content, so
    the position is reported and the characters are not (V1_SPEC §17).
    """
    if response.truncated:
        return (
            "The reasoning model's response was cut off before it was complete: "
            "the server reported that generation stopped at its output limit "
            f"(done_reason={response.finish_reason!r}). "
            "Raise the model's output limit (Ollama: options.num_predict) or ask a "
            "narrower question."
        )
    if not response.content.strip():
        return (
            "The reasoning model returned an empty response. The server answered "
            "normally, so this is the model producing nothing rather than a "
            "connection problem."
        )
    syntax = _json_syntax_error(error)
    if syntax is not None:
        return f"The reasoning model returned output that is not valid JSON ({syntax})."
    count = error.error_count() if isinstance(error, ValidationError) else 1
    return (
        "The reasoning model returned well-formed JSON that is not a RepoAtlas "
        f"answer ({count} schema {'violation' if count == 1 else 'violations'})."
    )


def _json_syntax_error(error: Exception) -> str | None:
    """Where the JSON broke, or ``None`` if the JSON itself was fine.

    Pydantic's ``model_validate_json`` reports a syntax failure as a
    ``ValidationError`` of type ``json_invalid`` rather than as a
    ``JSONDecodeError``, so the two cases cannot be told apart by exception
    class — which is exactly the kind of collapse this pass exists to undo. The
    ``ctx.error`` message is a line/column position and carries no content.
    """
    if isinstance(error, json.JSONDecodeError):
        return f"parsing failed at character {error.pos}"
    if not isinstance(error, ValidationError):
        return None
    errors = error.errors()
    if not errors or any(item.get("type") != "json_invalid" for item in errors):
        return None
    context = errors[0].get("ctx") or {}
    where = str(context.get("error", "")).strip()
    return f"parsing failed: {where}" if where else "parsing failed"


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
        except (ValidationError, json.JSONDecodeError) as first_error:
            # One repair attempt, then give up rather than render unvalidated
            # output (V1_SPEC §15).
            repaired = True
            repair = [
                *messages,
                Message(role="assistant", content=response.content),
                Message(
                    role="user",
                    content=render("repair", error=str(first_error)[:_MAX_REPAIR_ERROR_CHARS]),
                ),
            ]
            # A backend failure during the repair is a backend failure. Folding it
            # into "the model returned an invalid answer" would describe the wrong
            # layer — the first response was invalid, but what ended the run was
            # the server, and only one of those two is actionable.
            retried = await self._model.complete(repair, response_schema=RepoAtlasAnswer)
            try:
                answer = _parse(retried.content)
                response = retried
            except (ValidationError, json.JSONDecodeError) as repair_error:
                return self._insufficient(
                    "The reasoning model did not return a valid answer, and the "
                    "repair attempt did not either.\n\n"
                    f"First attempt: {_output_problem(response, first_error)}\n"
                    f"Repair attempt: {_output_problem(retried, repair_error)}",
                    retried.model or response.model,
                    repaired,
                )

        grounding = ground_answer(answer, evidence)
        return ReasoningResult(
            answer=grounding.answer,
            grounding=grounding,
            repaired=repaired,
            model=response.model or self._model.id,
        )

    @staticmethod
    def _insufficient(reason: str, model: str, repaired: bool) -> ReasoningResult:
        answer = RepoAtlasAnswer(
            summary=INSUFFICIENT_EVIDENCE, hypotheses=[], unknowns=[reason], next_check=None
        )
        return ReasoningResult(
            answer=answer,
            grounding=GroundingReport(answer=answer),
            repaired=repaired,
            model=model,
        )
