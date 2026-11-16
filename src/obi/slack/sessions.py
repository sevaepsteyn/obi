"""Ephemeral per-thread conversation sessions for the Slack interface (feature 009).

A session is the multi-turn chat transcript for one Slack DM reply thread, keyed by
``(channel_id, thread_root_ts)`` — implementing the spec's *thread = session* rule: a top-level
DM starts a new session rooted at its ``ts``; replies in that thread continue it; a new top-level
DM starts a fresh one. Nothing is persisted (a restart starts everyone fresh), and the map is
bounded (oldest evicted) so a long-lived listener does not grow without limit. Access is
lock-guarded for the listener's worker pool.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

# Bound the in-memory session map so a long-lived listener cannot grow without limit; the oldest
# (least-recently-used) thread is evicted past this. Not operator-configurable in v1 (YAGNI).
MAX_SESSIONS = 200

SessionKey = tuple[str, str]


class SessionStore:
    """A bounded, thread-safe LRU map of ``(channel, thread_root)`` → message transcript."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[SessionKey, list[dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_sessions

    def get_or_create(self, key: SessionKey) -> tuple[bool, list[dict[str, Any]]]:
        """Return ``(is_new, messages)`` for ``key``, creating an empty transcript if absent.

        The returned list is the stored object: appending to it (as the chat loop does) persists
        the turn for the next message in the same thread. ``is_new`` is True only the first time a
        key is seen, so the caller can post the one-time transparency notice.
        """

        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                self._sessions.move_to_end(key)
                return False, existing
            messages: list[dict[str, Any]] = []
            self._sessions[key] = messages
            self._sessions.move_to_end(key)
            while len(self._sessions) > self._max:
                self._sessions.popitem(last=False)
            return True, messages
