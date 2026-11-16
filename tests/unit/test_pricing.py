"""Unit (feature 089, T008): the price table and turn-cost arithmetic (SC-003 hand-checkable)."""

from __future__ import annotations

import pytest

from obi.chat.agent import TurnUsage
from obi.core.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MODEL_PRICES,
    estimate_cost,
)


def test_cost_matches_hand_calculation() -> None:
    # sonnet-5 list price: $3/MTok in, $15/MTok out.
    usage = TurnUsage(input_tokens=1_000_000, output_tokens=100_000)
    assert estimate_cost("claude-sonnet-5", usage) == pytest.approx(3.00 + 1.50)


def test_cache_classes_priced_at_their_multipliers() -> None:
    usage = TurnUsage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    expected = 3.00 * CACHE_WRITE_MULTIPLIER + 3.00 * CACHE_READ_MULTIPLIER
    assert estimate_cost("claude-sonnet-5", usage) == pytest.approx(expected)


def test_typical_warm_turn() -> None:
    # A realistic warm chat turn: mostly cache reads, a little fresh input, a short answer.
    usage = TurnUsage(
        input_tokens=800,
        output_tokens=310,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=11_620,
        model_calls=3,
    )
    expected = (800 * 3.00 + 11_620 * 0.10 * 3.00 + 310 * 15.00) / 1_000_000
    assert estimate_cost("claude-sonnet-5", usage) == pytest.approx(expected)


def test_unknown_model_returns_none() -> None:
    assert estimate_cost("some-local-llm", TurnUsage(input_tokens=10)) is None


def test_zero_usage_is_zero_cost() -> None:
    assert estimate_cost("claude-sonnet-5", TurnUsage()) == 0.0


def test_operator_models_are_priced() -> None:
    # The shipped table must at least cover the configured default and its predecessor.
    assert "claude-sonnet-5" in MODEL_PRICES  # [chat].model default
    assert "claude-sonnet-4-6" in MODEL_PRICES
