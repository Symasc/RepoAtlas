"""Evidence budget policy (V1_SPEC §14, §16).

What a caller may ask for, what the server grants, and how the granted answer
becomes internal retrieval bounds.

**Characters, not tokens.** A token count belongs to somebody else's tokenizer.
RepoAtlas does not have one and cannot acquire one without guessing, so the public
contract counts the thing it actually produces.

**Profiles, not knobs.** ``RetrievalLimits`` carries hop counts, fan-out, seed
counts, a primary-repository bias and a similarity floor. Those are tuning for a
ranking whose trust guarantees depend on them holding together (V1_SPEC §14): a
caller that could raise ``graph_seeds`` or drop ``semantic_floor`` could quietly
change which tier of evidence reaches the top, which is the one property evidence
is supposed to have. Three named profiles are exposed instead.

A limitation this module states rather than hides
-------------------------------------------------
:func:`retrieval_limits_for` converts a character budget into V1's
``token_budget`` at four characters per token. That is the ratio ``apply_budget``
already uses internally, so the conversion is faithful to the funnel — and the
funnel's bound is still **approximate**, for three reasons that live in
``retrieval/ranking.py``: the per-item estimate floors, the first item is admitted
whole whatever its size, and the estimate is computed before any truncation.

So this is a **pre-filter, not a guarantee**. Slice A defines and validates the
policy; nothing here claims V1 already enforces an exact character ceiling, and
nothing here changes what the retriever does. Exact enforcement, with line-aware
truncation at the boundary, belongs with the search capability.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.config.settings import Settings
from repoatlas.models.capability import AppliedBudget, EvidenceBudget, RetrievalBreadth
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS, RetrievalLimits

#: Characters per token, matching the estimate ``retrieval/ranking.py`` applies.
#: Stated once so the two cannot drift; see the module docstring for why the
#: resulting bound is approximate in both directions.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class BudgetProfile:
    """The defaults one :class:`RetrievalBreadth` stands for."""

    max_items: int
    max_total_chars: int
    max_item_chars: int


@dataclass(frozen=True, slots=True)
class BudgetCeiling:
    """The most the server will grant, whatever a caller asks for.

    Internal. A caller learns the numbers from :class:`AppliedBudget` after the
    fact rather than negotiating against them, so raising the ceiling never
    becomes a compatibility event.
    """

    max_items: int
    max_total_chars: int
    max_item_chars: int


def profile_for(breadth: RetrievalBreadth, settings: Settings) -> BudgetProfile:
    """The defaults for one breadth, derived from configured V1 settings.

    ``STANDARD`` is what ``locate`` retrieves and ``WIDE`` is what ``debug``
    retrieves, so the profiles are the two bounds V1 already ships rather than
    numbers invented for V2. ``FOCUSED`` halves the standard profile, for a caller
    that wants a short answer.

    ``max_item_chars`` is the chunk character cap at every breadth. It is a
    property of how content was stored, not of how widely to search: breadth
    decides how much evidence comes back, not how large a single indexed span is
    allowed to be.
    """
    retrieval = settings.retrieval
    item_chars = settings.indexing.max_chunk_chars

    if breadth is RetrievalBreadth.WIDE:
        return BudgetProfile(
            max_items=DIAGNOSTIC_LIMITS.final,
            max_total_chars=DIAGNOSTIC_LIMITS.token_budget * CHARS_PER_TOKEN,
            max_item_chars=item_chars,
        )
    if breadth is RetrievalBreadth.FOCUSED:
        return BudgetProfile(
            max_items=max(1, retrieval.final // 2),
            max_total_chars=max(1, retrieval.token_budget // 2 * CHARS_PER_TOKEN),
            max_item_chars=item_chars,
        )
    return BudgetProfile(
        max_items=retrieval.final,
        max_total_chars=retrieval.token_budget * CHARS_PER_TOKEN,
        max_item_chars=item_chars,
    )


def ceiling_for(settings: Settings) -> BudgetCeiling:
    """The server ceiling: the widest profile RepoAtlas offers.

    Pinned to ``WIDE`` deliberately. ``debug`` is the most evidence V1 will
    retrieve for one question, and a capability caller has no claim on more than
    the widest thing the product already does.
    """
    widest = profile_for(RetrievalBreadth.WIDE, settings)
    return BudgetCeiling(
        max_items=widest.max_items,
        max_total_chars=widest.max_total_chars,
        max_item_chars=widest.max_item_chars,
    )


def apply_budget_policy(
    requested: EvidenceBudget | None,
    settings: Settings,
    *,
    ceiling: BudgetCeiling | None = None,
) -> AppliedBudget:
    """Resolve a request against its profile and the server ceiling.

    An omitted field takes the profile default. A supplied field is honoured up to
    the ceiling and lowered to it otherwise — **never raised**: asking for less
    than the default is a legitimate request and silently widening it would return
    evidence the caller did not ask to pay for.

    Every lowering is named in :attr:`AppliedBudget.clamped`, so a request that
    did not fit is visible in the response rather than inferred from a short
    result.
    """
    budget = requested or EvidenceBudget()
    limit = ceiling or ceiling_for(settings)
    profile = profile_for(budget.breadth, settings)

    clamped: list[str] = []

    def settle(requested_value: int | None, default: int, allowed: int, field: str) -> int:
        if requested_value is None:
            # A profile default may itself exceed the ceiling if the two are
            # configured apart; the ceiling still wins, and quietly, because the
            # caller asked for nothing and so was refused nothing.
            return min(default, allowed)
        if requested_value > allowed:
            clamped.append(field)
            return allowed
        return requested_value

    return AppliedBudget(
        max_items=settle(budget.max_items, profile.max_items, limit.max_items, "max_items"),
        max_total_chars=settle(
            budget.max_total_chars,
            profile.max_total_chars,
            limit.max_total_chars,
            "max_total_chars",
        ),
        max_item_chars=settle(
            budget.max_item_chars,
            profile.max_item_chars,
            limit.max_item_chars,
            "max_item_chars",
        ),
        breadth=budget.breadth,
        clamped=tuple(clamped),
        ceiling_max_items=limit.max_items,
        ceiling_max_total_chars=limit.max_total_chars,
        ceiling_max_item_chars=limit.max_item_chars,
    )


def retrieval_limits_for(applied: AppliedBudget, settings: Settings) -> RetrievalLimits:
    """Internal retrieval bounds for a granted budget.

    The biases — hop count, fan-out, seeds, primary boost, semantic floor — come
    from configuration and are **not** derived from the budget, because they are
    not a quantity of evidence. Only the two caps a caller can move are set here.

    ``token_budget`` is the character budget divided by :data:`CHARS_PER_TOKEN`,
    rounded **up**, so this bound never cuts below what the caller was granted.
    It is a pre-filter and not the final ceiling; see the module docstring.
    """
    base = RetrievalLimits.from_settings(settings.retrieval)
    return base.widened(
        per_source=base.per_source,
        final=applied.max_items,
        token_budget=-(-applied.max_total_chars // CHARS_PER_TOKEN),
    )
