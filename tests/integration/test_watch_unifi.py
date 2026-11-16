"""T018: an ingested UniFi event reaches the watcher's judge and yields a finding (feature 016)."""

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


def _seed_unifi_event(db_path: Path, *, host: str, severity: str, message: str) -> None:
    event = LogEvent(
        ts=NOW,
        host=host,
        topics="unifi_devices,system,device_lost_contact",
        message=message,
        subsystem="unifi_devices",
        severity=severity,
        raw_line="{}",
        collectors=["unifi"],
    )
    conn = logstore.connect(db_path)
    try:
        logstore.ingest_events(conn, [(event, f"unifi:{host}:evt1")])
        conn.commit()
    finally:
        conn.close()


def test_unifi_event_reaches_judge_and_yields_finding(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])  # only RouterOS host is core-1
    _seed_unifi_event(
        cfg.logs.db_path,
        host="Alder",
        severity="warning",
        message="TP: Trailer 14 lost contact with the controller.",
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    finding = {
        "device": "Alder",
        "severity": "warning",
        "confidence": "medium",
        "summary": "AP lost contact with the controller",
        "description": "A UniFi AP stopped reporting to the controller.",
        "evidence": ["lost contact with the controller"],
    }
    stub = StubLLMClient([tool_response("report_findings", {"findings": [finding]})])
    run = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, llm_client=stub)

    # The UniFi row was NOT filtered out by routeros_only — it reached the model and produced a
    # finding via the unchanged feature-014 path.
    assert run.new_event_count == 1
    assert run.outcome == "findings"
    window_text = stub.calls[0]["messages"][0]["content"]
    assert "lost contact" in window_text
    findings = store.list_findings()
    assert len(findings) == 1
    assert findings[0].device == "Alder"
