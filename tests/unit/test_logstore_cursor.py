"""The watcher cursor queries on the log store (feature 014, T004)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from tests.fakes import seed_logs

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _rows() -> list[tuple[str, datetime, str, str]]:
    return [
        ("core-1", NOW.replace(second=1), "system,info", "first"),
        ("core-1", NOW.replace(second=2), "system,info", "second"),
        ("chestnut-1", NOW.replace(second=2), "dhcp,info", "same-ts-other-host"),
    ]


def test_latest_cursor_empty_store(tmp_path: Path) -> None:
    db = tmp_path / "logs.db"
    logstore.connect(db).close()  # create the empty schema
    conn = logstore.connect_readonly(db)
    try:
        assert logstore.latest_cursor(conn) is None
    finally:
        conn.close()


def test_query_since_orders_by_ingest_id_and_bounds(tmp_path: Path) -> None:
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db", _rows())
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    try:
        latest = logstore.latest_cursor(conn)
        assert latest is not None and latest == 3  # three rows ingested, high-water id = 3

        rows = logstore.query_since(conn, after_id=0, limit=10)
        assert len(rows) == 3
        ids = [row_id for row_id, _ in rows]
        assert ids == sorted(ids)  # ascending by ingest id (clock-independent)

        assert len(logstore.query_since(conn, after_id=0, limit=2)) == 2

        # Nothing is newer than the latest id.
        assert logstore.query_since(conn, after_id=latest, limit=10) == []
    finally:
        conn.close()


def test_future_dated_event_does_not_poison_the_watermark(tmp_path: Path) -> None:
    # A device with a far-future clock must not push the id watermark past real events.
    rows = [
        ("core-1", NOW, "system,info", "real event one"),
        ("skewed-ptp", datetime(2026, 7, 1, tzinfo=UTC), "system,info", "future-clock event"),
        ("core-1", NOW.replace(second=30), "system,info", "real event two"),
    ]
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db", rows)
    conn = logstore.connect_readonly(tmp_path / "logs.db")
    try:
        # The watermark is the max id (the last real event ingested), not the future timestamp.
        latest = logstore.latest_cursor(conn)
        assert latest == 3
        # After the first real event, the future event AND the later real event are both "new".
        assert len(logstore.query_since(conn, after_id=1, limit=10)) == 2
    finally:
        conn.close()
