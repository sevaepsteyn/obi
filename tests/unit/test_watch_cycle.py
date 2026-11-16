"""US2 — cycle bookkeeping: no-op, cursor advance, dry-run (feature 014, T011)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import WatchCursor, run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, seed_logs, tool_response, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def test_empty_window_is_no_op(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [("core-1", NOW - timedelta(minutes=2), "system,info", "ok")],
    )
    store = WatchStore(cfg.watch.watch_dir)

    first = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=None)
    assert first.new_event_count == 0 and first.outcome == "no_op"
    assert store.load_cursor() is not None  # baselined forward to the latest event

    second = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(minutes=5), llm_client=None
    )
    assert second.new_event_count == 0 and second.outcome == "no_op"


def test_cursor_advances_on_success(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [
            ("core-1", NOW - timedelta(minutes=3), "system,info", "a"),
            ("core-1", NOW - timedelta(minutes=2), "system,info", "b"),
        ],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)  # cursor (0, 0) ⇒ both events are new

    stub = StubLLMClient([tool_response("report_findings", {"findings": []})])
    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=stub)

    assert run.new_event_count == 2
    assert run.outcome == "no_op"  # no findings, no conditions
    assert run.cursor_after is not None and run.cursor_after.id > 0
    assert store.load_cursor() == run.cursor_after  # persisted


def test_dry_run_writes_and_advances_nothing(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [("core-1", NOW - timedelta(minutes=2), "interface,info", "link ether1 down")],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    finding = {
        "device": "core-1",
        "severity": "info",
        "confidence": "low",
        "summary": "s",
        "description": "d",
        "evidence": ["link ether1 down"],
    }
    stub = StubLLMClient([tool_response("report_findings", {"findings": [finding]})])
    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=stub, dry_run=True)

    assert run.outcome == "findings"
    assert store.list_findings() == []  # nothing written
    assert store.load_cursor() == WatchCursor(id=0)  # not advanced


def test_135_cycle_builds_one_maintenance_set(tmp_path: Path, monkeypatch) -> None:
    from obi.core import watch as watchmod

    calls: list[object] = []
    real = watchmod._maintenance_for

    def counting(config):  # type: ignore[no-untyped-def]
        calls.append(config)
        return real(config)

    monkeypatch.setattr(watchmod, "_maintenance_for", counting)
    cfg = watch_config(tmp_path)
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [("core-1", NOW - timedelta(minutes=2), "system,info", "ok")],
    )
    run_cycle(cfg, WatchStore(cfg.watch.watch_dir), audit=_audit(cfg), now=NOW, llm_client=None)
    assert len(calls) == 1  # built once, threaded into the window filter and every pass
