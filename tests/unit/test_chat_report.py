"""Unit (feature 089, T010): report assembly — summaries, footer, truncation, web shape."""

from __future__ import annotations

import json

from obi.chat.agent import ConversationResult, ToolCall, TurnUsage
from obi.chat.report import (
    MAX_RESULT_CHARS,
    format_cost,
    format_tokens,
    slack_footer,
    tool_summary,
    web_report,
)


def _result(**overrides: object) -> ConversationResult:
    fields: dict[str, object] = {
        "answer": "all up",
        "tools_used": ["routeros_query", "routeros_query", "ping"],
        "tool_calls": [
            ToolCall("routeros_query", {"device": "core-gw-1"}, "IFACE-TABLE", False),
            ToolCall("routeros_query", {"device": "chestnut-gw"}, "error: unreachable", True),
            ToolCall("ping", {"target": "172.18.84.1"}, "3/3 replies", False),
        ],
        "usage": TurnUsage(
            input_tokens=812,
            output_tokens=310,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=11_620,
            model_calls=3,
        ),
    }
    fields.update(overrides)
    return ConversationResult(**fields)  # type: ignore[arg-type]


def test_tool_summary_counts_in_first_call_order() -> None:
    assert tool_summary(["routeros_query", "routeros_query", "ping"]) == "routeros_query ×2, ping"
    assert tool_summary(["ping"]) == "ping"
    assert tool_summary([]) == ""


def test_format_tokens_humanizes() -> None:
    assert format_tokens(812) == "812"
    assert format_tokens(12_431) == "12.4k"
    assert format_tokens(5_000) == "5k"
    assert format_tokens(1_204_000) == "1.2M"


def test_format_cost_states() -> None:
    assert format_cost(None) == "cost unknown"
    assert format_cost(0.0192) == "~$0.02"
    assert format_cost(0.004) == "~$0.004"
    assert format_cost(0.0) == "~$0.00"


def test_slack_footer_tools_and_usage() -> None:
    footer = slack_footer(_result(), "claude-sonnet-5")
    assert footer.startswith("_🔧 routeros_query ×2, ping · ")
    assert "12.4k in / 310 out" in footer
    assert footer.endswith("_")
    # Names only — no argument or result fragment ever (FR-005).
    assert "core-gw-1" not in footer
    assert "IFACE-TABLE" not in footer


def test_slack_footer_no_tool_turn_omits_tool_part() -> None:
    footer = slack_footer(_result(tools_used=[], tool_calls=[]), "claude-sonnet-5")
    assert "🔧" not in footer
    assert "12.4k in / 310 out" in footer


def test_slack_footer_unpriced_model_says_cost_unknown() -> None:
    assert "cost unknown" in slack_footer(_result(), "some-local-llm")


def test_web_report_shape_and_serializability() -> None:
    report = web_report(_result(), "claude-sonnet-5")
    assert report["model"] == "claude-sonnet-5"
    assert [t["name"] for t in report["tools"]] == ["routeros_query", "routeros_query", "ping"]
    assert report["tools"][0]["arguments"] == {"device": "core-gw-1"}
    assert report["tools"][0]["result"] == "IFACE-TABLE"
    assert report["tools"][0]["truncated"] is False
    assert report["tools"][1]["error"] is True
    assert report["usage"]["input_tokens"] == 812
    assert report["usage"]["cache_read_input_tokens"] == 11_620
    assert report["usage"]["model_calls"] == 3
    assert report["cost_usd"] is not None
    json.dumps(report)  # the whole report must survive the NDJSON encoder


def test_web_report_truncates_large_results_for_display_only() -> None:
    huge = "x" * (MAX_RESULT_CHARS + 500)
    result = _result(tool_calls=[ToolCall("routeros_query", {}, huge, False)])
    (tool,) = web_report(result, "claude-sonnet-5")["tools"]
    assert tool["truncated"] is True
    assert tool["result"].startswith("x" * 100)
    assert f"{len(huge)} chars total" in tool["result"]
    assert "audit trail" in tool["result"]
    # Display-only: the capture itself is untouched.
    assert result.tool_calls[0].result == huge


def test_web_report_unpriced_model_has_null_cost() -> None:
    assert web_report(_result(), "some-local-llm")["cost_usd"] is None
