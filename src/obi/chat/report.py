"""Per-turn report assembly (feature 089, contracts/turn-report.md).

Pure functions from the loop's capture (:class:`ConversationResult`) to each egress's idiom —
the web ``answer`` event's ``report`` object and the Slack footer line. The 082 pattern
(`core/textfmt.py`): canonical data in core, one formatting seam per channel, zero I/O.
"""

from __future__ import annotations

from typing import Any

from obi.chat.agent import ConversationResult, TurnUsage
from obi.core.pricing import estimate_cost

# Display bound for one tool result in the web report (FR-011). Truncation is display-only —
# the audit trail (042) keeps the full text.
MAX_RESULT_CHARS = 4000


def tool_summary(tools_used: list[str]) -> str:
    """Tool names in first-call order with ``xN`` repeat counts: ``routeros_query ×2, ping``."""

    counts: dict[str, int] = {}
    for name in tools_used:
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in counts.items())


def format_tokens(count: int) -> str:
    """Humanize a token count: ``812``, ``12.4k``, ``1.2M``."""

    for bound, suffix in ((1_000_000, "M"), (1_000, "k")):
        if count >= bound:
            value = f"{count / bound:.1f}".removesuffix(".0")
            return f"{value}{suffix}"
    return str(count)


def format_cost(cost: float | None) -> str:
    """``~$0.02`` / sub-cent ``~$0.004`` / explicit ``cost unknown`` when unpriced (FR-010)."""

    if cost is None:
        return "cost unknown"
    if cost >= 0.01 or cost == 0:
        return f"~${cost:.2f}"
    return f"~${cost:.3f}"


def usage_line(usage: TurnUsage, cost: float | None) -> str:
    """The token/cost part shared by both surfaces: ``12.4k in / 310 out · ~$0.02``."""

    tokens = f"{format_tokens(usage.total_input())} in / {format_tokens(usage.output_tokens)} out"
    return f"{tokens} · {format_cost(cost)}"


def slack_footer(result: ConversationResult, model: str) -> str:
    """The one italic mrkdwn line under a Slack reply — names only, never arguments/results.

    Tokens+cost always (FR-006); the tool part only when tools ran (FR-004/005).
    """

    line = usage_line(result.usage, estimate_cost(model, result.usage))
    if result.tools_used:
        line = f"🔧 {tool_summary(result.tools_used)} · {line}"
    return f"_{line}_"


def _display_result(result: str) -> tuple[str, bool]:
    if len(result) <= MAX_RESULT_CHARS:
        return result, False
    marker = f"… [truncated for display — {len(result)} chars total; full text in the audit trail]"
    return result[:MAX_RESULT_CHARS] + marker, True


def web_report(result: ConversationResult, model: str) -> dict[str, Any]:
    """The ``report`` object for the web ``answer`` event (contract shape, JSON-safe).

    Results are display-truncated (FR-011) but otherwise byte-identical to what the model saw —
    the tool layer already redacted them, and no other transformation is applied (FR-009).
    """

    tools: list[dict[str, Any]] = []
    for call in result.tool_calls:
        display, truncated = _display_result(call.result)
        tools.append(
            {
                "name": call.name,
                "arguments": call.arguments,
                "result": display,
                "truncated": truncated,
                "error": call.error,
            }
        )
    usage = result.usage
    return {
        "model": model,
        "tools": tools,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "model_calls": usage.model_calls,
        },
        "cost_usd": estimate_cost(model, usage),
    }
