"""Conditions a deployment link holds under, and how they are evaluated against a context.

A condition is ``{kind, op, value}`` -- with ``name`` for a ``variable`` -- and a link's
conditions hold together (AND). Evaluation never guesses: a context that does not say
what a condition asks about leaves it ``unknown``, never a match; conditions that cannot
all hold at once are a ``conflict`` whatever the context.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConditionKind = Literal["branch", "environment", "pull_request", "variable"]
ConditionOp = Literal["equals", "glob"]
#: ``none``: no conditions, so nothing narrows the link.
ConditionState = Literal["match", "no_match", "unknown", "conflict", "none"]

#: Longest value or name a condition may carry.
MAX_CONDITION_TEXT = 200
#: Conditions one link may carry.
MAX_CONDITIONS = 16


class DeploymentCondition(BaseModel):
    """One condition: the context's ``kind`` (a ``variable`` by ``name``) ``op`` ``value``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ConditionKind
    op: ConditionOp = "equals"
    value: str = Field(min_length=1, max_length=MAX_CONDITION_TEXT)
    #: The variable's name; required for ``variable`` and refused otherwise.
    name: str | None = Field(default=None, min_length=1, max_length=MAX_CONDITION_TEXT)

    @model_validator(mode="after")
    def _named(self) -> DeploymentCondition:
        if (self.kind == "variable") != (self.name is not None):
            raise ValueError("a variable condition, and only one, names its variable")
        if (
            self.kind == "pull_request"
            and self.op == "equals"
            and self.value
            not in {
                "true",
                "false",
            }
        ):
            raise ValueError("a pull_request condition equals `true` or `false`")
        return self


class ConditionContext(BaseModel):
    """What is known about the situation a link is evaluated in; ``None`` is unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    branch: str | None = Field(default=None, max_length=MAX_CONDITION_TEXT)
    environment: str | None = Field(default=None, max_length=MAX_CONDITION_TEXT)
    pull_request: bool | None = None
    variables: dict[str, str] = Field(default_factory=dict)


def _subject(condition: DeploymentCondition, context: ConditionContext) -> str | None:
    if condition.kind == "branch":
        return context.branch
    if condition.kind == "environment":
        return context.environment
    if condition.kind == "pull_request":
        return None if context.pull_request is None else str(context.pull_request).lower()
    return context.variables.get(condition.name or "")


def _holds(condition: DeploymentCondition, subject: str) -> bool:
    if condition.op == "glob":
        return fnmatch.fnmatchcase(subject, condition.value)
    return subject == condition.value


def _conflicting(conditions: Sequence[DeploymentCondition]) -> bool:
    """Whether two conditions on one subject cannot both hold: two different ``equals``,
    or an ``equals`` its own subject's ``glob`` refuses."""
    by_subject: dict[tuple[str, str | None], list[DeploymentCondition]] = {}
    for condition in conditions:
        by_subject.setdefault((condition.kind, condition.name), []).append(condition)
    for group in by_subject.values():
        fixed = {c.value for c in group if c.op == "equals"}
        if len(fixed) > 1:
            return True
        if fixed:
            (value,) = fixed
            if any(c.op == "glob" and not fnmatch.fnmatchcase(value, c.value) for c in group):
                return True
    return False


def evaluate(
    conditions: Sequence[DeploymentCondition], context: ConditionContext
) -> ConditionState:
    """Whether every condition holds in ``context``: ``no_match`` as soon as one known
    subject refuses, ``unknown`` when none refuses but one subject is not known."""
    if not conditions:
        return "none"
    if _conflicting(conditions):
        return "conflict"
    unknown = False
    for condition in conditions:
        subject = _subject(condition, context)
        if subject is None:
            unknown = True
        elif not _holds(condition, subject):
            return "no_match"
    return "unknown" if unknown else "match"
