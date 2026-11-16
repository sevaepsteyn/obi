"""Unit (feature 013): incremental, idempotent, deduplicating ingest of the mirrored files.

Asserts the schema applies, the per-host daily files are parsed (and ``messages.log`` skipped),
the same line seen by two collectors becomes one row recording both, a re-run is a no-op, and a
file that grows is read only from its last offset.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from obi.core import logstore

_LINE = (
    "Jun  3 10:53:51.207065 chestnut-gw dhcp,info dhcp-brst-bmorg assigned 100.90.13.55 "
    "for AA:99:1F:66:4E:DD Pixel-8-Pro\n"
)
_ERR = "Jun  3 11:00:02.000000 chestnut-gw system,error login failure for admin\n"
_BAD = "not a syslog line\n"


def _write(mirror: Path, collector: str, host: str, text: str) -> Path:
    day = mirror / collector / "2026" / "06" / "03"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{host}.log"
    path.write_text(text)
    return path


def _rows(db: Path) -> list[tuple[str, str, str]]:
    conn = logstore.connect_readonly(db)
    try:
        return [
            (r["host"], r["topics"], r["collectors"])
            for r in conn.execute("SELECT host, topics, collectors FROM logs ORDER BY ts_us")
        ]
    finally:
        conn.close()


def test_ingest_counts_and_skips_malformed(tmp_path: Path) -> None:
    mirror = tmp_path / "logs"
    _write(mirror, "core-vms-1", "chestnut-gw", _LINE + _ERR + _BAD)
    # messages.log at the collector root must be ignored, not parsed.
    (mirror / "core-vms-1").mkdir(parents=True, exist_ok=True)
    (mirror / "core-vms-1" / "messages.log").write_text(_LINE)

    conn = logstore.connect(tmp_path / "logs.db")
    stats = logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()

    assert stats.files_scanned == 1  # messages.log excluded
    assert stats.lines_parsed == 3
    assert stats.inserted == 2
    assert stats.skipped_malformed == 1


def test_dedup_across_collectors(tmp_path: Path) -> None:
    mirror = tmp_path / "logs"
    # The SAME logical line lands on both core and chestnut collectors.
    _write(mirror, "core-vms-1", "chestnut-gw", _LINE)
    _write(mirror, "chestnut-vms-1", "chestnut-gw", _LINE)

    conn = logstore.connect(tmp_path / "logs.db")
    stats = logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()

    assert stats.inserted == 1 and stats.deduped == 1
    rows = _rows(tmp_path / "logs.db")
    assert len(rows) == 1
    # Both collectors are recorded on the single deduped row.
    assert set(rows[0][2].split(",")) == {"core-vms-1", "chestnut-vms-1"}


def test_reingest_is_idempotent(tmp_path: Path) -> None:
    mirror = tmp_path / "logs"
    _write(mirror, "core-vms-1", "chestnut-gw", _LINE + _ERR)
    db = tmp_path / "logs.db"

    conn = logstore.connect(db)
    first = logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()
    assert first.inserted == 2

    conn = logstore.connect(db)
    second = logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()
    # Unchanged file → skipped entirely; no new rows, no re-parse.
    assert second.inserted == 0 and second.lines_parsed == 0 and second.files_ingested == 0
    assert len(_rows(db)) == 2


def test_incremental_append_reads_only_new_bytes(tmp_path: Path) -> None:
    mirror = tmp_path / "logs"
    path = _write(mirror, "core-vms-1", "chestnut-gw", _LINE)
    db = tmp_path / "logs.db"

    conn = logstore.connect(db)
    logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()
    assert len(_rows(db)) == 1

    with path.open("a") as handle:  # the day's file grows (one appended line)
        handle.write(_ERR)

    conn = logstore.connect(db)
    stats = logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()
    assert stats.lines_parsed == 1 and stats.inserted == 1  # only the appended line
    assert len(_rows(db)) == 2
