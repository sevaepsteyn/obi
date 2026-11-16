"""Unit (feature 013): query_events filters (host, time window, severity, full-text) + bounding.

Extended by 038 with the web-tail additions: severity FLOORS (``min_severity``) and the
filtered ``query_since`` — one shared WHERE builder, so both read paths must agree.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from obi.core import logstore
from obi.core.models import LogEvent


def _seed(tmp_path: Path) -> Path:
    mirror = tmp_path / "logs"
    day = mirror / "core-vms-1" / "2026" / "06" / "03"
    day.mkdir(parents=True)
    (day / "chestnut-gw.log").write_text(
        "Jun  3 10:00:00.000000 chestnut-gw dhcp,info assigned 100.90.13.55 to phone\n"
        "Jun  3 11:00:00.000000 chestnut-gw system,error login failure for admin\n"
    )
    (day / "core-gw.log").write_text(
        "Jun  3 12:00:00.000000 core-gw ospf,info neighbor 172.18.81.1 up\n"
    )
    db = tmp_path / "logs.db"
    conn = logstore.connect(db)
    logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()
    return db


def test_host_substring_filter(tmp_path: Path) -> None:
    conn = logstore.connect_readonly(_seed(tmp_path))
    try:
        hosts = {e.host for e in logstore.query_events(conn, host="chestnut")}
        assert hosts == {"chestnut-gw"}
    finally:
        conn.close()


def test_severity_filter(tmp_path: Path) -> None:
    conn = logstore.connect_readonly(_seed(tmp_path))
    try:
        events = logstore.query_events(conn, severity="error")
        assert len(events) == 1 and events[0].severity == "error"
    finally:
        conn.close()


def test_time_window_is_half_open(tmp_path: Path) -> None:
    conn = logstore.connect_readonly(_seed(tmp_path))
    try:
        since = datetime(2026, 6, 3, 11, 0, 0, tzinfo=UTC)
        until = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
        events = logstore.query_events(conn, since=since, until=until)
        # 11:00 included (>= since), 12:00 excluded (< until), 10:00 before the window.
        assert [e.subsystem for e in events] == ["system"]
    finally:
        conn.close()


def test_full_text_contains(tmp_path: Path) -> None:
    conn = logstore.connect_readonly(_seed(tmp_path))
    try:
        events = logstore.query_events(conn, contains="login failure")
        assert len(events) == 1 and "login failure" in events[0].message
    finally:
        conn.close()


def test_newest_first_and_limit(tmp_path: Path) -> None:
    conn = logstore.connect_readonly(_seed(tmp_path))
    try:
        events = logstore.query_events(conn, limit=2)
        assert len(events) == 2
        # Ordered by timestamp DESC: core-gw 12:00 first, then chestnut-gw 11:00.
        assert [e.host for e in events] == ["core-gw", "chestnut-gw"]
    finally:
        conn.close()


# --- 038: severity floors + the filtered tail ---------------------------------------------------

BASE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _event(
    minute: int,
    *,
    host: str = "core-gw-1",
    severity: str | None = "info",
    message: str = "link up",
    collector: str = "core",
) -> LogEvent:
    return LogEvent(
        ts=BASE + timedelta(minutes=minute),
        host=host,
        topics=f"wireless,{severity}" if severity else "wireless",
        message=message,
        subsystem="wireless",
        severity=severity,
        raw_line=f"raw {minute}",
        collectors=[collector],
    )


@pytest.fixture()
def store(tmp_path: Path) -> sqlite3.Connection:
    conn = logstore.connect(tmp_path / "logs.db")
    events = [
        _event(0, severity="debug", message="debug noise"),
        _event(1, severity="info", message="login ok"),
        _event(2, severity="warning", message="signal drop", host="tamarack-gw"),
        _event(3, severity="error", message="link down", host="tamarack-gw"),
        _event(4, severity="critical", message="power fail"),
        _event(5, severity=None, message="uisp outage note", collector="uisp"),
        _event(6, severity="info", message="client join", collector="unifi", host="yew-ap"),
    ]
    logstore.ingest_events(conn, [(e, f"k{i}") for i, e in enumerate(events)])
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def test_floor_expansion_at_each_level() -> None:
    assert logstore.severities_at_or_above("debug") == (
        "debug", "info", "warning", "error", "critical")
    assert logstore.severities_at_or_above("warning") == ("warning", "error", "critical")
    assert logstore.severities_at_or_above("critical") == ("critical",)


def test_unknown_floor_raises_value_error() -> None:
    with pytest.raises(ValueError):
        logstore.severities_at_or_above("loud")


def test_min_severity_filters_a_floor(store: sqlite3.Connection) -> None:
    events = logstore.query_events(store, min_severity="warning")
    assert {e.severity for e in events} == {"warning", "error", "critical"}


def test_min_severity_excludes_null_severity_rows(store: sqlite3.Connection) -> None:
    """NULL severity cannot be ranked — any floor excludes it; no floor still serves it."""

    events = logstore.query_events(store, min_severity="debug")
    assert all(e.severity is not None for e in events)
    unfloored = logstore.query_events(store)
    assert any(e.severity is None for e in unfloored)


def test_min_severity_combines_with_other_filters(store: sqlite3.Connection) -> None:
    events = logstore.query_events(store, min_severity="warning", host="tamarack")
    assert {(e.host, e.severity) for e in events} == {
        ("tamarack-gw", "warning"), ("tamarack-gw", "error")}


def test_bare_query_since_is_unchanged(store: sqlite3.Connection) -> None:
    """The 014 watcher's call shape: everything after the watermark, ingest order."""

    rows = logstore.query_since(store, after_id=0, limit=100)
    assert len(rows) == 7
    ids = [i for i, _ in rows]
    assert ids == sorted(ids)


def test_tail_respects_after_id(store: sqlite3.Connection) -> None:
    all_rows = logstore.query_since(store, after_id=0, limit=100)
    midpoint = all_rows[2][0]
    later = logstore.query_since(store, after_id=midpoint, limit=100)
    assert [i for i, _ in later] == [i for i, _ in all_rows[3:]]


def test_tail_filters_match_query_events_semantics(store: sqlite3.Connection) -> None:
    tail = logstore.query_since(store, after_id=0, limit=100, min_severity="warning")
    assert {e.severity for _, e in tail} == {"warning", "error", "critical"}
    by_host = logstore.query_since(store, after_id=0, limit=100, host="tamarack")
    assert {e.host for _, e in by_host} == {"tamarack-gw"}
    by_source = logstore.query_since(store, after_id=0, limit=100, source="unifi")
    assert [e.message for _, e in by_source] == ["client join"]
    by_text = logstore.query_since(store, after_id=0, limit=100, contains="link down")
    assert [e.message for _, e in by_text] == ["link down"]


def test_tail_contains_is_literal_not_fts_syntax(store: sqlite3.Connection) -> None:
    """Operator text is a phrase, never FTS/LogsQL syntax (the 013 rule, on the tail too)."""

    assert logstore.query_since(store, after_id=0, limit=100, contains="link AND (") == []


def test_tail_limit_bounds_the_page(store: sqlite3.Connection) -> None:
    rows = logstore.query_since(store, after_id=0, limit=2)
    assert len(rows) == 2
    ids = [i for i, _ in rows]
    assert ids == sorted(ids)  # oldest new rows first — the client pages forward


# --- 050: the reserved "syslog" source family --------------------------------------------------
#
# `source="syslog"` = device syslog only (rows with no declared controller tag), resolved by
# EXCLUSION of CONTROLLER_COLLECTOR_TAGS. Everything else keeps substring semantics. Exercised on
# BOTH read paths (query_events + query_since share _filter_clauses).


@pytest.fixture()
def family_store(tmp_path: Path) -> sqlite3.Connection:
    conn = logstore.connect(tmp_path / "logs.db")
    events = [
        # device syslog — collector is a syslog-ng HOSTNAME (open-ended), across several hosts
        _event(0, message="core syslog a", collector="core-vms-1", host="core-gw-1"),
        _event(1, message="core syslog b", collector="core-vms-1", host="chestnut-gw"),
        _event(2, message="chestnut syslog", collector="chestnut-vms-1", host="chestnut-gw"),
        # controllers — literal declared tags
        _event(3, message="unifi client join", collector="unifi", host="yew-ap"),
        _event(4, message="uisp outage", collector="uisp", host="sf-aspen"),
        # a syslog host whose NAME merely contains a family word — must NOT be misclassified
        _event(5, message="annex syslog", collector="unifi-annex", host="annex-gw"),
        # a collector host that never appears in code — must be picked up by syslog (SC-004)
        _event(6, message="playa syslog", collector="playa-vms-1", host="playa-gw"),
    ]
    logstore.ingest_events(conn, [(e, f"fk{i}") for i, e in enumerate(events)])
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _by_source(conn: sqlite3.Connection, source: str) -> set[str]:
    """Messages matching ``source`` via BOTH read paths, asserting they agree (shared builder)."""

    events = [e.message for e in logstore.query_events(conn, source=source, limit=100)]
    tail = [e.message for _, e in logstore.query_since(conn, after_id=0, limit=100, source=source)]
    assert sorted(events) == sorted(tail), f"read-path drift for source={source!r}"
    return set(events)


def test_syslog_family_selects_only_device_syslog(family_store: sqlite3.Connection) -> None:
    msgs = _by_source(family_store, "syslog")
    assert msgs == {
        "core syslog a", "core syslog b", "chestnut syslog", "annex syslog", "playa syslog"}
    assert "unifi client join" not in msgs and "uisp outage" not in msgs


def test_controller_substring_sources_unchanged(family_store: sqlite3.Connection) -> None:
    assert _by_source(family_store, "uisp") == {"uisp outage"}
    # "unifi" is a plain SUBSTRING (unchanged): it matches "unifi" AND "unifi-annex" collectors.
    assert _by_source(family_store, "unifi") == {"unifi client join", "annex syslog"}


def test_collector_hostname_substring_still_works(family_store: sqlite3.Connection) -> None:
    assert _by_source(family_store, "core-vms-1") == {"core syslog a", "core syslog b"}
    assert _by_source(family_store, "vms") == {
        "core syslog a", "core syslog b", "chestnut syslog", "playa syslog"}


def test_family_is_exact_token_not_substring(family_store: sqlite3.Connection) -> None:
    """A host named ``unifi-annex`` is a syslog-family row (not a declared controller tag): syslog
    INCLUDES it, while the unchanged ``unifi`` substring still over-matches it (FR-005/FR-003)."""

    assert "annex syslog" in _by_source(family_store, "syslog")
    assert "annex syslog" in _by_source(family_store, "unifi")  # substring unchanged


def test_future_collector_included_by_syslog(family_store: sqlite3.Connection) -> None:
    # playa-vms-1 is named nowhere in code; syslog picks it up by exclusion (SC-004).
    assert "playa syslog" in _by_source(family_store, "syslog")


def test_syslog_family_is_case_insensitive(family_store: sqlite3.Connection) -> None:
    canonical = _by_source(family_store, "syslog")
    assert _by_source(family_store, "SYSLOG") == canonical
    assert _by_source(family_store, "Syslog") == canonical
