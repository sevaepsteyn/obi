"""US2 — self-monitoring lag/backlog + pipeline-silent alarms (feature 062, A4)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, seed_logs, tool_response, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _empty_llm() -> StubLLMClient:
    return StubLLMClient([tool_response("report_findings", {"findings": []})])


def _window(cfg, size: int):  # type: ignore[no-untyped-def]
    return dataclasses.replace(cfg, watch=dataclasses.replace(cfg.watch, max_window_events=size))


def _watchdog(store: WatchStore):  # type: ignore[no-untyped-def]
    return [f for f in store.list_findings() if f.source == "watchdog"]


def test_backlog_alarm_on_count_threshold(tmp_path: Path) -> None:
    cfg = _window(watch_config(tmp_path, lag_alarm_events=2), 2)  # budget 2, alarm if >2 remain
    rows = [("core-1", NOW - timedelta(minutes=5 - i), "system,error", f"e{i}") for i in range(5)]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert run.remaining_candidates == 3  # 5 candidates, budget 2 → 3 left over
    backlog = [f for f in _watchdog(store) if f.dedup_extra == "backlog"]
    assert len(backlog) == 1 and backlog[0].severity == "warning"


def test_no_alarm_when_thresholds_disabled(tmp_path: Path) -> None:
    cfg = _window(watch_config(tmp_path), 2)  # lag knobs default to 0 (disabled) in the fixture
    rows = [("core-1", NOW - timedelta(minutes=5 - i), "system,error", f"e{i}") for i in range(5)]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert _watchdog(store) == []  # behind, but alarms are off → silence


def test_pipeline_silent_alarm(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, lag_alarm_minutes=30)
    seed_logs(
        cfg.logs.mirror_root, cfg.logs.db_path,
        [("core-1", NOW - timedelta(hours=2), "system,info", "old")],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert run.remaining_candidates == 0  # the one event is judged; nothing remains
    silent = [f for f in _watchdog(store) if f.dedup_extra == "pipeline-silent"]
    assert len(silent) == 1  # newest ingest is 2h old > 30m → pipeline looks silent


def test_future_stamped_event_does_not_false_alarm(tmp_path: Path) -> None:
    # A device with a skewed clock stamps the future; the clamped ingest age must not read stale,
    # so no pipeline-silent alarm fires (D6).
    cfg = watch_config(tmp_path, lag_alarm_minutes=30)
    seed_logs(
        cfg.logs.mirror_root, cfg.logs.db_path,
        [("core-1", NOW + timedelta(days=2), "system,info", "future")],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert _watchdog(store) == []


def test_backlog_alarm_dedups_across_runs(tmp_path: Path) -> None:
    cfg = _window(watch_config(tmp_path, lag_alarm_events=2), 2)
    rows = [("core-1", NOW - timedelta(minutes=9 - i), "system,error", f"e{i}") for i in range(9)]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    # still behind next run, within the dedup TTL → the same backlog key is suppressed.
    run2 = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(minutes=5), llm_client=_empty_llm()
    )
    assert run2.remaining_candidates > 2  # still behind
    backlog = [f for f in _watchdog(store) if f.dedup_extra == "backlog"]
    assert len(backlog) == 1  # alerted once, not every run
