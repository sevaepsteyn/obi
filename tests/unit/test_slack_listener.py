"""T026: the listener dispatches only live events (FR-017) and requires a [slack] config."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.slack.listener import SlackListener, _extract_event
from tests.fakes import chat_config, chat_runtime, text_response


def test_extract_event_only_returns_real_events() -> None:
    # A real events_api payload yields its event…
    assert _extract_event({"event": {"type": "message", "text": "hi"}}) == {
        "type": "message",
        "text": "hi",
    }
    # …while control/reconnect frames (no event) yield None — nothing is dispatched/replayed.
    assert _extract_event({}) is None
    assert _extract_event({"event": None}) is None
    assert _extract_event(None) is None


def test_listener_requires_slack_config(tmp_path: Path) -> None:
    config = chat_config(tmp_path)  # no [slack] section configured
    runtime = chat_runtime(config, responses=[text_response("x")])
    with pytest.raises(ValueError, match="slack"):
        SlackListener(config, runtime)


class _FakeRepliesWeb:
    """A WebClient double for `conversations_replies` cursor pagination (feature 045)."""

    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def conversations_replies(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def test_web_client_history_follows_cursor_pagination() -> None:
    from obi.slack.listener import _WebClientHistory

    web = _FakeRepliesWeb(
        [
            {
                "messages": [{"ts": "1.0", "text": "parent"}],
                "response_metadata": {"next_cursor": "c2"},
            },
            {"messages": [{"ts": "2.0", "text": "reply"}], "response_metadata": {}},
        ]
    )
    history = _WebClientHistory(web)

    messages = history.fetch_thread("D1", "1.0")

    assert [m["ts"] for m in messages] == ["1.0", "2.0"]
    assert len(web.calls) == 2
    assert web.calls[0]["channel"] == "D1" and web.calls[0]["ts"] == "1.0"
    assert "cursor" not in web.calls[0]
    assert web.calls[1]["cursor"] == "c2"


def test_web_client_history_caps_pages() -> None:
    from obi.slack.listener import HISTORY_MAX_PAGES, _WebClientHistory

    page = {"messages": [{"ts": "1.0"}], "response_metadata": {"next_cursor": "again"}}
    web = _FakeRepliesWeb([dict(page) for _ in range(HISTORY_MAX_PAGES + 3)])
    history = _WebClientHistory(web)

    messages = history.fetch_thread("D1", "1.0")

    assert len(web.calls) == HISTORY_MAX_PAGES
    assert len(messages) == HISTORY_MAX_PAGES
