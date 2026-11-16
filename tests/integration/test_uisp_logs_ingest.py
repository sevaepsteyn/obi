"""T013: engine.uisp_ingest (feature 019) — store, query, watermark, idempotency, isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import Config, LogsSettings
from obi.core.drivers.uisp import UispController, UispError
from obi.core.engine import uisp_ingest
from obi.core.models import SourceOutcome
from tests.fakes import load_uisp_outages, uisp_log_transport, uisp_settings

# `now` just after the newest fixture timestamp (outage onset 2026-06-21T08:00Z), so the 30d
# first-run window covers every record.
_NOW = datetime(2026, 6, 21, 9, 0, tzinfo=UTC)


def _config(tmp_path: Path, **uisp_overrides: object) -> tuple[Config, AuditTrail]:
    settings = uisp_settings(ingest_events=True, ingest_outages=True, **uisp_overrides)
    config = Config(
        uisp=settings,
        logs=LogsSettings(db_path=tmp_path / "logs.db", log_tz="UTC"),
        audit_path=tmp_path / "audit.jsonl",
    )
    return config, AuditTrail(config.audit_path)


def _rows(db_path: Path) -> list[object]:
    conn = logstore.connect_readonly(db_path)
    try:
        return logstore.query_events(conn, source="uisp", limit=100)
    finally:
        conn.close()


def test_ingest_stores_events_and_outages_queryable_as_uisp(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    client = UispController(config.uisp, log_transport=uisp_log_transport())  # type: ignore[arg-type]

    report = uisp_ingest(config, audit=audit, now=_NOW, controller=client)

    assert report.outcome is SourceOutcome.REACHABLE
    # logs: 4 events; outages: out-ongoing onset (1) + out-cleared onset+clearance (2) = 3.
    assert report.inserted == 7
    rows = _rows(config.logs.db_path)  # type: ignore[union-attr]
    assert len(rows) == 7
    assert all("uisp" in r.collectors for r in rows)  # tagged UISP-sourced
    # an outage onset crosses the warning floor; its clearance is recorded at info.
    by_msg = {r.message: r.severity for r in rows}
    assert any("started" in m and sev == "warning" for m, sev in by_msg.items())
    assert any("cleared after" in m and sev == "info" for m, sev in by_msg.items())

    conn = logstore.connect_readonly(config.logs.db_path)  # type: ignore[union-attr]
    try:
        assert logstore.get_uisp_watermark(conn, "logs") is not None
        assert logstore.get_uisp_watermark(conn, "logs")[1] == "log-4"  # newest event id
        assert logstore.get_uisp_watermark(conn, "outages") is not None
    finally:
        conn.close()


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    first = uisp_ingest(
        config,
        audit=audit,
        now=_NOW,
        controller=UispController(config.uisp, log_transport=uisp_log_transport()),  # type: ignore[arg-type]
    )
    assert first.inserted == 7

    second = uisp_ingest(
        config,
        audit=audit,
        now=_NOW,
        controller=UispController(config.uisp, log_transport=uisp_log_transport()),  # type: ignore[arg-type]
    )
    assert second.inserted == 0  # overlap events + re-scanned outages all dedup
    assert len(_rows(config.logs.db_path)) == 7  # type: ignore[union-attr]


def test_ongoing_outage_not_duplicated_and_clearance_added_when_it_ends(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    uisp_ingest(
        config,
        audit=audit,
        now=_NOW,
        controller=UispController(config.uisp, log_transport=uisp_log_transport()),  # type: ignore[arg-type]
    )
    assert len(_rows(config.logs.db_path)) == 7  # type: ignore[union-attr]

    # The previously-ongoing outage clears: same id, now with an endTimestamp.
    cleared = load_uisp_outages()
    for item in cleared["items"]:
        if item["id"] == "out-ongoing":
            item["endTimestamp"] = "2026-06-21T08:30:00.000Z"
            item["inProgress"] = False
    later = datetime(2026, 6, 21, 9, 30, tzinfo=UTC)

    report = uisp_ingest(
        config,
        audit=audit,
        now=later,
        controller=UispController(
            config.uisp, log_transport=uisp_log_transport(outages=cleared)  # type: ignore[arg-type]
        ),
    )
    assert report.inserted == 1  # only the new clearance row; the onset dedups
    rows = _rows(config.logs.db_path)  # type: ignore[union-attr]
    assert len(rows) == 8
    assert any("ptp-ridge-east" in r.message and "cleared after 30m" in r.message for r in rows)


def test_one_source_fails_other_proceeds(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    transport = uisp_log_transport(
        logs_error=UispError(SourceOutcome.UNREACHABLE, "controller returned HTTP 503")
    )
    client = UispController(config.uisp, log_transport=transport)  # type: ignore[arg-type]

    report = uisp_ingest(config, audit=audit, now=_NOW, controller=client)

    sources = {s.source: s for s in report.sources}
    assert sources["logs"].outcome is SourceOutcome.UNREACHABLE
    assert sources["outages"].outcome is SourceOutcome.REACHABLE
    assert sources["outages"].inserted == 3  # outages still ingested
    assert report.outcome is SourceOutcome.REACHABLE  # any source ok -> cycle ok


def test_disabled_config_is_a_clean_noop(tmp_path: Path) -> None:
    settings = uisp_settings()  # no ingest flags -> ingestion inactive
    config = Config(
        uisp=settings,
        logs=LogsSettings(db_path=tmp_path / "logs.db"),
        audit_path=tmp_path / "audit.jsonl",
    )
    report = uisp_ingest(config, audit=AuditTrail(config.audit_path), now=_NOW)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.sources == []
