"""T006: logstore seam for UniFi event ingest (feature 016) — explicit dedup key + watermark."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.models import LogEvent


def _ev(ts_ms: int, host: str, msg: str) -> LogEvent:
    return LogEvent(
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        host=host,
        topics="unifi_devices,system_wifi,device_stop_mesh",
        message=msg,
        subsystem="unifi_devices",
        severity="info",
        raw_line="{}",
        collectors=["unifi"],
    )


def _count(conn: object) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])  # type: ignore[attr-defined]


def test_ingest_events_dedups_on_explicit_key(tmp_path: Path) -> None:
    conn = logstore.connect(tmp_path / "logs.db")
    items = [
        (_ev(1000, "Alder", "a"), "unifi:Alder:evt1"),
        (_ev(2000, "Alder", "b"), "unifi:Alder:evt2"),
    ]
    stats = logstore.ingest_events(conn, items)
    conn.commit()
    assert stats.inserted == 2
    assert _count(conn) == 2

    # Re-ingest the same events (overlap) → zero new rows, idempotent on the explicit key.
    stats2 = logstore.ingest_events(conn, items)
    conn.commit()
    assert stats2.inserted == 0
    assert stats2.deduped == 2
    assert _count(conn) == 2


def test_explicit_key_keeps_distinct_same_second_events(tmp_path: Path) -> None:
    # Two events sharing ts + host + message but with different ids must NOT collapse (the default
    # sha(ts|host|message) would have merged them; the explicit event id keeps them distinct).
    conn = logstore.connect(tmp_path / "logs.db")
    items = [
        (_ev(1000, "Alder", "same"), "unifi:Alder:evtA"),
        (_ev(1000, "Alder", "same"), "unifi:Alder:evtB"),
    ]
    stats = logstore.ingest_events(conn, items)
    conn.commit()
    assert stats.inserted == 2
    assert _count(conn) == 2


def test_default_insert_path_unchanged(tmp_path: Path) -> None:
    # Regression: with no explicit key, dedup falls back to sha(ts|host|message) as in feature 013.
    conn = logstore.connect(tmp_path / "logs.db")
    items = [(_ev(1000, "core", "x"), None), (_ev(1000, "core", "x"), None)]
    stats = logstore.ingest_events(conn, items)
    conn.commit()
    assert stats.inserted == 1
    assert stats.deduped == 1
    assert _count(conn) == 1


def test_query_events_source_filter(tmp_path: Path) -> None:
    # --source filters on the collectors tag, disambiguating a UniFi site from a like-named host.
    db = tmp_path / "logs.db"
    conn = logstore.connect(db)
    unifi_row = _ev(2000, "Alder", "mesh stopped")  # collectors=["unifi"]
    routeros_row = LogEvent(
        ts=datetime.fromtimestamp(1, tz=UTC),
        host="chestnut-gw",
        topics="system,info",
        message="boot",
        collectors=["core"],
    )
    logstore.ingest_events(conn, [(unifi_row, "unifi:Alder:e1"), (routeros_row, "ros:1")])
    conn.commit()
    conn.close()

    ro = logstore.connect_readonly(db)
    try:
        assert [e.host for e in logstore.query_events(ro, source="unifi", limit=50)] == ["Alder"]
        assert [e.host for e in logstore.query_events(ro, source="core", limit=50)] == [
            "chestnut-gw"
        ]
        assert len(logstore.query_events(ro, limit=50)) == 2  # no filter → both
    finally:
        ro.close()


def test_list_watermarks_tolerates_missing_table(tmp_path: Path) -> None:
    # A pre-016 store (has a logs table, never created unifi_watermark) must not crash `status`.
    db = tmp_path / "old.db"
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()
    conn = logstore.connect_readonly(db)
    try:
        assert logstore.list_unifi_watermarks(conn) == []
    finally:
        conn.close()


def test_watermark_roundtrips_and_survives_reconnect(tmp_path: Path) -> None:
    db = tmp_path / "logs.db"
    conn = logstore.connect(db)
    assert logstore.get_unifi_watermark(conn, "Alder") is None

    logstore.set_unifi_watermark(conn, "Alder", 1782444302721, "evt-xyz")
    conn.commit()
    conn.close()

    conn2 = logstore.connect(db)
    assert logstore.get_unifi_watermark(conn2, "Alder") == (1782444302721, "evt-xyz")

    logstore.set_unifi_watermark(conn2, "Alder", 1782444400000, "evt-abc")
    conn2.commit()
    assert logstore.get_unifi_watermark(conn2, "Alder") == (1782444400000, "evt-abc")
    # A second site is independent.
    assert logstore.get_unifi_watermark(conn2, "Chestnut") is None
