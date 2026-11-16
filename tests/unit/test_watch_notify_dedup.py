"""Delivery rides the watcher's post-dedup set: alert once per TTL, never every cycle (015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import (
    FakeNotifier,
    StubLLMClient,
    prime_cursor,
    seed_logs,
    tool_response,
    watch_config,
)

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
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, [("core-1", NOW, "interface,info", message)])


def test_persistent_finding_delivered_once_then_silent_then_realerts(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # dedup on, ttl 6h
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    _seed(cfg, "down #1")
    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_report(), notifier=notifier)
    assert len(notifier.batches) == 1  # delivered on first occurrence

    _seed(cfg, "down #2")  # same problem recurs within the TTL
    run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(minutes=5),
        llm_client=_report(), notifier=notifier,
    )
    assert len(notifier.batches) == 1  # suppressed: it never reached the notifier again

    _seed(cfg, "down #3")  # past the re-alert TTL
    run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(hours=7),
        llm_client=_report(), notifier=notifier,
    )
    assert len(notifier.batches) == 2  # re-alerted after the TTL lapsed
