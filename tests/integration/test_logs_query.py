"""Integration (feature 013): the SqliteLogSource is total and bounded.

Real ingest → query through the engine wrapper (audited once); a missing or corrupt store yields
a clear, non-REACHABLE report and never raises (field-resilient degradation, Principle II/VI).
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import LogsSettings
from obi.core.drivers.sqlite_logs import SqliteLogSource
from obi.core.engine import query_logs
from obi.core.models import SourceOutcome


def _ingested_db(tmp_path: Path, lines: int) -> Path:
    mirror = tmp_path / "logs"
    day = mirror / "core-vms-1" / "2026" / "06" / "03"
    day.mkdir(parents=True)
    body = "".join(
        f"Jun  3 10:{i:02d}:00.000000 chestnut-gw dhcp,info assigned 100.90.13.{i} to host{i}\n"
        for i in range(lines)
    )
    (day / "chestnut-gw.log").write_text(body)
    db = tmp_path / "logs.db"
    conn = logstore.connect(db)
    logstore.ingest_tree(conn, mirror, tz=UTC)
    conn.close()
    return db


def test_query_is_bounded_and_audited(tmp_path: Path) -> None:
    db = _ingested_db(tmp_path, lines=10)
    source = SqliteLogSource(LogsSettings(db_path=db, max_limit=3))
    report = query_logs(
        source, audit=AuditTrail(tmp_path / "audit.jsonl"), db_label=str(db), limit=100,
    )
    assert report.outcome is SourceOutcome.REACHABLE
    assert len(report.events) == 3  # hard max_limit applied even though limit=100 requested
    assert report.truncated is True
    trail = (tmp_path / "audit.jsonl").read_text()
    assert len([ln for ln in trail.splitlines() if '"logs_query"' in ln]) == 1


def test_missing_store_is_unreachable_not_raising(tmp_path: Path) -> None:
    source = SqliteLogSource(LogsSettings(db_path=tmp_path / "absent.db"))
    report = source.query(
        host=None, since=None, until=None, severity=None, contains=None, limit=10, timeout=1,
    )
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.events == []
    assert "ingest" in (report.detail or "")


def test_corrupt_store_is_unreachable_not_raising(tmp_path: Path) -> None:
    db = tmp_path / "logs.db"
    db.write_bytes(b"this is not a sqlite database")
    source = SqliteLogSource(LogsSettings(db_path=db))
    report = source.query(
        host=None, since=None, until=None, severity=None, contains=None, limit=10, timeout=1,
    )
    assert report.outcome is SourceOutcome.UNREACHABLE
    assert report.events == []
