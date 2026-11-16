"""Core delivery wiring: run_cycle hands the qualifying findings to the injected notifier (015)."""

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


def _finding(device, severity="warning", summary="problem"):  # type: ignore[no-untyped-def]
    return {
        "device": device,
        "severity": severity,
        "confidence": "high",
        "summary": summary,
        "description": f"{device} {summary}",
        "evidence": [f"{device} evidence"],
    }


def _seed(cfg, message):  # type: ignore[no-untyped-def]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, [("core-1", NOW, "interface,info", message)])


def _report(findings):  # type: ignore[no-untyped-def]
    return StubLLMClient([tool_response("report_findings", {"findings": findings})])


def _run(cfg, store, llm, notifier=None, dry_run=False):  # type: ignore[no-untyped-def]
    return run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=llm, notifier=notifier, dry_run=dry_run
    )


def test_single_finding_delivered_once(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    _seed(cfg, "down")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    run = _run(cfg, store, _report([_finding("core-1")]), notifier=notifier)

    assert len(notifier.batches) == 1  # one deliver() call this cycle
    assert len(notifier.batches[0]) == 1
    assert run.alerts_delivered == 1
    assert run.alerts_failed == 0


def test_multiple_findings_delivered_as_one_batch(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    _seed(cfg, "trouble")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()
    findings = [_finding("core-1", summary="a"), _finding("dist-2", summary="b")]

    run = _run(cfg, store, _report(findings), notifier=notifier)

    assert len(notifier.batches) == 1  # the cycle delivers the whole qualifying set at once
    assert len(notifier.batches[0]) == 2
    assert run.alerts_delivered == 2


def test_disabled_delivers_nothing_but_still_writes(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    _seed(cfg, "down")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = _run(cfg, store, _report([_finding("core-1")]), notifier=None)

    assert run.alerts_delivered == 0 and run.alerts_failed == 0
    assert store.list_findings()  # report still written to disk (FR-008)


def test_dry_run_delivers_nothing(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    _seed(cfg, "down")
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    run = _run(cfg, store, _report([_finding("core-1")]), notifier=notifier, dry_run=True)

    assert notifier.batches == []  # FR-014
    assert run.alerts_delivered == 0
