"""US2 — the self-monitoring heartbeat (feature 062, A4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _empty_llm() -> StubLLMClient:
    return StubLLMClient([tool_response("report_findings", {"findings": []})])


def _seed_one(cfg) -> None:  # type: ignore[no-untyped-def]
    seed_logs(
        cfg.logs.mirror_root, cfg.logs.db_path,
        [("core-1", NOW - timedelta(minutes=2), "system,info", "ok")],
    )


def test_inaugural_heartbeat_sent_and_state_stamped(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, heartbeat_hours=24)
    _seed_one(cfg)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    run = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm(), notifier=notifier
    )
    assert run.heartbeat_sent and len(notifier.status_messages) == 1
    sc = store.load_selfcheck()
    assert sc.last_heartbeat_at == NOW and sc.period_runs == 0  # stamped + period reset


def test_no_heartbeat_before_cadence_then_fires_after(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, heartbeat_hours=24)
    _seed_one(cfg)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm(), notifier=notifier)
    # 1h later: within the 24h cadence → no second heartbeat, but the run still accumulates.
    r2 = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(hours=1),
        llm_client=_empty_llm(), notifier=notifier,
    )
    assert not r2.heartbeat_sent and len(notifier.status_messages) == 1
    # 25h after the first: cadence elapsed → second heartbeat.
    r3 = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(hours=25),
        llm_client=_empty_llm(), notifier=notifier,
    )
    assert r3.heartbeat_sent and len(notifier.status_messages) == 2


def test_accumulators_roll_between_heartbeats(tmp_path: Path) -> None:
    # heartbeat disabled → the period keeps accumulating (no reset).
    cfg = watch_config(tmp_path, heartbeat_hours=0)
    seed_logs(
        cfg.logs.mirror_root, cfg.logs.db_path,
        [("core-1", NOW - timedelta(minutes=3), "system,error", "a")],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm(), notifier=notifier)
    seed_logs(
        cfg.logs.mirror_root, cfg.logs.db_path,
        [("core-1", NOW + timedelta(minutes=1), "system,error", "b")],
    )
    run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW + timedelta(minutes=2),
        llm_client=_empty_llm(), notifier=notifier,
    )
    sc = store.load_selfcheck()
    assert sc.period_runs == 2 and sc.period_events == 2 and notifier.status_messages == []


def test_heartbeat_delivery_failure_retains_state(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, heartbeat_hours=24)
    _seed_one(cfg)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier(status_result=DeliveryResult(delivered=0, failed=1, error="boom"))

    run = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm(), notifier=notifier
    )
    assert not run.heartbeat_sent
    assert any("heartbeat delivery failed" in c for c in run.conditions)
    sc = store.load_selfcheck()
    assert sc.last_heartbeat_at is None  # NOT stamped → retried next run
    assert sc.period_runs == 1  # period retained


def test_dry_run_sends_no_heartbeat_and_mutates_no_state(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, heartbeat_hours=24)
    _seed_one(cfg)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    run = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm(),
        notifier=notifier, dry_run=True,
    )
    assert not run.heartbeat_sent and notifier.status_messages == []
    assert store.load_selfcheck().period_runs == 0  # dry-run persisted nothing


def test_pre062_state_loads_clean(tmp_path: Path) -> None:
    # FR-014: a state.json written before this feature (no "selfcheck" key) loads without error;
    # the first upgraded run then sends an inaugural heartbeat.
    cfg = watch_config(tmp_path, heartbeat_hours=24)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)  # writes a pre-062 state.json (cursor/last_run/open_findings only)
    sc = store.load_selfcheck()
    assert sc.last_heartbeat_at is None and sc.period_runs == 0


def test_degraded_period_heartbeat_says_degraded(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, heartbeat_hours=24, lag_alarm_events=0)
    _seed_one(cfg)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    notifier = FakeNotifier()

    # llm_client=None ⇒ the nonempty window records "llm_unavailable" → a degraded period.
    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=None, notifier=notifier)
    assert run.heartbeat_sent
    assert "degraded" in notifier.status_messages[0]
