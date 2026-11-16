"""T010 (US2): direct @obi mentions in an authorized channel are answered in-thread (104).

Everything else in channels stays exactly as it was: ordinary chatter is ignored by
construction, mentions in non-authorized channels get the existing "please DM me" pointer,
and edited/system channel events are dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeMembership,
    FakeThreadHistory,
    chat_config,
    chat_runtime,
    mention_event,
    save_inventory,
    slack_handler,
    text_response,
    user_message,
)

WAN = "C0FAKE66336"
BOT = "<@U_OBI>"


def _runtime(tmp_path: Path):  # noqa: ANN202 — test helper
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return chat_runtime(config, responses=[text_response("answer")])


def _wan_handler(runtime, **kwargs):  # noqa: ANN001, ANN202, ANN003 — test helper
    membership = kwargs.pop("membership", FakeMembership(rosters={WAN: {"U_WAN1"}}))
    return slack_handler(
        runtime,
        allowlist=["U_ADMIN"],
        membership=membership,
        authorized_channels=[WAN],
        **kwargs,
    )


def test_mention_in_authorized_channel_answered_in_thread(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_WAN1", channel=WAN, ts="300.1")
    )

    live = slack.live_texts()
    assert any("answer" in t for t in live)
    # Every post (working indicator included) is threaded under the triggering mention.
    assert all(post["thread_ts"] == "300.1" for post in slack.posts)
    # The model saw the question with obi's mention token stripped.
    first_user_message = runtime.client.calls[0]["messages"][0]
    assert "what's down?" in str(first_user_message)
    assert "U_OBI" not in str(first_user_message)


def test_threaded_mention_answers_in_thread_and_seeds_context(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    history = FakeThreadHistory(messages=[user_message("earlier team chatter", ts="290.0")])
    handler, slack = _wan_handler(runtime, history=history)

    handler.handle_event(
        mention_event(f"{BOT} and now?", user="U_WAN1", channel=WAN, ts="301.5", thread_ts="290.0")
    )

    assert any("answer" in t for t in slack.live_texts())
    assert all(post["thread_ts"] == "290.0" for post in slack.posts)
    assert history.calls == [(WAN, "290.0")]
    # The recovered history rides into the model call as the seeded context block.
    assert "earlier team chatter" in str(runtime.client.calls[0]["messages"])


def test_ordinary_channel_message_is_ignored(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime)

    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "channel",
        "user": "U_WAN1",
        "channel": WAN,
        "text": "hey team, lunch?",
        "ts": "302.1",
    }
    handler.handle_event(event)

    assert slack.posts == []
    assert runtime.client.calls == []


def test_mention_in_unconfigured_channel_gets_dm_pointer(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_WAN1", channel="C_OTHER", ts="303.1")
    )

    assert any("direct message" in t.lower() or "dm" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []


def test_bare_mention_gets_usage_hint_without_model_call(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime)

    handler.handle_event(mention_event(f"{BOT}", user="U_WAN1", channel=WAN, ts="304.1"))

    assert len(slack.posts) == 1
    assert slack.posts[0]["thread_ts"] == "304.1"
    assert "ask" in slack.posts[0]["text"].lower()
    assert runtime.client.calls == []


def test_unauthorized_sender_mention_refused_in_thread(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_STRANGER", channel=WAN, ts="305.1")
    )

    assert any("not authorized" in t.lower() for t in slack.texts())
    assert all(post["thread_ts"] == "305.1" for post in slack.posts)
    assert runtime.client.calls == []
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[-1]["action"] == "slack:unauthorized"
    assert records[-1]["outcome"] == "denied"
    assert records[-1]["actor"] == "U_STRANGER"


def test_membership_error_on_mention_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime, membership=FakeMembership(fail=True))

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_WAN1", channel=WAN, ts="306.1")
    )

    assert any("verify" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[-1]["action"] == "slack:auth_error"
    assert records[-1]["outcome"] == "error"


def test_edited_channel_message_is_ignored(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _wan_handler(runtime)

    event = mention_event(f"{BOT} what's down?", user="U_WAN1", channel=WAN, ts="307.1")
    event["subtype"] = "message_changed"
    handler.handle_event(event)

    assert slack.posts == []
    assert runtime.client.calls == []
