"""Severity-floor gating: below-floor findings are written but not delivered (feature 015)."""

from __future__ import annotations

from datetime import UTC, datetime
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


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _finding(severity):  # type: ignore[no-untyped-def]
    return {
        "device": "core-1",
        "severity": severity,
        "confidence": "high",
        "summary": f"{severity} thing",
        "description": "d",
        "evidence": ["e"],
    }


def _cycle(cfg, severity):  # type: ignore[no-untyped-def]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, [("core-1", NOW, "interface,info", severity)])
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()
    llm = StubLLMClient([tool_response("report_findings", {"findings": [_finding(severity)]})])
    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=llm, notifier=notifier)
    return run, store, notifier


def test_below_floor_written_but_not_delivered(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # default floor = warning
    run, store, notifier = _cycle(cfg, "info")

    assert notifier.batches == []  # info is below the warning floor (FR-004)
    assert run.alerts_delivered == 0
    assert len(store.list_findings()) == 1  # but still recorded to disk


def test_default_floor_delivers_warning(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # default floor = warning
    run, _store, notifier = _cycle(cfg, "warning")

    assert len(notifier.batches) == 1
    assert run.alerts_delivered == 1


def test_critical_floor_suppresses_warning(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, slack_min_severity="critical")
    run, store, notifier = _cycle(cfg, "warning")

    assert notifier.batches == []  # warning < critical floor
    assert len(store.list_findings()) == 1


def test_info_floor_delivers_info(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, slack_min_severity="info")
    run, _store, notifier = _cycle(cfg, "info")

    assert len(notifier.batches) == 1
    assert run.alerts_delivered == 1
