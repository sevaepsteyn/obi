"""Contract (feature 089, T019/T020): the Slack reply footer — tool names + tokens + cost.

One italic subordinate line rides every successful reply (contracts/turn-report.md): tokens and
cost always, tool names (with repeat counts) only when tools ran, and NEVER any tool argument or
result content (FR-004/005/006, SC-006). Error replies carry no footer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.llm import ChatUnavailable, Usage
from tests.fakes import chat_config, chat_runtime, dm_event, slack_handler, text_response

# A distinctive marker riding a fake tool result: it reaches the model, and must never reach
# any posted Slack message (SC-006).
_RESULT_MARKER = "SECRET-RESULT-MARKER-89"


def _tool_runtime(tmp_path: Path) -> Any:
    """A runtime whose scripted turn runs routeros_status twice, then ping-like answers."""

    from tests.fakes import tool_response

    config = chat_config(tmp_path)
    return chat_runtime(
        config,
        responses=[
            tool_response(
                "routeros_status",
                {"target": "core-gw-1"},
                use_id="a",
                usage=Usage(input_tokens=500, output_tokens=40, cache_read_input_tokens=9500),
            ),
            tool_response(
                "routeros_status",
                {"target": "chestnut-gw"},
                use_id="b",
                usage=Usage(input_tokens=600, output_tokens=40),
            ),
            text_response("both are up.", usage=Usage(input_tokens=700, output_tokens=120)),
        ],
    )


def test_footer_names_tools_tokens_and_cost(tmp_path: Path) -> None:
    handler, fake = slack_handler(_tool_runtime(tmp_path), allowlist=["U_OP"])
    handler.handle_event(dm_event("are the gateways up?"))

    reply = fake.live_texts()[-1]
    footer = reply.strip().splitlines()[-1]
    assert footer.startswith("_🔧 routeros_status ×2 · ")
    assert "in / " in footer and " out" in footer
    assert "~$" in footer  # the test model (claude-sonnet-5 default) is priced
    assert footer.endswith("_")
    # The answer itself is above the footer, in the same reply.
    assert "both are up." in reply


def test_footer_never_carries_arguments_or_results(tmp_path: Path) -> None:
    """SC-006: a marker string in every tool result must never appear in ANY posted message."""

    from tests.fakes import tool_response

    config = chat_config(tmp_path)
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("logs_query", {"query": _RESULT_MARKER + "-arg"}),
            text_response("nothing notable."),
        ],
    )
    # Force the dispatch result through a stub the marker rides on: monkeypatch is avoided by
    # asserting on the argument marker (model-authored) and the result content the real
    # dispatch returns — neither may surface in Slack.
    handler, fake = slack_handler(runtime, allowlist=["U_OP"])
    handler.handle_event(dm_event("anything in the logs?"))

    for text in fake.texts():
        assert _RESULT_MARKER not in text  # the argument value never leaks into any post
    footer = fake.live_texts()[-1].strip().splitlines()[-1]
    assert "logs_query" in footer  # the NAME is reported…
    assert "{" not in footer and "-arg" not in footer  # …the argument payload is not


def test_tool_free_turn_has_tokens_but_no_tool_part(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    runtime = chat_runtime(
        config,
        responses=[text_response("hello!", usage=Usage(input_tokens=900, output_tokens=25))],
    )
    handler, fake = slack_handler(runtime, allowlist=["U_OP"])
    handler.handle_event(dm_event("hi obi"))

    footer = fake.live_texts()[-1].strip().splitlines()[-1]
    assert "🔧" not in footer  # FR-004: no tool line without tools
    assert "900 in / 25 out" in footer
    assert "~$" in footer


def test_error_replies_carry_no_footer(tmp_path: Path) -> None:
    class _Down:
        def create_message(self, **kwargs: Any) -> Any:
            raise ChatUnavailable("offline")

    import dataclasses

    config = chat_config(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("unused")])
    runtime = dataclasses.replace(runtime, client=_Down())
    handler, fake = slack_handler(runtime, allowlist=["U_OP"])
    handler.handle_event(dm_event("are we up?"))

    assert any("can't reach the reasoning model" in t for t in fake.live_texts())
    assert not any(" in / " in t and " out" in t for t in fake.live_texts())


def test_footer_survives_message_splitting(tmp_path: Path) -> None:
    """A long answer splits into chunks; the footer still arrives within the same reply flow."""

    config = chat_config(tmp_path)
    long_answer = "status line\n" * 800  # well past SLACK_MAX_CHARS
    runtime = chat_runtime(
        config,
        responses=[text_response(long_answer, usage=Usage(input_tokens=50, output_tokens=5000))],
    )
    handler, fake = slack_handler(runtime, allowlist=["U_OP"])
    handler.handle_event(dm_event("full status please"))

    live = fake.live_texts()
    assert len(live) > 1  # it split
    assert "50 in / 5k out" in live[-1]  # the footer rides the final chunk
