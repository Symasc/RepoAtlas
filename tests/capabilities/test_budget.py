"""Evidence budget policy (V1_SPEC §14, §16).

The budget is the caller's only lever over how much evidence comes back, and it
is deliberately a small one. ``RetrievalLimits`` carries hop counts, fan-out, seed
counts, a primary-repository bias and a similarity floor; those are tuning for a
ranking whose trust guarantees depend on them holding together, so a caller that
could reach them could quietly change which tier of evidence wins. The public
contract is three named profiles and three character caps.

What this suite does **not** assert is that the retriever honours the character
caps exactly. It does not, and Slice A does not claim it does: V1's bound is a
token estimate with a floor, a first-item exemption and pre-truncation
accounting. Exact enforcement is the search capability's job, and asserting it
here would pin a guarantee nothing currently provides.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.budget import (
    CHARS_PER_TOKEN,
    apply_budget_policy,
    ceiling_for,
    profile_for,
    retrieval_limits_for,
)
from repoatlas.config.settings import Settings
from repoatlas.models.capability import AppliedBudget, EvidenceBudget, RetrievalBreadth
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS

#: Raw funnel parameters a caller must never reach. Named individually because
#: the failure is additive: one of them appearing on a public model is enough.
FORBIDDEN_KNOBS = (
    "graph_hops",
    "graph_fanout",
    "graph_seeds",
    "primary_boost",
    "semantic_limit",
    "semantic_floor",
    "per_source",
    "token_budget",
    "final",
)


@pytest.fixture
def default_settings() -> Settings:
    return Settings()


# -- defaults --------------------------------------------------------------------


def test_the_standard_profile_is_what_locate_already_retrieves(
    default_settings: Settings,
) -> None:
    """Profiles are the bounds V1 ships, not numbers invented for V2."""
    profile = profile_for(RetrievalBreadth.STANDARD, default_settings)
    assert profile.max_items == default_settings.retrieval.final
    assert profile.max_total_chars == default_settings.retrieval.token_budget * CHARS_PER_TOKEN
    assert profile.max_item_chars == default_settings.indexing.max_chunk_chars


def test_the_wide_profile_is_what_debug_already_retrieves(default_settings: Settings) -> None:
    profile = profile_for(RetrievalBreadth.WIDE, default_settings)
    assert profile.max_items == DIAGNOSTIC_LIMITS.final
    assert profile.max_total_chars == DIAGNOSTIC_LIMITS.token_budget * CHARS_PER_TOKEN


def test_the_focused_profile_is_narrower_than_standard(default_settings: Settings) -> None:
    focused = profile_for(RetrievalBreadth.FOCUSED, default_settings)
    standard = profile_for(RetrievalBreadth.STANDARD, default_settings)
    assert focused.max_items < standard.max_items
    assert focused.max_total_chars < standard.max_total_chars


def test_an_empty_request_takes_the_standard_profile(default_settings: Settings) -> None:
    applied = apply_budget_policy(None, default_settings)
    standard = profile_for(RetrievalBreadth.STANDARD, default_settings)
    assert applied.max_items == standard.max_items
    assert applied.max_total_chars == standard.max_total_chars
    assert applied.breadth is RetrievalBreadth.STANDARD
    assert applied.clamped == ()


def test_choosing_a_breadth_moves_the_defaults(default_settings: Settings) -> None:
    applied = apply_budget_policy(EvidenceBudget(breadth=RetrievalBreadth.WIDE), default_settings)
    assert applied.max_items == DIAGNOSTIC_LIMITS.final
    assert applied.breadth is RetrievalBreadth.WIDE


# -- narrowing and clamping ---------------------------------------------------------


def test_a_caller_may_ask_for_less(default_settings: Settings) -> None:
    """Asking for less is legitimate, and is never widened back to the default."""
    applied = apply_budget_policy(
        EvidenceBudget(max_items=3, max_total_chars=500, max_item_chars=120), default_settings
    )
    assert (applied.max_items, applied.max_total_chars, applied.max_item_chars) == (3, 500, 120)
    assert applied.clamped == ()
    assert applied.was_clamped is False


def test_asking_for_more_than_the_ceiling_is_clamped_and_said_so(
    default_settings: Settings,
) -> None:
    ceiling = ceiling_for(default_settings)
    applied = apply_budget_policy(
        EvidenceBudget(max_items=10_000, max_total_chars=10_000_000, max_item_chars=10_000_000),
        default_settings,
    )
    assert applied.max_items == ceiling.max_items
    assert applied.max_total_chars == ceiling.max_total_chars
    assert applied.max_item_chars == ceiling.max_item_chars
    assert applied.clamped == ("max_items", "max_total_chars", "max_item_chars")
    assert applied.was_clamped is True


def test_a_clamp_is_reported_per_field(default_settings: Settings) -> None:
    """A caller learns which of its numbers did not fit, not merely that one did not."""
    applied = apply_budget_policy(
        EvidenceBudget(max_items=10_000, max_total_chars=100), default_settings
    )
    assert applied.clamped == ("max_items",)
    assert applied.max_total_chars == 100


def test_the_ceiling_travels_with_the_answer(default_settings: Settings) -> None:
    """So adjusting a request never needs a second round trip to discover the limit."""
    ceiling = ceiling_for(default_settings)
    applied = apply_budget_policy(EvidenceBudget(max_items=10_000), default_settings)
    assert applied.ceiling_max_items == ceiling.max_items
    assert applied.ceiling_max_total_chars == ceiling.max_total_chars
    assert applied.ceiling_max_item_chars == ceiling.max_item_chars


def test_the_ceiling_is_the_widest_profile(default_settings: Settings) -> None:
    """A caller has no claim on more than the widest thing V1 already does."""
    ceiling = ceiling_for(default_settings)
    widest = profile_for(RetrievalBreadth.WIDE, default_settings)
    assert (ceiling.max_items, ceiling.max_total_chars) == (
        widest.max_items,
        widest.max_total_chars,
    )


def test_even_the_widest_breadth_cannot_exceed_the_ceiling(default_settings: Settings) -> None:
    applied = apply_budget_policy(
        EvidenceBudget(breadth=RetrievalBreadth.WIDE, max_items=10_000), default_settings
    )
    assert applied.max_items == ceiling_for(default_settings).max_items


# -- validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda value: EvidenceBudget(max_items=value),
        lambda value: EvidenceBudget(max_total_chars=value),
        lambda value: EvidenceBudget(max_item_chars=value),
    ],
    ids=["max_items", "max_total_chars", "max_item_chars"],
)
@pytest.mark.parametrize("value", [0, -1, -10_000])
def test_zero_and_negative_budgets_are_refused(
    build: Callable[[int], EvidenceBudget], value: int
) -> None:
    """Zero is not "no limit"; omission is. A zero budget asks for nothing at all."""
    with pytest.raises(ValidationError):
        build(value)


def test_an_omitted_field_means_the_default_rather_than_no_limit(
    default_settings: Settings,
) -> None:
    applied = apply_budget_policy(EvidenceBudget(max_items=2), default_settings)
    standard = profile_for(RetrievalBreadth.STANDARD, default_settings)
    assert applied.max_items == 2
    assert applied.max_total_chars == standard.max_total_chars


# -- the public surface ----------------------------------------------------------------


@pytest.mark.parametrize("model", [EvidenceBudget, AppliedBudget])
def test_no_raw_retrieval_knob_is_reachable_from_a_public_budget(model: type) -> None:
    exposed = set(model.model_fields)
    for knob in FORBIDDEN_KNOBS:
        assert knob not in exposed, f"{model.__name__} exposes {knob}"


def test_the_public_budget_counts_characters_and_never_tokens() -> None:
    """A token belongs to somebody else's tokenizer. A character is ours."""
    for model in (EvidenceBudget, AppliedBudget):
        names = " ".join(model.model_fields)
        assert "token" not in names, model.__name__
        assert "chars" in names, model.__name__


def test_the_budget_models_carry_no_transport_or_vendor_field() -> None:
    for model in (EvidenceBudget, AppliedBudget):
        for field in model.model_fields:
            assert "session" not in field
            assert "client" not in field


# -- the internal mapping, and what it does not claim -------------------------------------


def test_the_internal_limits_keep_configured_biases_out_of_the_callers_hands(
    default_settings: Settings,
) -> None:
    applied = apply_budget_policy(EvidenceBudget(max_items=5), default_settings)
    limits = retrieval_limits_for(applied, default_settings)
    assert limits.final == 5
    assert limits.graph_hops == default_settings.retrieval.graph_hops
    assert limits.graph_seeds == default_settings.retrieval.graph_seeds
    assert limits.primary_boost == default_settings.retrieval.primary_boost
    assert limits.semantic_floor == default_settings.retrieval.semantic_floor


def test_the_token_prefilter_rounds_up_so_it_never_cuts_below_the_grant(
    default_settings: Settings,
) -> None:
    """A pre-filter, not the ceiling. Rounding down would silently narrow the grant."""
    applied = apply_budget_policy(EvidenceBudget(max_total_chars=4001), default_settings)
    limits = retrieval_limits_for(applied, default_settings)
    assert limits.token_budget == 1001
    assert limits.token_budget * CHARS_PER_TOKEN >= applied.max_total_chars
