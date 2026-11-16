"""Unit (feature 027): the metrics-evaluation pass inside `obi watch run`.

Covers the detector mapping (increase → a `source="metrics"` finding), the merge into `run_cycle`
and delivery through the existing path, alert-once namespacing per interface+counter, and the
offline / opt-in / resilience properties (fires with no LLM, survives a missing log store, is
inactive when disabled, honors dry-run).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import logstore, metricstore
from obi.core.audit import AuditTrail
from obi.core.config import WatchMetricsSettings
from obi.core.metricstore import StoredSample
from obi.core.watch import WatchFinding, _evaluate_metrics, run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import FakeNotifier, watch_config

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


def _err_row(
    ts: datetime,
    value: int,
    *,
    metric: str = "ifInErrors",
    device: str = "core-gw-1",
    ifindex: str = "1",
    ifname: str = "ether1",
) -> StoredSample:
    return StoredSample(
        ts_us=int(ts.timestamp() * 1_000_000),
        ts_iso=ts.isoformat(),
        device=device,
        address="172.18.84.1",
        profile="interfaces",
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="counter",
        value=value,
    )


def _seed_metrics(path: Path, rows: list[StoredSample]) -> None:
    conn = metricstore.connect(path)
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()


def _cfg(tmp_path, *, threshold=0, counters=None, enabled=True, slack=False):
    cfg = watch_config(tmp_path, slack=slack)
    cfg.watch.metrics = WatchMetricsSettings(
        enabled=enabled,
        threshold=threshold,
        window_seconds=3600,
        counters=counters or ["ifInErrors", "ifOutErrors"],
        db_path=tmp_path / "metrics.db",
    )
    return cfg


def _rising(tmp_path, **kw) -> None:
    """ifInErrors 100 → 1532 (+1432) across the window."""
    _seed_metrics(
        tmp_path / "metrics.db",
        [
            _err_row(NOW - timedelta(minutes=30), 100, **kw),
            _err_row(NOW - timedelta(minutes=1), 1532, **kw),
        ],
    )


def _empty_logs(cfg) -> None:
    logstore.connect(cfg.logs.db_path).close()  # create an empty logs store → not degraded


def _run(cfg, tmp_path, *, notifier=None, now=NOW, llm_client=None, dry_run=False):
    return run_cycle(
        cfg,
        WatchStore(cfg.watch.watch_dir),
        audit=AuditTrail(cfg.audit_path),
        now=now,
        llm_client=llm_client,
        notifier=notifier,
        dry_run=dry_run,
    )


# --- T004 (US1): detector mapping -------------------------------------------------------------


def test_evaluate_maps_increase_to_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _rising(tmp_path)
    findings = _evaluate_metrics(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.source == "metrics"
    assert f.severity == "warning"
    assert f.confidence == "high"
    assert f.device == "core-gw-1"
    assert f.device_address == "172.18.84.1"
    assert "ether1" in f.summary and "ifInErrors" in f.summary and "1,432" in f.summary
    assert f.dedup_extra == "ether1:ifInErrors"
    assert f.evidence and f.evidence[0].kind == "metric"


def test_no_increase_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_metrics(
        tmp_path / "metrics.db",
        [_err_row(NOW - timedelta(minutes=30), 100), _err_row(NOW - timedelta(minutes=1), 100)],
    )
    assert _evaluate_metrics(cfg, now=NOW) == []


def test_below_threshold_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, threshold=100)
    _seed_metrics(
        tmp_path / "metrics.db",
        [_err_row(NOW - timedelta(minutes=30), 100), _err_row(NOW - timedelta(minutes=1), 150)],
    )  # +50 < 100
    assert _evaluate_metrics(cfg, now=NOW) == []


def test_ignored_interfaces_excluded_by_default(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # default ignore_interfaces = ["wireguard"]
    _seed_metrics(
        tmp_path / "metrics.db",
        [
            _err_row(NOW - timedelta(minutes=30), 100, ifindex="1", ifname="wireguard1"),
            _err_row(NOW - timedelta(minutes=1), 5000, ifindex="1", ifname="wireguard1"),
            _err_row(NOW - timedelta(minutes=30), 10, ifindex="2", ifname="ether1"),
            _err_row(NOW - timedelta(minutes=1), 40, ifindex="2", ifname="ether1"),
        ],
    )
    findings = _evaluate_metrics(cfg, now=NOW)
    assert {f.dedup_extra for f in findings} == {"ether1:ifInErrors"}  # wireguard1 skipped


# --- T005 (US1): run_cycle merge + delivery ---------------------------------------------------


def test_run_cycle_delivers_metric_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _rising(tmp_path)
    notifier = FakeNotifier()
    run = _run(cfg, tmp_path, notifier=notifier)
    assert len(run.finding_ids) == 1
    assert notifier.batches and notifier.batches[0][0].source == "metrics"
    assert list((cfg.watch.watch_dir / "reports").glob("*.md"))  # report written


def test_multiple_interfaces_each_produce_a_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _seed_metrics(
        tmp_path / "metrics.db",
        [
            _err_row(NOW - timedelta(minutes=30), 100, ifindex="1", ifname="ether1"),
            _err_row(NOW - timedelta(minutes=1), 200, ifindex="1", ifname="ether1"),
            _err_row(NOW - timedelta(minutes=30), 10, ifindex="2", ifname="ether2"),
            _err_row(NOW - timedelta(minutes=1), 40, ifindex="2", ifname="ether2"),
        ],
    )
    notifier = FakeNotifier()
    run = _run(cfg, tmp_path, notifier=notifier)
    assert len(run.finding_ids) == 2
    delivered = {f.dedup_extra for f in notifier.batches[0]}
    assert delivered == {"ether1:ifInErrors", "ether2:ifInErrors"}


# --- T009 (US2): alert-once namespacing -------------------------------------------------------


def test_alert_once_across_cycles(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _rising(tmp_path)
    store = WatchStore(cfg.watch.watch_dir)
    run1 = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, notifier=FakeNotifier())
    assert len(run1.finding_ids) == 1
    n2 = FakeNotifier()
    run2 = run_cycle(
        cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW + timedelta(minutes=1), notifier=n2
    )
    assert run2.finding_ids == []  # ongoing condition suppressed (alert-once)
    assert n2.batches == []


def test_metric_findings_have_distinct_dedup_keys() -> None:
    def mk(extra: str, source: str = "metrics", dedup_extra: str | None = None) -> WatchFinding:
        return WatchFinding(
            id="",
            created_at=NOW,
            device="core-gw-1",
            window_start=NOW,
            window_end=NOW,
            severity="warning",
            confidence="high",
            summary="",
            description="",
            evidence=[],
            source=source,
            dedup_extra=dedup_extra,
        )

    a = mk("a", dedup_extra="ether1:ifInErrors")
    b = mk("b", dedup_extra="ether2:ifInErrors")
    assert a.dedup_key() != b.dedup_key()
    # LLM findings (no dedup_extra) keep the exact legacy key — regression guard.
    llm = mk("", source="llm", dedup_extra=None)
    assert llm.dedup_key() == "llm|core-gw-1|warning"


# --- T010/T011 (US3): offline / opt-in / resilience -------------------------------------------


def test_metrics_pass_survives_missing_log_store(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)  # logs.db intentionally NOT created
    _rising(tmp_path)
    notifier = FakeNotifier()
    run = _run(cfg, tmp_path, notifier=notifier)
    assert "store_unavailable" in run.conditions  # the log pass degraded
    assert len(run.finding_ids) == 1  # but the independent metrics pass still fired (FR-009)
    assert notifier.batches[0][0].source == "metrics"


def test_disabled_metrics_leaves_cycle_unchanged(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # no [watch.metrics]
    assert cfg.watch.metrics is None
    _empty_logs(cfg)
    _rising(tmp_path)  # data exists, but the pass is inactive
    run = _run(cfg, tmp_path)
    assert run.finding_ids == []


def test_missing_metrics_store_no_finding_no_crash(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # metrics enabled, but no metrics.db on disk
    assert _evaluate_metrics(cfg, now=NOW) == []


def test_dry_run_writes_and_delivers_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    _rising(tmp_path)
    notifier = FakeNotifier()
    _run(cfg, tmp_path, notifier=notifier, dry_run=True)
    assert notifier.batches == []
    reports = cfg.watch.watch_dir / "reports"
    assert not reports.exists() or not list(reports.glob("*.md"))


def test_135_maintenance_device_skipped(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    rows = [
        _err_row(NOW - timedelta(minutes=30), 10, ifindex="2", ifname="ether1"),
        _err_row(NOW - timedelta(minutes=1), 40, ifindex="2", ifname="ether1"),
    ]
    cfg = _cfg(tmp_path)
    _seed_metrics(tmp_path / "metrics.db", rows)
    device = _evaluate_metrics(cfg, now=NOW)[0].device
    assert device
    cfg.maintenance = MaintenanceSettings(devices=(device.lower(),))
    assert _evaluate_metrics(cfg, now=NOW) == []
