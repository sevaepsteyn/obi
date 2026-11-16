"""T006/T009 (feature 045): seeding unknown threads with their Slack history, and degradation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from obi.slack.handler import _HISTORY_UNAVAILABLE
from tests.fakes import (
    FakeThreadHistory,
    bot_message,
    chat_config,
    chat_runtime,
    dm_event,
    save_inventory,
    slack_handler,
    text_response,
    user_message,
)


def _seed(tmp_path: Path) -> object:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return config


def _first_turn_messages(runtime: Any) -> list[dict[str, Any]]:
    return list(runtime.client.calls[0]["messages"])


REPORT = ":warning: *Climbing error counters on core-gw-1 sfp-plus-1*"
REPORT_BODY = "## Watch report\ncore-gw-1 sfp-plus-1 in-errors rose 40 in 1h"


def test_reply_under_report_thread_seeds_the_report(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("it's the sfp port")])
    history = FakeThreadHistory(
        messages=[bot_message(REPORT, ts="70.0"), bot_message(REPORT_BODY, ts="70.1")]
    )
    handler, poster = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event(
            "what device is this about?", user="U_OP", channel="D1", ts="71.0", thread_ts="70.0"
        )
    )

    assert history.calls == [("D1", "70.0")]
    messages = _first_turn_messages(runtime)
    assert messages[0]["role"] == "user"
    assert "Slack thread context" in str(messages[0]["content"])
    assert REPORT in str(messages[0]["content"])
    assert REPORT_BODY.splitlines()[1] in str(messages[0]["content"])
    assert messages[1] == {"role": "user", "content": "what device is this about?"}
    # 089: the reply carries the answer plus the subordinate usage footer.
    assert any(text.startswith("it's the sfp port") for text in poster.texts())


def test_digest_thread_seeds_every_threaded_report(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("two findings")])
    history = FakeThreadHistory(
        messages=[
            bot_message("obi watch — 2 findings this cycle", ts="80.0"),
            bot_message("report one body", ts="80.1"),
            bot_message("report two body", ts="80.2"),
        ]
    )
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event("summarize these", user="U_OP", channel="D1", ts="81.0", thread_ts="80.0")
    )

    seeded = str(_first_turn_messages(runtime)[0]["content"])
    assert "report one body" in seeded
    assert "report two body" in seeded


def test_second_reply_in_same_thread_does_not_refetch(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a1"), text_response("a2")])
    history = FakeThreadHistory(messages=[bot_message(REPORT, ts="70.0")])
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event("first reply", user="U_OP", channel="D1", ts="71.0", thread_ts="70.0")
    )
    handler.handle_event(
        dm_event("second reply", user="U_OP", channel="D1", ts="72.0", thread_ts="70.0")
    )

    assert len(history.calls) == 1
    second_turn = runtime.client.calls[1]["messages"]
    assert any(REPORT in str(m.get("content")) for m in second_turn)  # context persists


def test_top_level_dm_never_fetches(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a1")])
    history = FakeThreadHistory(messages=[bot_message(REPORT, ts="70.0")])
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(dm_event("fresh question", user="U_OP", channel="D1", ts="90.0"))

    assert history.calls == []
    assert "Slack thread context" not in str(_first_turn_messages(runtime)[0]["content"])


def test_unauthorized_threaded_reply_fetches_nothing(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[])
    history = FakeThreadHistory(messages=[bot_message(REPORT, ts="70.0")])
    handler, poster = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event("let me in", user="U_EVIL", channel="D9", ts="71.0", thread_ts="70.0")
    )

    assert history.calls == []
    assert runtime.client.calls == []
    assert any("not authorized" in text for text in poster.texts())


def test_pre_restart_chat_thread_recovers_context(tmp_path: Path) -> None:
    """US2: after a restart (fresh sessions), a reply into an old chat thread is seeded."""

    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("gw uptime is 4d")])
    history = FakeThreadHistory(
        messages=[
            user_message("tell me about gw", ts="60.0"),
            bot_message("gw is a RouterOS gateway at 172.25.110.1", ts="60.1"),
        ]
    )
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event("and its uptime?", user="U_OP", channel="D1", ts="61.0", thread_ts="60.0")
    )

    seeded = str(_first_turn_messages(runtime)[0]["content"])
    assert "operator: tell me about gw" in seeded
    assert "obi: gw is a RouterOS gateway at 172.25.110.1" in seeded


def test_fetch_failure_posts_notice_and_still_answers(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("blind answer"), text_response("a2")])
    history = FakeThreadHistory(fail=True)
    handler, poster = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event("what's this about?", user="U_OP", channel="D1", ts="71.0", thread_ts="70.0")
    )

    assert _HISTORY_UNAVAILABLE in poster.texts()
    # 089: the reply carries the answer plus the subordinate usage footer.
    assert any(text.startswith("blind answer") for text in poster.texts())
    assert _first_turn_messages(runtime)[0] == {"role": "user", "content": "what's this about?"}

    # not sticky: the session now exists, the next turn neither refetches nor re-notices
    handler.handle_event(
        dm_event("follow-up", user="U_OP", channel="D1", ts="72.0", thread_ts="70.0")
    )
    assert len(history.calls) == 1
    assert poster.texts().count(_HISTORY_UNAVAILABLE) == 1


def test_empty_thread_seeds_nothing_and_posts_no_notice(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a1")])
    history = FakeThreadHistory(messages=[])
    handler, poster = slack_handler(runtime, allowlist=["U_OP"], history=history)

    handler.handle_event(
        dm_event("hello?", user="U_OP", channel="D1", ts="71.0", thread_ts="70.0")
    )

    assert history.calls == [("D1", "70.0")]
    assert _first_turn_messages(runtime)[0] == {"role": "user", "content": "hello?"}
    assert _HISTORY_UNAVAILABLE not in poster.texts()
