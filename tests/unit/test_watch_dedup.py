"""Alert-once dedup across cycles for LLM findings (feature 014, noise fix)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, seed_logs, tool_response, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)

_FINDING = {
    "device": "core-1",
    "severity": "warning",
    "confidence": "high",
    "summary": "ether1 flapping",
    "description": "ether1 keeps bouncing",
    "evidence": ["link ether1 down"],
}


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _report():  # type: ignore[no-untyped-def]
    return StubLLMClient([tool_response("report_findings", {"findings": [_FINDING]})])


def _seed(cfg, message):  # type: ignore[no-untyped-def]
    # Unique message ⇒ a new ingested row (new id) ⇒ the cycle has new events to judge.
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, [("core-1", NOW, "interface,info", message)])


def test_recurring_finding_alerts_once_then_suppresses(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    _seed(cfg, "down #1")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    first = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_report())
    assert first.finding_ids and len(store.list_findings()) == 1

    _seed(cfg, "down #2")  # fresh events; the model reports the same problem again
    second = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(minutes=5), llm_client=_report()
    )
    assert second.finding_ids == []  # suppressed (same device+severity already open)
    assert second.outcome == "no_op"
    assert len(store.list_findings()) == 1  # no duplicate report written


def test_finding_re_alerts_after_ttl(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # dedup_ttl_hours defaults to 6
    _seed(cfg, "down #1")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_report())

    _seed(cfg, "down #2")
    later = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(hours=7), llm_client=_report()
    )
    assert later.finding_ids  # past the TTL → re-alert
    assert len(store.list_findings()) == 2


def test_dedup_disabled_reports_every_cycle(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    cfg.watch.dedup = False
    _seed(cfg, "down #1")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_report())

    _seed(cfg, "down #2")
    second = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(minutes=5), llm_client=_report()
    )
    assert second.finding_ids  # reported again — dedup off
    assert len(store.list_findings()) == 2
