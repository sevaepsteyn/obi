"""T015: an ingested UISP outage onset reaches the watcher's judge and yields a finding (019)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.models import LogEvent
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, tool_response, watch_config, watch_inventory

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _seed_uisp_outage(db_path: Path, *, host: str, message: str) -> None:
    event = LogEvent(
        ts=NOW,
        host=host,
        topics="uisp,outage,outage",
        message=message,
        subsystem="uisp",
        severity="warning",  # outage onset crosses the watcher's default floor
        raw_line="{}",
        collectors=["uisp"],
    )
    conn = logstore.connect(db_path)
    try:
        logstore.ingest_events(conn, [(event, "uisp:outage:out-1:start")])
        conn.commit()
    finally:
        conn.close()


def test_uisp_outage_reaches_judge_and_yields_finding(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])  # only RouterOS host is core-1
    _seed_uisp_outage(cfg.logs.db_path, host="Alder", message="ptp-ridge-east outage started")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    finding = {
        "device": "Alder",
        "severity": "warning",
        "confidence": "high",
        "summary": "PtP link ptp-ridge-east went down",
        "description": "A UISP-managed point-to-point link started an outage.",
        "evidence": ["ptp-ridge-east outage started"],
    }
    stub = StubLLMClient([tool_response("report_findings", {"findings": [finding]})])
    run = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, llm_client=stub)

    # The UISP row was NOT filtered out by routeros_only — it reached the model (feature-014 path).
    assert run.new_event_count == 1
    assert run.outcome == "findings"
    window_text = stub.calls[0]["messages"][0]["content"]
    assert "ptp-ridge-east outage started" in window_text
    findings = store.list_findings()
    assert len(findings) == 1
    assert findings[0].device == "Alder"
