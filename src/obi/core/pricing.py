"""The per-model price table and turn-cost estimator (feature 089, contracts/turn-report.md).

One table, one place (FR-008): both chat egresses derive their cost figure here. Static data
shipped with obi — no network fetch, no cloud dependency (Constitution VI); an unknown model id
degrades to ``None`` so surfaces render an explicit "cost unknown" instead of a wrong number
(FR-010). List prices in USD per million tokens, from Anthropic's published pricing
(claude-api reference, cached 2026-06-24); edit :data:`MODEL_PRICES` when models or prices
change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard is typing-only
    from obi.chat.agent import TurnUsage

# Prompt-cache billing relative to the input rate (5-minute ephemeral cache).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

_MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """USD per million input / output tokens for one model id."""

    input_per_mtok: float
    output_per_mtok: float


MODEL_PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-5": ModelPrice(3.00, 15.00),
    "claude-sonnet-4-6": ModelPrice(3.00, 15.00),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00),
    "claude-opus-5": ModelPrice(5.00, 25.00),
    "claude-opus-4-8": ModelPrice(5.00, 25.00),
    "claude-opus-4-7": ModelPrice(5.00, 25.00),
    "claude-opus-4-6": ModelPrice(5.00, 25.00),
    "claude-fable-5": ModelPrice(10.00, 50.00),
}


def estimate_cost(model: str, usage: TurnUsage) -> float | None:
    """The estimated USD cost of one turn, or ``None`` when ``model`` has no price entry.

    Each input class is billed at its own rate: uncached input at the list price, cache writes
    at 1.25x, cache reads at 0.1x. The arithmetic is deliberately trivial (SC-003: a hand
    calculation from the reported tokens must match exactly).
    """

    price = MODEL_PRICES.get(model)
    if price is None:
        return None
    input_cost = (
        usage.input_tokens
        + usage.cache_creation_input_tokens * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_input_tokens * CACHE_READ_MULTIPLIER
    ) * price.input_per_mtok
    output_cost = usage.output_tokens * price.output_per_mtok
    return (input_cost + output_cost) / _MTOK
