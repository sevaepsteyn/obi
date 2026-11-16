"""Integration (feature 042, T010): a watch cycle audits its judge, findings, and errors.

Runs the real run_cycle with a stub judge inside a recorder-backed capture: the judge's model
calls land as llm_call events, each written finding as a `report` event, and the legacy
watch_run intent record as an `action` event — all under one `watch-…` session. An
unavailable model records an `llm_call` error event while the cycle degrades as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core import auditstore, logstore
from obi.core.audit import AuditRecorder, AuditTrail
from obi.core.config import AuditSettings
from obi.core.llm import ChatUnavailable
from obi.core.models import LogEvent
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, tool_response, watch_config, watch_inventory

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def _seed_event(db_path: Path) -> None:
    event = LogEvent(
        ts=NOW,
        host="core-1",
        topics="system,error",
        message="interface ether1 flapping",
        subsystem="system",
        severity="error",
        raw_line="raw",
        collectors=["core"],
    )
    conn = logstore.connect(db_path)
    try:
        logstore.ingest_events(conn, [(event, "dedup-1")])
        conn.commit()
    finally:
        conn.close()


def _events(db: Path) -> list[dict[str, Any]]:
    conn = auditstore.connect_readonly(db)
    try:
        rows, _ = auditstore.query_events(conn, limit=100, include_payload=True)
        return list(reversed(rows))
    finally:
        conn.close()


def test_watch_cycle_audits_judge_and_reports(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])
    _seed_event(cfg.logs.db_path)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    finding = {
        "device": "core-1",
        "severity": "warning",
        "confidence": "high",
        "summary": "ether1 flapping",
        "description": "Repeated link flaps on ether1.",
        "evidence": ["interface ether1 flapping"],
    }
    stub = StubLLMClient([tool_response("report_findings", {"findings": [finding]})])
    db = tmp_path / "audit.db"
    recorder = AuditRecorder(AuditSettings(db_path=db))

    run = run_cycle(
        cfg,
        store,
        audit=AuditTrail(cfg.audit_path),
        now=NOW,
        llm_client=stub,
        recorder=recorder,
    )
    recorder.close()
    assert run.outcome == "findings"

    events = _events(db)
    assert events, "the cycle recorded nothing"
    sessions = {e["session_id"] for e in events}
    assert len(sessions) == 1
    (session,) = sessions
    assert session is not None and session.startswith("watch-")
    assert all(e["source"] == "watch" for e in events)

    llm_events = [e for e in events if e["kind"] == "llm_call"]
    assert len(llm_events) == 1
    assert "interface ether1 flapping" in llm_events[0]["payload"]["messages"][0]["content"]

    report_events = [e for e in events if e["kind"] == "report"]
    assert len(report_events) == 1
    assert report_events[0]["payload"]["channel"] == "local"
    assert report_events[0]["payload"]["title"] == "ether1 flapping"
    assert report_events[0]["target"] == "core-1"

    action_events = [e for e in events if e["kind"] == "action"]
    assert any(e["payload"]["action"] == "watch_run" for e in action_events)


def test_watch_cycle_llm_error_recorded(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])
    _seed_event(cfg.logs.db_path)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    class DownClient:
        def create_message(self, **kwargs: Any) -> Any:
            raise ChatUnavailable("model unreachable")

    db = tmp_path / "audit.db"
    recorder = AuditRecorder(AuditSettings(db_path=db))
    run = run_cycle(
        cfg,
        store,
        audit=AuditTrail(cfg.audit_path),
        now=NOW,
        llm_client=DownClient(),
        recorder=recorder,
    )
    recorder.close()
    assert run.outcome == "degraded"  # unchanged degraded-mode behavior

    llm_events = [e for e in _events(db) if e["kind"] == "llm_call"]
    assert len(llm_events) == 1
    assert llm_events[0]["outcome"] == "error"
    assert "model unreachable" in llm_events[0]["error"]
