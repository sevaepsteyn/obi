"""Unit (feature 096): the ``[watch.health]`` deterministic pass inside ``obi watch run``.

The sibling canon (072/087 baseline-transition shape) plus the pass-specific rules:
freshness gate (FR-016), sustained CPU/memory (FR-014), fan-stop vs fan-absent, ignore
lists, FR-017 alert content, and mass coalescing. Plus one ``run_cycle`` end-to-end with
``FakeNotifier`` and the D4 regression (a Linux collector VM never leaks into 030).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obi.core import logstore, metricstore
from obi.core.audit import AuditTrail
from obi.core.config import WatchHealthSettings
from obi.core.metricstore import StoredSample
from obi.core.watch import _HEALTH_BASELINE, _evaluate_health, run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import FakeNotifier, watch_config

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
NOW_US = int(NOW.timestamp() * 1_000_000)


def _cfg(tmp_path: Path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = watch_config(tmp_path)
    settings = {"enabled": True, "db_path": tmp_path / "metrics.db", **overrides}
    cfg.watch.health = WatchHealthSettings(**settings)
    logstore.connect(cfg.logs.db_path).close()  # empty logs store → log pass not degraded
    return cfg


def _row(device: str, metric: str, value: int, *, age_s: float = 60, ifindex: str = "",
         ifname: str | None = None, profile: str = "health") -> StoredSample:
    ts_us = NOW_US - int(age_s * 1_000_000)
    return StoredSample(
        ts_us=ts_us,
        ts_iso=datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address="172.18.81.1",
        profile=profile,
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="gauge",
        value=value,
    )


def _seed(tmp_path: Path, rows: list[StoredSample]) -> None:
    conn = metricstore.connect(tmp_path / "metrics.db")
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()


def _baseline(cfg, mapping: dict[str, bool]) -> None:
    path = cfg.watch.watch_dir / _HEALTH_BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping))


def _read_baseline(cfg) -> dict:
    return json.loads((cfg.watch.watch_dir / _HEALTH_BASELINE).read_text())


_HOT = "chestnut-gw|health:board-temperature2|7102"


def _hot_board(age_s: float = 60) -> StoredSample:
    return _row("chestnut-gw", "health:board-temperature2", 810, age_s=age_s,
                ifindex="7102", ifname="board-temperature2")  # 81.0 °C > 75


def _cool_board(age_s: float = 60) -> StoredSample:
    return _row("chestnut-gw", "health:board-temperature2", 450, age_s=age_s,
                ifindex="7102", ifname="board-temperature2")


# --- transition semantics ---------------------------------------------------------------------


def test_healthy_to_breached_transition_alerts_with_full_content(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path, [_hot_board()])
    _baseline(cfg, {_HOT: True})
    findings = _evaluate_health(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.source == "health" and f.severity == "warning"
    assert f.device == "chestnut-gw"
    assert f.dedup_extra == "chestnut-gw:board-temperature2:temperature"
    # FR-017: device, sensor instance, observed value+unit, threshold, first-seen
    text = f.summary + f.description + " ".join(e.text for e in f.evidence)
    assert "board-temperature2" in text
    assert "81" in text and "°C" in text
    assert "75" in text
    assert NOW.strftime("%Y-%m-%d") in text


def test_breached_stays_breached_silently_and_recovery_is_silent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path, [_hot_board()])
    _baseline(cfg, {_HOT: False})  # already breached at baseline
    assert _evaluate_health(cfg, now=NOW) == []

    # recovery: breached at baseline, now cool — silent, baseline flips back to healthy
    cfg2 = _cfg(tmp_path / "r")
    (tmp_path / "r").mkdir(exist_ok=True)
    _seed(tmp_path / "r", [_cool_board()])
    _baseline(cfg2, {_HOT: False})
    assert _evaluate_health(cfg2, now=NOW) == []
    assert _read_baseline(cfg2)[_HOT] is True  # a later re-breach re-alerts


def test_first_run_seeds_silently_even_when_breached(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path, [_hot_board()])
    findings = _evaluate_health(cfg, now=NOW)
    assert findings == []
    assert _read_baseline(cfg)[_HOT] is False  # seeded as breached — never alerts for it


def test_dry_run_writes_no_baseline(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path, [_hot_board()])
    _evaluate_health(cfg, now=NOW, dry_run=True)
    assert not (cfg.watch.watch_dir / _HEALTH_BASELINE).exists()


# --- gates ------------------------------------------------------------------------------------


def test_disabled_absent_section_and_missing_store_are_noops(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enabled=False)
    _seed(tmp_path, [_hot_board()])
    assert _evaluate_health(cfg, now=NOW) == []

    cfg2 = watch_config(tmp_path)
    cfg2.watch.health = None
    assert _evaluate_health(cfg2, now=NOW) == []

    cfg3 = _cfg(tmp_path)
    cfg3.watch.health = WatchHealthSettings(enabled=True, db_path=tmp_path / "missing.db")
    assert _evaluate_health(cfg3, now=NOW) == []


def test_freshness_gate_stale_device_is_silent(tmp_path: Path) -> None:
    """FR-016: a device with no fresh sample produces no health alert — staleness belongs
    to the reachability passes. The stale device's baseline knowledge is carried forward."""

    cfg = _cfg(tmp_path)
    _seed(tmp_path, [_hot_board(age_s=1200)])  # 20 min old > 3×5min freshness gate
    _baseline(cfg, {_HOT: True})
    assert _evaluate_health(cfg, now=NOW) == []
    assert _read_baseline(cfg)[_HOT] is True  # carried forward, not forgotten


def test_ignore_lists_filter_devices_and_sensors(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ignore_devices=["chestnut"])
    _seed(tmp_path, [_hot_board()])
    _baseline(cfg, {_HOT: True})
    assert _evaluate_health(cfg, now=NOW) == []

    cfg2 = _cfg(tmp_path, ignore_sensors=["board-temperature"])
    assert _evaluate_health(cfg2, now=NOW) == []


# --- sustained CPU/memory (FR-014) ------------------------------------------------------------


def test_two_of_three_hot_cpu_samples_stay_silent_three_alert(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    key = "chestnut-gw|cpuLoad|"
    _seed(
        tmp_path,
        [
            _row("chestnut-gw", "cpuLoad", 95, age_s=600, profile="system"),
            _row("chestnut-gw", "cpuLoad", 40, age_s=300, profile="system"),
            _row("chestnut-gw", "cpuLoad", 96, age_s=60, profile="system"),
        ],
    )
    _baseline(cfg, {key: True})
    assert _evaluate_health(cfg, now=NOW) == []  # latest hot, but not sustained

    cfg2 = _cfg(tmp_path / "s")
    (tmp_path / "s").mkdir(exist_ok=True)
    _seed(
        tmp_path / "s",
        [
            _row("chestnut-gw", "cpuLoad", 95, age_s=600, profile="system"),
            _row("chestnut-gw", "cpuLoad", 93, age_s=300, profile="system"),
            _row("chestnut-gw", "cpuLoad", 96, age_s=60, profile="system"),
        ],
    )
    _baseline(cfg2, {key: True})
    findings = _evaluate_health(cfg2, now=NOW)
    assert len(findings) == 1
    assert findings[0].dedup_extra.endswith(":cpu")


# --- fan semantics ----------------------------------------------------------------------------


def test_fan_stop_alerts_fan_absent_is_silent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    key = "chestnut-gw|health:fan1-speed|7001"
    _seed(tmp_path, [_row("chestnut-gw", "health:fan1-speed", 0, ifindex="7001",
                          ifname="fan1-speed")])
    _baseline(cfg, {key: True})
    findings = _evaluate_health(cfg, now=NOW)
    assert len(findings) == 1 and findings[0].dedup_extra.endswith(":fan")

    # a fanless model simply has no fan series — nothing to evaluate, nothing to alert
    cfg2 = _cfg(tmp_path / "f")
    (tmp_path / "f").mkdir(exist_ok=True)
    _seed(tmp_path / "f", [_row("manzanita-gw", "health:cpu-temperature", 500, ifindex="17",
                                ifname="cpu-temperature")])
    assert _evaluate_health(cfg2, now=NOW) == []  # first run; and no fan keys exist
    assert not any("fan" in k for k in _read_baseline(cfg2))


# --- coalescing -------------------------------------------------------------------------------


def test_mass_breach_coalesces_below_ratio_stays_per_sensor(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mass_outage_ratio=0.8)
    rows, mapping = [], {}
    for dev in ("a-gw", "b-gw", "c-gw"):
        rows.append(_row(dev, "health:cpu-temperature", 900, ifindex="17",
                         ifname="cpu-temperature"))
        mapping[f"{dev}|health:cpu-temperature|17"] = True
    _seed(tmp_path, rows)
    _baseline(cfg, mapping)
    findings = _evaluate_health(cfg, now=NOW)
    assert len(findings) == 1
    assert findings[0].dedup_extra == "health-mass-breach"
    assert "3" in findings[0].summary

    cfg2 = _cfg(tmp_path / "b", mass_outage_ratio=0.8)
    (tmp_path / "b").mkdir(exist_ok=True)
    rows2 = [
        _row("a-gw", "health:cpu-temperature", 900, ifindex="17", ifname="cpu-temperature"),
        _row("b-gw", "health:cpu-temperature", 450, ifindex="17", ifname="cpu-temperature"),
        _row("c-gw", "health:cpu-temperature", 450, ifindex="17", ifname="cpu-temperature"),
    ]
    _seed(tmp_path / "b", rows2)
    _baseline(cfg2, mapping)
    findings = _evaluate_health(cfg2, now=NOW)
    assert len(findings) == 1 and findings[0].device == "a-gw"


# --- end-to-end -------------------------------------------------------------------------------


def test_run_cycle_delivers_health_finding_and_writes_report(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed(tmp_path, [_hot_board()])
    _baseline(cfg, {_HOT: True})
    notifier = FakeNotifier()
    run = run_cycle(
        cfg,
        WatchStore(cfg.watch.watch_dir),
        audit=AuditTrail(cfg.audit_path),
        now=NOW,
        llm_client=None,
        notifier=notifier,
    )
    assert run.finding_ids  # the health finding entered the cycle pipeline
    assert notifier.batches and notifier.batches[0][0].source == "health"
    reports = list((cfg.watch.watch_dir / "reports").glob("*.md"))
    assert reports and "board-temperature2" in reports[0].read_text()


def test_reachability_pass_never_adopts_linux_collector_rows(tmp_path: Path) -> None:
    """D4 regression: Linux probe rows share metrics.db; 030's freshness-derived watch set
    must not treat a collector VM as an SNMP device (probe failure ⇒ no 'unreachable')."""

    from obi.core.config import WatchReachabilitySettings
    from obi.core.watch import _evaluate_reachability

    cfg = watch_config(tmp_path)
    cfg.watch.reachability = WatchReachabilitySettings(
        enabled=True, db_path=tmp_path / "metrics.db"
    )
    rows = []
    # a healthy SNMP gateway with a real cadence...
    for age in (3600, 3000, 2400, 1800, 1200):
        rows.append(_row("chestnut-gw", "sysUpTime", 100 + age, age_s=age, profile="system"))
    # ...and a Linux collector whose probe DIED an hour ago (stale by any cadence math)
    for age in (7200, 6900, 6600, 6300, 6000):
        rows.append(
            _row("core-vms-1", "linux:cpuUtilPercent", 40, age_s=age, profile="linux-health")
        )
    _seed(tmp_path, rows)
    findings = _evaluate_reachability(cfg, now=NOW)
    assert not any(f.device == "core-vms-1" for f in findings)


# --- US3: the Linux collector fleet rides the same pass ---------------------------------------


def test_vm_disk_breach_transitions_to_exactly_one_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    key = "core-vms-1|linux:diskUsedPercent|/"
    _seed(tmp_path, [_row("core-vms-1", "linux:diskUsedPercent", 910, ifindex="/",
                          ifname="/", profile="linux-health")])
    _baseline(cfg, {key: True})
    findings = _evaluate_health(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.device == "core-vms-1"
    assert f.dedup_extra == "core-vms-1:/:disk"
    assert "91" in f.summary and "85" in f.summary
    # persisting breach stays silent
    assert _evaluate_health(cfg, now=NOW) == []


def test_probe_dark_vm_never_health_alerts(tmp_path: Path) -> None:
    """FR-008/FR-016: a collector whose probe died has no fresh sample — the health pass
    skips it (030 is also excluded from adopting it, pinned separately)."""

    cfg = _cfg(tmp_path)
    key = "core-vms-1|linux:diskUsedPercent|/"
    _seed(tmp_path, [_row("core-vms-1", "linux:diskUsedPercent", 910, age_s=1500,
                          ifindex="/", ifname="/", profile="linux-health")])
    _baseline(cfg, {key: True})
    assert _evaluate_health(cfg, now=NOW) == []


def test_135_maintenance_device_skipped(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = _cfg(tmp_path)
    cfg.maintenance = MaintenanceSettings(sites=("chestnut",))
    _seed(tmp_path, [_hot_board()])
    _baseline(cfg, {_HOT: True})
    assert _evaluate_health(cfg, now=NOW) == []
