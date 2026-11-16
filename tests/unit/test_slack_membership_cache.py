"""T003: the channel-membership TTL cache (feature 104, research §2).

`_WebClientMembership` wraps `conversations.members` behind a per-channel TTL cache with an
injected clock. Fetch errors must RAISE (membership unknown ⇒ the handler fails closed) — an
expired entry is never served as a fallback, keeping revocation bounded at the TTL.
"""

from __future__ import annotations

from typing import Any

import pytest

from obi.slack.listener import _WebClientMembership


class _FakeWeb:
    """A fake WebClient serving canned ``conversations_members`` pages per channel."""

    def __init__(self, pages: dict[str, list[dict[str, Any]]], *, fail: bool = False) -> None:
        self._pages = pages
        self.fail = fail
        self.calls: list[tuple[str, str | None]] = []

    def conversations_members(
        self, *, channel: str, limit: int, cursor: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((channel, cursor))
        if self.fail:
            raise RuntimeError("slack unreachable")
        pages = self._pages[channel]
        index = 0 if cursor is None else int(cursor)
        page = dict(pages[index])
        if index + 1 < len(pages):
            page["response_metadata"] = {"next_cursor": str(index + 1)}
        return page


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_first_call_fetches_members() -> None:
    web = _FakeWeb({"C1": [{"members": ["U1", "U2"]}]})
    membership = _WebClientMembership(web, ttl=300.0, now=_Clock())
    assert membership.members("C1") == {"U1", "U2"}
    assert len(web.calls) == 1


def test_within_ttl_serves_cache_without_api_call() -> None:
    clock = _Clock()
    web = _FakeWeb({"C1": [{"members": ["U1"]}]})
    membership = _WebClientMembership(web, ttl=300.0, now=clock)
    membership.members("C1")
    clock.now += 299.0
    assert membership.members("C1") == {"U1"}
    assert len(web.calls) == 1


def test_after_ttl_refetches() -> None:
    clock = _Clock()
    web = _FakeWeb({"C1": [{"members": ["U1"]}]})
    membership = _WebClientMembership(web, ttl=300.0, now=clock)
    membership.members("C1")
    clock.now += 300.0
    web._pages["C1"] = [{"members": ["U1", "U_NEW"]}]
    assert membership.members("C1") == {"U1", "U_NEW"}
    assert len(web.calls) == 2


def test_fetch_error_raises_and_no_stale_fallback() -> None:
    clock = _Clock()
    web = _FakeWeb({"C1": [{"members": ["U1"]}]})
    membership = _WebClientMembership(web, ttl=300.0, now=clock)
    membership.members("C1")
    clock.now += 300.0  # entry expired; the refetch fails → raise, never serve the stale set
    web.fail = True
    with pytest.raises(RuntimeError):
        membership.members("C1")


def test_channels_cached_independently() -> None:
    clock = _Clock()
    web = _FakeWeb({"C1": [{"members": ["U1"]}], "C2": [{"members": ["U2"]}]})
    membership = _WebClientMembership(web, ttl=300.0, now=clock)
    assert membership.members("C1") == {"U1"}
    assert membership.members("C2") == {"U2"}
    assert membership.members("C1") == {"U1"}
    assert len(web.calls) == 2  # one fetch per channel; C1's second read was cached


def test_paginated_member_lists_merge() -> None:
    web = _FakeWeb({"C1": [{"members": ["U1", "U2"]}, {"members": ["U3"]}]})
    membership = _WebClientMembership(web, ttl=300.0, now=_Clock())
    assert membership.members("C1") == {"U1", "U2", "U3"}
    assert [cursor for _, cursor in web.calls] == [None, "1"]
