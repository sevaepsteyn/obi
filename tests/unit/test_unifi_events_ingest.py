"""T014: engine.unifi_ingest (feature 016) — first-run backfill, watermark, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import Config, LogsSettings
from obi.core.engine import unifi_ingest
from obi.core.models import SourceOutcome
from tests.unifi_fakes import FakeUnifiInternalClient, unifi_internal_settings

# A `now` just after the newest sample event so both fall inside the first-run backfill window.
_NOW = datetime.fromtimestamp(1_782_444_500, tz=UTC)
_LATEST_TS_MS = 1_782_444_400_000  # evt-high-1
_SEVEN_DAYS_MS = 7 * 86400 * 1000


def _config(tmp_path: Path) -> tuple[Config, AuditTrail]:
    settings = unifi_internal_settings(sites=["Alder"])
    config = Config(
        unifi_internal=settings,
        logs=LogsSettings(db_path=tmp_path / "logs.db", log_tz="UTC"),
        audit_path=tmp_path / "audit.jsonl",
    )
    return config, AuditTrail(config.audit_path)


def test_first_run_backfills_and_stores(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    client = FakeUnifiInternalClient(config.unifi_internal)  # type: ignore[arg-type]

    report = unifi_ingest(config, audit=audit, now=_NOW, client=client)

    assert report.outcome is SourceOutcome.REACHABLE
    assert report.inserted == 2
    # First run requested a backfill window of `now - 7d`.
    _internal, frm_ms, to_ms = client.windows[0]
    assert to_ms == int(_NOW.timestamp() * 1000)
    assert frm_ms == to_ms - _SEVEN_DAYS_MS

    conn = logstore.connect_readonly(config.logs.db_path)  # type: ignore[union-attr]
    try:
        rows = logstore.query_events(conn, host="Alder", limit=50)
        wm = logstore.get_unifi_watermark(conn, "Alder")
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0].ts >= rows[1].ts  # newest-first
    assert wm == (_LATEST_TS_MS, "evt-high-1")


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    settings = config.unifi_internal

    first = unifi_ingest(config, audit=audit, now=_NOW, client=FakeUnifiInternalClient(settings))  # type: ignore[arg-type]
    assert first.inserted == 2

    second = unifi_ingest(config, audit=audit, now=_NOW, client=FakeUnifiInternalClient(settings))  # type: ignore[arg-type]
    assert second.inserted == 0  # nothing new — overlap events dedup on their stable id

    conn = logstore.connect_readonly(config.logs.db_path)  # type: ignore[union-attr]
    try:
        rows = logstore.query_events(conn, host="Alder", limit=50)
    finally:
        conn.close()
    assert len(rows) == 2  # still two rows, no duplicates


def test_no_internal_config_is_a_clean_noop(tmp_path: Path) -> None:
    config = Config(
        logs=LogsSettings(db_path=tmp_path / "logs.db"), audit_path=tmp_path / "a.jsonl"
    )
    report = unifi_ingest(config, audit=AuditTrail(config.audit_path), now=_NOW)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.sites == []
