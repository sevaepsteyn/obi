"""Unit (feature 030): the device-reachability pass inside `obi watch run`.

Covers the detector mapping (a stale device → a `source="reachability"` finding), the ≥ratio
mass-outage coalescing vs per-device findings and the transition between them, the single-blip
boundary (US2), alert-once + recurrence (US3), and the offline / opt-in / resilience / ignore
properties (US4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import logstore, metricstore
from obi.core.audit import AuditTrail
from obi.core.config import WatchReachabilitySettings
from obi.core.metricstore import StoredSample
from obi.core.watch import _evaluate_reachability, run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import FakeNotifier, watch_config

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


def _row(ts: datetime, *, device: str, address: str = "172.18.84.1") -> StoredSample:
    return StoredSample(
        ts_us=int(ts.timestamp() * 1_000_000),
        ts_iso=ts.isoformat(),
        device=device,
        address=address,
        profile="system",
        metric="cpuLoad",
        ifindex="",
        ifname=None,
        kind="gauge",
        value=1,
    )


def _device(
    device: str,
    *,
    age_min: float,
    cadence_min: int = 5,
    count: int = 6,
    address: str = "172.18.84.1",
) -> list[StoredSample]:
    """``count`` readings at ``cadence_min`` spacing; the newest at ``NOW - age_min``."""
    newest = NOW - timedelta(minutes=age_min)
    return [
        _row(newest - timedelta(minutes=cadence_min * i), device=device, address=address)
        for i in range(count)
    ]


def _seed(path: Path, *device_rows: list[StoredSample]) -> None:
    conn = metricstore.connect(path)
    metricstore.insert_samples(conn, [r for rows in device_rows for r in rows])
    conn.commit()
    conn.close()


def _add(path: Path, rows: list[StoredSample]) -> None:
    conn = metricstore.connect(path)
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()


def _cfg(
    tmp_path: Path,
    *,
    enabled: bool = True,
    stale_after_cycles: int = 2,
    mass_outage_ratio: float = 0.8,
    ignore_devices: list[str] | None = None,
    slack: bool = False,
    dedup_ttl_hours: int = 6,
):
    cfg = watch_config(tmp_path, slack=slack)
    cfg.watch.dedup_ttl_hours = dedup_ttl_hours
    cfg.watch.reachability = WatchReachabilitySettings(
        enabled=enabled,
        stale_after_cycles=stale_after_cycles,
        mass_outage_ratio=mass_outage_ratio,
        ignore_devices=ignore_devices or [],
        db_path=tmp_path / "metrics.db",
    )
    return cfg


def _empty_logs(cfg) -> None:
    logstore.connect(cfg.logs.db_path).close()  # empty logs store → log pass not degraded


def _run(cfg, *, notifier=None, now=NOW, llm_client=None, dry_run=False, store=None):
    return run_cycle(
        cfg,
        store or WatchStore(cfg.watch.watch_dir),
        audit=AuditTrail(cfg.audit_path),
        now=now,
        llm_client=llm_client,
        notifier=notifier,
        dry_run=dry_run,
    )


# --- T004 (US1): detector mapping -------------------------------------------------------------


def test_evaluate_flags_stale_device(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(
        tmp_path / "metrics.db",
        _device("core-gw-1", age_min=12),  # stale (age 12m > 2×5m)
        _device("walnut-gw", age_min=3, address="172.18.86.1"),  # fresh
    )
    findings = _evaluate_reachability(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.source == "reachability"
    assert f.severity == "warning"
    assert f.confidence == "high"
    assert f.device == "core-gw-1"
    assert f.device_address == "172.18.84.1"
    assert "no SNMP metrics" in f.summary and "missed cycles" in f.summary
    assert f.dedup_extra is None
    assert f.evidence and f.evidence[0].kind == "metric"


def test_fresh_device_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=3))
    assert _evaluate_reachability(cfg, now=NOW) == []


# --- T005 (US1): run_cycle merge + delivery ---------------------------------------------------


def test_run_cycle_delivers_reachability_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=12))
    notifier = FakeNotifier()
    run = _run(cfg, notifier=notifier)
    assert len(run.finding_ids) == 1
    assert notifier.batches and notifier.batches[0][0].source == "reachability"
    assert list((cfg.watch.watch_dir / "reports").glob("*.md"))  # report written


def test_several_stale_below_ratio_each_own_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)  # ratio 0.8
    _empty_logs(cfg)
    # 3 stale of 5 watched → 0.6 < 0.8 → per-device findings
    _seed(
        tmp_path / "metrics.db",
        _device("a", age_min=12, address="172.25.110.1"),
        _device("b", age_min=12, address="172.25.110.2"),
        _device("c", age_min=12, address="172.25.110.3"),
        _device("d", age_min=3, address="172.25.110.4"),
        _device("e", age_min=3, address="172.25.110.5"),
    )
    notifier = FakeNotifier()
    run = _run(cfg, notifier=notifier)
    assert len(run.finding_ids) == 3
    assert {f.device for f in notifier.batches[0]} == {"a", "b", "c"}
    assert all(f.source == "reachability" and f.dedup_extra is None for f in notifier.batches[0])


# --- T006 (US1): mass-outage coalescing + transition ------------------------------------------


def test_mass_outage_coalesces(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    # 4 stale of 5 → 0.8 ≥ 0.8 → one coalesced finding
    _seed(
        tmp_path / "metrics.db",
        _device("a", age_min=12, address="172.25.110.1"),
        _device("b", age_min=12, address="172.25.110.2"),
        _device("c", age_min=12, address="172.25.110.3"),
        _device("d", age_min=12, address="172.25.110.4"),
        _device("e", age_min=3, address="172.25.110.5"),
    )
    notifier = FakeNotifier()
    run = _run(cfg, notifier=notifier)
    assert len(run.finding_ids) == 1
    f = notifier.batches[0][0]
    assert f.source == "reachability"
    assert f.device == "collection"
    assert f.dedup_extra == "collection-stopped"
    assert "4/5 devices silent" in f.summary
    assert "Silent: a, b, c, d." in f.description  # sorted silent identities; fresh 'e' excluded


def test_coalesced_to_per_device_transition(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    db = tmp_path / "metrics.db"
    rows = [_device(n, age_min=12, address=f"172.25.110.{i}") for i, n in enumerate("abcde", 1)]
    _seed(db, *rows)
    store = WatchStore(cfg.watch.watch_dir)
    # cycle 1: 5/5 stale → one coalesced finding
    n1 = FakeNotifier()
    run1 = _run(cfg, notifier=n1, store=store)
    assert len(run1.finding_ids) == 1 and n1.batches[0][0].device == "collection"
    # a, b, c report again → only d, e stale = 2/5 = 0.4 < 0.8 → per-device
    _add(db, [_row(NOW, device=d, address="10.9.9.9") for d in ("a", "b", "c")])
    n2 = FakeNotifier()
    run2 = _run(cfg, notifier=n2, now=NOW + timedelta(minutes=1), store=store)
    assert len(run2.finding_ids) == 2
    assert {f.device for f in n2.batches[0]} == {"d", "e"}
    assert all(f.dedup_extra is None for f in n2.batches[0])


# --- T009 (US2): single-blip boundary ---------------------------------------------------------


def test_fresh_within_cadence_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path / "metrics.db", _device("g", age_min=3))  # age 3m ≤ cadence
    assert _evaluate_reachability(cfg, now=NOW) == []


def test_single_missed_cycle_tolerated(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path / "metrics.db", _device("g", age_min=8))  # 5m < 8m < 10m → one miss
    assert _evaluate_reachability(cfg, now=NOW) == []


def test_two_missed_cycles_alerts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path / "metrics.db", _device("g", age_min=12))  # 12m > 2×5m
    assert len(_evaluate_reachability(cfg, now=NOW)) == 1


def test_stale_after_cycles_shifts_boundary(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, stale_after_cycles=3)  # threshold now 3×5m = 15m
    _seed(tmp_path / "metrics.db", _device("g", age_min=12))  # 12m < 15m → not stale
    assert _evaluate_reachability(cfg, now=NOW) == []
    (tmp_path / "metrics.db").unlink()
    _seed(tmp_path / "metrics.db", _device("g", age_min=17))  # 17m > 15m → stale
    assert len(_evaluate_reachability(cfg, now=NOW)) == 1


def test_thin_history_skipped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path / "metrics.db", [_row(NOW - timedelta(minutes=12), device="g")])  # 1 reading
    assert _evaluate_reachability(cfg, now=NOW) == []


# --- T010 (US3): alert-once + recurrence ------------------------------------------------------


def test_ongoing_outage_alerts_once(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=12))
    store = WatchStore(cfg.watch.watch_dir)
    run1 = _run(cfg, notifier=FakeNotifier(), store=store)
    assert len(run1.finding_ids) == 1
    n2 = FakeNotifier()
    run2 = _run(cfg, notifier=n2, now=NOW + timedelta(minutes=5), store=store)
    assert run2.finding_ids == [] and n2.batches == []  # ongoing → suppressed


def test_two_silent_devices_distinct_keys(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    # 2 stale of 3 (0.67 < 0.8) → per-device
    _seed(
        tmp_path / "metrics.db",
        _device("a", age_min=12, address="172.25.110.1"),
        _device("b", age_min=12, address="172.25.110.2"),
        _device("c", age_min=3, address="172.25.110.3"),
    )
    n = FakeNotifier()
    run = _run(cfg, notifier=n)
    assert len(run.finding_ids) == 2
    assert {f.dedup_key() for f in n.batches[0]} == {
        "reachability|a|warning",
        "reachability|b|warning",
    }


def test_coalesced_dedups_as_one(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    rows = [_device(n, age_min=12, address=f"172.25.110.{i}") for i, n in enumerate("abcd", 1)]
    _seed(tmp_path / "metrics.db", *rows)
    store = WatchStore(cfg.watch.watch_dir)
    run1 = _run(cfg, notifier=FakeNotifier(), store=store)
    assert len(run1.finding_ids) == 1
    n2 = FakeNotifier()
    run2 = _run(cfg, notifier=n2, now=NOW + timedelta(minutes=5), store=store)
    assert run2.finding_ids == []  # the coalesced condition alert-onces


def test_recovery_then_reoutage_realerts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True, dedup_ttl_hours=1)
    _empty_logs(cfg)
    db = tmp_path / "metrics.db"
    _seed(db, _device("core-gw-1", age_min=12))
    store = WatchStore(cfg.watch.watch_dir)
    run1 = _run(cfg, notifier=FakeNotifier(), store=store)
    assert len(run1.finding_ids) == 1  # onset alert
    _add(db, [_row(NOW, device="core-gw-1")])  # device reports again (recovers)
    run2 = _run(cfg, notifier=FakeNotifier(), now=NOW + timedelta(minutes=2), store=store)
    assert run2.finding_ids == []  # fresh again → cleared, no alert
    n3 = FakeNotifier()
    # silent again (no new readings) and now past the TTL → re-alert
    run3 = _run(cfg, notifier=n3, now=NOW + timedelta(hours=2), store=store)
    assert len(run3.finding_ids) == 1 and n3.batches[0][0].source == "reachability"


# --- T011 (US4): offline + independence -------------------------------------------------------


def test_fires_with_no_llm_client(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=12))
    n = FakeNotifier()
    run = _run(cfg, notifier=n, llm_client=None)
    assert len(run.finding_ids) == 1 and n.batches[0][0].source == "reachability"


def test_survives_missing_log_store(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)  # logs.db intentionally NOT created
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=12))
    n = FakeNotifier()
    run = _run(cfg, notifier=n)
    assert "store_unavailable" in run.conditions  # the log pass degraded
    assert len(run.finding_ids) == 1  # the independent reachability pass still fired (FR-009)
    assert n.batches[0][0].source == "reachability"


# --- T012 (US4): opt-in / resilience / ignore -------------------------------------------------


def test_disabled_leaves_cycle_unchanged(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # no [watch.reachability]
    assert cfg.watch.reachability is None
    _empty_logs(cfg)
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=12))
    assert _run(cfg).finding_ids == []


def test_missing_metrics_store_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # enabled, but no metrics.db on disk
    assert _evaluate_reachability(cfg, now=NOW) == []


def test_empty_metrics_store_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    metricstore.connect(tmp_path / "metrics.db").close()  # created but empty
    assert _evaluate_reachability(cfg, now=NOW) == []


def test_dry_run_writes_and_delivers_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _seed(tmp_path / "metrics.db", _device("core-gw-1", age_min=12))
    n = FakeNotifier()
    _run(cfg, notifier=n, dry_run=True)
    assert n.batches == []
    reports = cfg.watch.watch_dir / "reports"
    assert not reports.exists() or not list(reports.glob("*.md"))


def test_ignore_devices_excludes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ignore_devices=["core-gw-1"])
    _seed(
        tmp_path / "metrics.db",
        _device("core-gw-1", age_min=12, address="172.18.84.1"),  # stale but ignored
        _device("walnut-gw", age_min=12, address="172.18.86.1"),  # stale
        _device("yew-gw", age_min=3, address="172.18.80.1"),  # fresh
        _device("ponderosa-gw", age_min=3, address="172.18.87.1"),  # fresh
    )
    findings = _evaluate_reachability(cfg, now=NOW)  # watched = walnut/yew/ponderosa; 1/3 < 0.8
    assert [f.device for f in findings] == ["walnut-gw"]


def test_135_maintenance_device_excluded_like_ignored(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = _cfg(tmp_path)
    cfg.maintenance = MaintenanceSettings(sites=("walnut",))
    _seed(
        tmp_path / "metrics.db",
        _device("walnut-gw", age_min=12, address="172.18.86.1"),  # stale but in maintenance
        _device("core-gw-1", age_min=12, address="172.18.84.1"),  # stale
        _device("yew-gw", age_min=3, address="172.18.80.1"),
        _device("ponderosa-gw", age_min=3, address="172.18.87.1"),
    )
    findings = _evaluate_reachability(cfg, now=NOW)  # watched excludes walnut (denominator too)
    assert [f.device for f in findings] == ["core-gw-1"]
