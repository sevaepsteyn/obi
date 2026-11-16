"""A Slack delivery failure never breaks the read-only cycle (feature 015, FR-007)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from obi.core.audit import AuditTrail
from obi.core.watch import DeliveryResult, run_cycle
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
    "description": "d",
    "evidence": ["e"],
}


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _setup(tmp_path):  # type: ignore[no-untyped-def]
    cfg = watch_config(tmp_path)
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, [("core-1", NOW, "interface,info", "down")])
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    llm = StubLLMClient([tool_response("report_findings", {"findings": [_FINDING]})])
    return cfg, store, llm


@pytest.mark.parametrize(
    "notifier",
    [
        FakeNotifier(result=DeliveryResult(delivered=0, failed=1, error="rate_limited")),
        FakeNotifier(raises=True),
    ],
)
def test_delivery_failure_does_not_break_the_cycle(tmp_path: Path, notifier: FakeNotifier) -> None:
    cfg, store, llm = _setup(tmp_path)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=llm, notifier=notifier)

    assert run.outcome == "degraded"  # surfaced as degraded (F1), not a crash
    assert run.alerts_failed == 1
    assert run.alerts_delivered == 0
    assert any("slack delivery failed" in c for c in run.conditions)
    assert len(store.list_findings()) == 1  # the report is still written to disk
