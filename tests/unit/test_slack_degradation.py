"""T022 (US4): the handler degrades cleanly and never leaks data into channels (feature 009)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.inventory import InventoryEntry
from obi.core.llm import ChatUnavailable, LLMResponse
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeSlackClient,
    chat_config,
    chat_runtime,
    dm_event,
    mention_event,
    save_inventory,
    slack_handler,
    text_response,
    tool_response,
)


class _UnavailableClient:
    """An LLM client that is always offline (raises :class:`ChatUnavailable`)."""

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> LLMResponse:
        raise ChatUnavailable("could not reach the reasoning model (offline?).")


def _seed(tmp_path: Path) -> object:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return config


def test_model_unavailable_degrades_without_crashing(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("unused")])
    runtime.client = _UnavailableClient()
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("what's down?", user="U_OP", channel="D1", ts="1.1"))

    assert any("reasoning model" in t.lower() for t in slack.texts())


def test_slack_post_failure_does_not_crash(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(
        config, responses=[tool_response("inventory_list", {}), text_response("ok")]
    )
    poster = FakeSlackClient(fail=True)
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], poster=poster)

    handler.handle_event(dm_event("list devices", user="U_OP", channel="D1", ts="1.1"))

    assert poster.posts == []  # every post raised, but the handler did not crash


def test_channel_mention_yields_no_network_answer(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("must not run")])
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(mention_event("hey obi what's down", user="U_OP", channel="C1"))

    assert runtime.client.calls == []  # no reasoning-model access
    assert all("thinking" not in t.lower() for t in slack.texts())  # no answer flow
    assert any("send me a dm" in t.lower() for t in slack.texts())  # only a "please DM" nudge
    audit = tmp_path / "audit.jsonl"
    assert not audit.exists() or audit.read_text() == ""  # no data-access record


def test_bot_and_subtype_and_empty_events_ignored(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("x")])
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(
        {"type": "message", "channel_type": "im", "bot_id": "B1", "channel": "D1", "ts": "1.1"}
    )
    handler.handle_event(
        {
            "type": "message",
            "channel_type": "im",
            "subtype": "message_changed",
            "user": "U_OP",
            "channel": "D1",
            "text": "edited",
            "ts": "1.2",
        }
    )
    handler.handle_event(dm_event("   ", user="U_OP", channel="D1", ts="1.3"))  # empty text

    assert runtime.client.calls == []
    assert slack.posts == []
