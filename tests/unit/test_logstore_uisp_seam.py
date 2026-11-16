"""T006: UISP per-source watermark seam in the log store (feature 019)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.models import LogEvent


def _event(msg: str) -> LogEvent:
    return LogEvent(
        ts=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        host="Alder",
        topics="uisp,log,info",
        message=msg,
        subsystem="uisp",
        severity="info",
        raw_line="{}",
        collectors=["uisp"],
    )


def test_watermark_round_trips_per_source_and_survives_reconnect(tmp_path: Path) -> None:
    db = tmp_path / "logs.db"
    conn = logstore.connect(db)
    try:
        assert logstore.get_uisp_watermark(conn, "logs") is None
        logstore.set_uisp_watermark(conn, "logs", 1_000, "log-9")
        logstore.set_uisp_watermark(conn, "outages", 2_000, "out-3")
        conn.commit()
    finally:
        conn.close()

    conn = logstore.connect(db)
    try:
        assert logstore.get_uisp_watermark(conn, "logs") == (1_000, "log-9")
        assert logstore.get_uisp_watermark(conn, "outages") == (2_000, "out-3")
        rows = logstore.list_uisp_watermarks(conn)
    finally:
        conn.close()
    assert {r[0] for r in rows} == {"logs", "outages"}


def test_list_watermarks_empty_on_store_without_table(tmp_path: Path) -> None:
    """A pre-019 read-only store has no uisp_watermark table — list must degrade, not raise."""

    db = tmp_path / "bare.db"
    # Create a logs-only DB the way feature 013 would, without the 019 table.
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    conn = logstore.connect_readonly(db)
    try:
        assert logstore.list_uisp_watermarks(conn) == []
    finally:
        conn.close()


def test_ingest_events_dedups_on_uisp_key(tmp_path: Path) -> None:
    db = tmp_path / "logs.db"
    conn = logstore.connect(db)
    try:
        items = [(_event("device down"), "uisp:outage:out-1:start")]
        first = logstore.ingest_events(conn, items)
        second = logstore.ingest_events(conn, items)  # same key again
        conn.commit()
    finally:
        conn.close()
    assert first.inserted == 1
    assert second.inserted == 0 and second.deduped == 1
