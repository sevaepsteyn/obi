"""T002 (feature 045): render_thread_context — attribution, filtering, bounds, edge cases."""

from __future__ import annotations

from obi.slack.handler import (
    _HISTORY_UNAVAILABLE,
    _TRANSPARENCY,
    _WORKING,
    MAX_SEEDED_CHARS,
    MAX_SEEDED_MESSAGES,
    render_thread_context,
)
from tests.fakes import bot_message, user_message


def test_attribution_and_order() -> None:
    fetched = [
        bot_message(":warning: *High error rate on sfp-plus-1*", ts="10.0"),
        user_message("is this bad?", ts="11.0", user="U_OP"),
        bot_message("## Watch report body", ts="12.0"),
    ]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    lines = block.splitlines()
    assert lines[0].startswith("[Slack thread context")
    assert lines[1] == "obi: :warning: *High error rate on sfp-plus-1*"
    assert lines[2] == "operator: is this bad?"
    assert lines[3] == "obi: ## Watch report body"


def test_triggering_reply_is_excluded() -> None:
    fetched = [
        bot_message("the report", ts="10.0"),
        user_message("what device is this?", ts="11.0"),
    ]
    block = render_thread_context(fetched, exclude_ts="11.0", replier="U_OP")
    assert block is not None
    assert "what device is this?" not in block
    assert "the report" in block


def test_bot_service_messages_are_filtered() -> None:
    fetched = [
        bot_message(_TRANSPARENCY + " Your configured network context is also included.", ts="1.0"),
        bot_message(_WORKING, ts="2.0"),
        bot_message(_HISTORY_UNAVAILABLE, ts="3.0"),
        bot_message("the actual report", ts="4.0"),
    ]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    assert _WORKING not in block
    assert "cloud reasoning model" not in block
    assert "the actual report" in block


def test_operator_text_resembling_service_message_survives() -> None:
    fetched = [user_message(_WORKING, ts="1.0"), bot_message("report", ts="2.0")]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    assert f"operator: {_WORKING}" in block


def test_other_user_is_labeled_with_id() -> None:
    fetched = [user_message("hello", ts="1.0", user="U_OTHER")]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    assert "operator (U_OTHER): hello" in block


def test_authorless_and_empty_messages_are_skipped() -> None:
    fetched = [{"ts": "1.0", "text": "no author"}, bot_message("", ts="2.0")]
    assert render_thread_context(fetched, exclude_ts="99.9", replier="U_OP") is None


def test_empty_history_returns_none() -> None:
    assert render_thread_context([], exclude_ts="99.9", replier="U_OP") is None


def test_message_count_bound_keeps_newest_and_notes_omission() -> None:
    fetched = [bot_message(f"msg {i}", ts=f"{i}.0") for i in range(MAX_SEEDED_MESSAGES + 5)]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    assert "(earlier messages omitted)" in block
    assert "obi: msg 0" not in block  # oldest dropped
    assert f"obi: msg {MAX_SEEDED_MESSAGES + 4}" in block  # newest kept
    assert len(block.splitlines()) == MAX_SEEDED_MESSAGES + 1  # + header


def test_char_bound_keeps_newest_and_notes_omission() -> None:
    big = "x" * (MAX_SEEDED_CHARS // 3)
    fetched = [bot_message(big, ts=f"{i}.0") for i in range(5)]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    assert "(earlier messages omitted)" in block
    assert len(block) <= MAX_SEEDED_CHARS + 200  # header slack only
    assert block.splitlines()[-1] == f"obi: {big}"  # newest survives


def test_no_bound_hit_means_no_omission_note() -> None:
    fetched = [bot_message("short report", ts="1.0")]
    block = render_thread_context(fetched, exclude_ts="99.9", replier="U_OP")
    assert block is not None
    assert "omitted" not in block
