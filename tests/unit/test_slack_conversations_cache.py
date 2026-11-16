"""The conversation-type cache behind the group-DM mention fix.

`_WebClientConversations` wraps `conversations.info` behind a lifetime cache (a conversation's
type never changes). A ``missing_scope`` refusal is a positive group-DM verdict — the token
reads every channel type a mention can arrive from, so unreadable metadata means an mpim (and
``app_mention`` never fires in a 1:1 im); granting ``mpim:read`` later makes the call succeed
instead. Any other error must RAISE so the handler fails closed.
"""

from __future__ import annotations

from typing import Any

import pytest

from obi.slack.listener import _WebClientConversations


class _FakeWeb:
    """A fake WebClient serving canned ``conversations_info`` verdicts per channel."""

    def __init__(self, kinds: dict[str, Any]) -> None:
        # kinds: channel → True/False (is_mpim) | "missing_scope" | "down"
        self._kinds = kinds
        self.calls: list[str] = []

    def conversations_info(self, *, channel: str) -> dict[str, Any]:
        self.calls.append(channel)
        kind = self._kinds[channel]
        if kind == "missing_scope":
            raise _ApiError({"error": "missing_scope", "needed": "mpim:read"})
        if kind == "down":
            raise RuntimeError("slack unreachable")
        return {"channel": {"id": channel, "is_mpim": kind}}


class _ApiError(Exception):
    """Shaped like slack_sdk's SlackApiError: carries a dict-like ``response``."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(response.get("error"))
        self.response = response


def test_mpim_and_channel_verdicts() -> None:
    conversations = _WebClientConversations(_FakeWeb({"G1": True, "C1": False}))
    assert conversations.is_mpim("G1") is True
    assert conversations.is_mpim("C1") is False


def test_verdicts_cache_for_the_listener_lifetime() -> None:
    web = _FakeWeb({"G1": True})
    conversations = _WebClientConversations(web)
    assert conversations.is_mpim("G1") is True
    assert conversations.is_mpim("G1") is True
    assert web.calls == ["G1"]


def test_missing_scope_is_a_group_dm_verdict_and_caches() -> None:
    web = _FakeWeb({"G1": "missing_scope"})
    conversations = _WebClientConversations(web)
    assert conversations.is_mpim("G1") is True
    assert conversations.is_mpim("G1") is True
    assert web.calls == ["G1"]  # the refusal verdict caches like any other


def test_other_errors_raise_and_do_not_cache() -> None:
    web = _FakeWeb({"C1": "down"})
    conversations = _WebClientConversations(web)
    with pytest.raises(RuntimeError):
        conversations.is_mpim("C1")
    web._kinds["C1"] = False  # Slack recovers: the next ask fetches instead of serving an error
    assert conversations.is_mpim("C1") is False
