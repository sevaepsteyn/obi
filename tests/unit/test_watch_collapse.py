"""US3 — in-window duplicate collapse (feature 062, A3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from obi.core.models import LogEvent
from obi.core.watch import collapse_window

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _ev(host: str, minutes: int, msg: str, *, topics: str = "system,error", sev: str = "error"):
    ts = NOW + timedelta(minutes=minutes)
    return LogEvent(
        ts=ts, host=host, topics=topics, message=msg, subsystem=topics.split(",")[0],
        severity=sev, raw_line=msg, collectors=["core"],
    )


def test_identical_repeats_collapse_with_count_and_span() -> None:
    cand = [(i + 1, _ev("sw-1", i, "no storage space")) for i in range(300)]
    cand.append((301, _ev("sw-1", 300, "a distinct line")))
    entries = collapse_window(cand)

    assert len(entries) == 2  # 300 repeats → 1 line, + the distinct one
    storm = entries[0]
    assert storm.count == 300
    assert storm.first_ts == NOW and storm.last_ts == NOW + timedelta(minutes=299)
    assert storm.event.message == "no storage space"  # representative = first occurrence
    assert entries[1].count == 1


def test_no_merge_across_hosts() -> None:
    entries = collapse_window([(1, _ev("sw-1", 0, "same")), (2, _ev("sw-2", 1, "same"))])
    assert len(entries) == 2 and all(e.count == 1 for e in entries)


def test_no_merge_across_message_or_severity() -> None:
    entries = collapse_window([
        (1, _ev("sw-1", 0, "one")),
        (2, _ev("sw-1", 1, "two")),
        (3, _ev("sw-1", 2, "one", sev="warning")),  # same text, different severity
    ])
    assert len(entries) == 3


def test_whitespace_normalized_merge() -> None:
    entries = collapse_window([(1, _ev("sw-1", 0, "a   b")), (2, _ev("sw-1", 1, "a b"))])
    assert len(entries) == 1 and entries[0].count == 2


def test_accounting_is_lossless() -> None:
    cand = [(i + 1, _ev("sw-1", i, "dup")) for i in range(10)]
    entries = collapse_window(cand)
    assert sum(e.count for e in entries) == 10  # true count preserved
    assert len(entries) == 1


def test_first_occurrence_order_preserved() -> None:
    entries = collapse_window([
        (1, _ev("a", 0, "x")), (2, _ev("b", 1, "y")), (3, _ev("a", 2, "x")),
    ])
    assert [e.event.host for e in entries] == ["a", "b"]  # a first (its first occurrence is oldest)
