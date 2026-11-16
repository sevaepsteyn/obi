"""T016: UISP log ingest is secret-safe and failure-isolated (feature 019)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import Config, LogsSettings, Secret
from obi.core.drivers.uisp import UispController, UispError
from obi.core.engine import uisp_ingest
from obi.core.models import SourceOutcome
from tests.fakes import uisp_log_transport, uisp_settings

_NOW = datetime(2026, 6, 21, 9, 0, tzinfo=UTC)
_TOKEN = "UISP-TOKEN-supersecret"


def _config(tmp_path: Path) -> tuple[Config, AuditTrail]:
    settings = uisp_settings(token=Secret(_TOKEN), ingest_events=True, ingest_outages=True)
    config = Config(
        uisp=settings,
        logs=LogsSettings(db_path=tmp_path / "logs.db", log_tz="UTC"),
        audit_path=tmp_path / "audit.jsonl",
    )
    return config, AuditTrail(config.audit_path)


def test_token_absent_from_audit_after_a_cycle(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    client = UispController(config.uisp, log_transport=uisp_log_transport())  # type: ignore[arg-type]
    report = uisp_ingest(config, audit=audit, now=_NOW, controller=client)

    assert report.outcome is SourceOutcome.REACHABLE
    audit_text = config.audit_path.read_text()
    assert "uisp_logs_ingest" in audit_text  # the cycle was audited (Principle V)
    assert _TOKEN not in audit_text
    assert _TOKEN not in repr(report)


def test_unreachable_controller_is_non_fatal_and_advances_nothing(tmp_path: Path) -> None:
    config, audit = _config(tmp_path)
    boom = UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)")
    transport = uisp_log_transport(logs_error=boom, outages_error=boom)
    client = UispController(config.uisp, log_transport=transport)  # type: ignore[arg-type]

    report = uisp_ingest(config, audit=audit, now=_NOW, controller=client)

    # Records the condition, never raises, stores nothing, advances no watermark.
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.inserted == 0
    conn = logstore.connect_readonly(config.logs.db_path)  # type: ignore[union-attr]
    try:
        assert logstore.query_events(conn, source="uisp", limit=10) == []
        assert logstore.get_uisp_watermark(conn, "logs") is None
        assert logstore.get_uisp_watermark(conn, "outages") is None
    finally:
        conn.close()
    assert _TOKEN not in config.audit_path.read_text()

    # A later healthy cycle resumes and ingests from scratch.
    healthy = UispController(config.uisp, log_transport=uisp_log_transport())  # type: ignore[arg-type]
    report2 = uisp_ingest(config, audit=audit, now=_NOW, controller=healthy)
    assert report2.outcome is SourceOutcome.REACHABLE
    assert report2.inserted == 7
