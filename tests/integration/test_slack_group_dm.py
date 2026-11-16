"""Group DMs (mpim): @obi mentions in a multi-person DM are answered, not pointed to DM.

A group DM is a conversation obi was deliberately added to, so a direct mention there flows
through the same sender-authorization gate and in-thread answer as an authorized-channel
mention (feature 104). Plain group-DM chatter stays ignored, strangers are refused with an
audit, and an unreadable conversation type fails closed with the honest "couldn't verify".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeConversations,
    FakeMembership,
    chat_config,
    chat_runtime,
    mention_event,
    save_inventory,
    slack_handler,
    text_response,
)

WAN = "C0FAKE66336"
TRIO = "G_TRIO"  # the multi-person DM: operator + a colleague + obi
BOT = "<@U_OBI>"


def _runtime(tmp_path: Path):  # noqa: ANN202 — test helper
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return chat_runtime(config, responses=[text_response("answer")])


def _trio_handler(runtime, **kwargs):  # noqa: ANN001, ANN202, ANN003 — test helper
    conversations = kwargs.pop("conversations", FakeConversations(mpims={TRIO}))
    membership = kwargs.pop("membership", FakeMembership(rosters={WAN: {"U_WAN1"}}))
    return slack_handler(
        runtime,
        allowlist=["U_ADMIN"],
        membership=membership,
        authorized_channels=[WAN],
        conversations=conversations,
        **kwargs,
    )


def test_group_dm_mention_by_operator_answered_in_thread(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    conversations = FakeConversations(mpims={TRIO})
    handler, slack = _trio_handler(runtime, conversations=conversations)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_ADMIN", channel=TRIO, ts="400.1")
    )

    assert any("answer" in t for t in slack.live_texts())
    # Never the "please DM" pointer — obi is IN this conversation.
    assert not any("dm" in t.lower() for t in slack.texts())
    assert all(post["thread_ts"] == "400.1" for post in slack.posts)
    assert conversations.calls == [TRIO]
    # The model saw the question with obi's mention token stripped.
    first_user_message = runtime.client.calls[0]["messages"][0]
    assert "what's down?" in str(first_user_message)
    assert "U_OBI" not in str(first_user_message)


def test_group_dm_mention_by_channel_member_answered(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _trio_handler(runtime)

    handler.handle_event(
        mention_event(f"{BOT} is the wan up?", user="U_WAN1", channel=TRIO, ts="401.1")
    )

    assert any("answer" in t for t in slack.live_texts())
    assert all(post["thread_ts"] == "401.1" for post in slack.posts)


def test_group_dm_mention_by_stranger_refused(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _trio_handler(runtime)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_STRANGER", channel=TRIO, ts="402.1")
    )

    assert any("not authorized" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[-1]["action"] == "slack:unauthorized"
    assert records[-1]["outcome"] == "denied"
    assert records[-1]["actor"] == "U_STRANGER"


def test_group_dm_bare_mention_gets_usage_hint(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _trio_handler(runtime)

    handler.handle_event(mention_event(f"{BOT}", user="U_ADMIN", channel=TRIO, ts="403.1"))

    assert len(slack.posts) == 1
    assert "ask" in slack.posts[0]["text"].lower()
    assert runtime.client.calls == []


def test_mention_in_ordinary_channel_still_gets_pointer(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    conversations = FakeConversations(mpims={TRIO})
    handler, slack = _trio_handler(runtime, conversations=conversations)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_ADMIN", channel="C_OTHER", ts="404.1")
    )

    assert any("dm" in t.lower() for t in slack.texts())
    assert conversations.calls == ["C_OTHER"]
    assert runtime.client.calls == []


def test_unreadable_conversation_type_fails_closed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = _trio_handler(runtime, conversations=FakeConversations(fail=True))

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_ADMIN", channel=TRIO, ts="405.1")
    )

    assert any("verify" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[-1]["action"] == "slack:auth_error"
    assert records[-1]["outcome"] == "error"


def test_authorized_channel_mention_skips_classification(tmp_path: Path) -> None:
    # A mention in the authorized channel must never spend a conversations.info call.
    runtime = _runtime(tmp_path)
    conversations = FakeConversations(mpims={TRIO})
    handler, slack = _trio_handler(runtime, conversations=conversations)

    handler.handle_event(
        mention_event(f"{BOT} what's down?", user="U_WAN1", channel=WAN, ts="406.1")
    )

    assert any("answer" in t for t in slack.live_texts())
    assert conversations.calls == []


def test_group_dm_plain_chatter_is_ignored(tmp_path: Path) -> None:
    # Humans talking to each other in the group DM never reach obi — only direct mentions do.
    runtime = _runtime(tmp_path)
    handler, slack = _trio_handler(runtime)

    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "mpim",
        "user": "U_ADMIN",
        "channel": TRIO,
        "text": "peter, lunch?",
        "ts": "407.1",
    }
    handler.handle_event(event)

    assert slack.posts == []
    assert runtime.client.calls == []
