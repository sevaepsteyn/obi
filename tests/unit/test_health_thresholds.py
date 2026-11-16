"""Unit (feature 096): the pure threshold/evaluation functions in ``core/health.py``.

Every alert class from contracts/watch-health.md, the status-sensor healthy-value
semantics pinned from the live T006 fixture (0 = ok, overridable), the sustained-samples
gate, and the tracked-series baseline domain. Shared by the watch pass and the web route —
these tests are the single place the decision rules are pinned.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.health import (
    Breach,
    DeviceHealth,
    HealthReading,
    HealthThresholds,
    evaluate_device,
    sustained,
)

T = HealthThresholds()


def _reading(metric: str, value: float, *, unit: str = "", instance: str = "1",
             label: str | None = None) -> HealthReading:
    return HealthReading(metric=metric, instance=instance, label=label, value=value,
                        unit=unit, ts_us=0)


def _dev(*readings: HealthReading, device: str = "chestnut-gw") -> DeviceHealth:
    return DeviceHealth(device=device, kind="routeros", newest_us=0, stale=False,
                        readings=list(readings))


def _classes(breaches: list[Breach]) -> set[str]:
    return {b.klass for b in breaches}


def test_board_temperature_over_limit_breaches() -> None:
    dev = _dev(_reading("health:board-temperature2", 81.0, unit="°C",
                        label="board-temperature2"))
    breaches, tracked = evaluate_device(dev, T)
    assert _classes(breaches) == {"temperature"}
    assert breaches[0].value == 81.0
    assert "75" in breaches[0].limit
    assert tracked == [breaches[0].series_key]


def test_temperature_under_limit_is_healthy_but_tracked() -> None:
    dev = _dev(_reading("health:cpu-temperature", 45.0, unit="°C", label="cpu-temperature"))
    breaches, tracked = evaluate_device(dev, T)
    assert breaches == []
    assert len(tracked) == 1


def test_sfp_temperatures_use_the_tighter_limit() -> None:
    hot_sfp = _dev(_reading("optics:temperature", 72.0, unit="°C", label="sfp-sfpplus1"))
    breaches, _ = evaluate_device(hot_sfp, T)
    assert _classes(breaches) == {"temperature"}  # 72 > sfp limit 70, under board limit 75
    device_sfp_sensor = _dev(
        _reading("health:sfp-temperature", 72.0, unit="°C", label="sfp-temperature")
    )
    breaches, _ = evaluate_device(device_sfp_sensor, T)
    assert _classes(breaches) == {"temperature"}


def test_status_sensor_semantics_pinned_from_live_fixture() -> None:
    """The T006 CCR2004 fixture recorded psu/fan state sensors reading 0 on a healthy,
    fan-spinning, mains-powered device — 0 is ok (research D2), and the healthy value is
    overridable for platforms that disagree."""

    fixture = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "snmp" / "health-ccr2004.json")
        .read_text()
    )
    raw = fixture["walks"]["1.3.6.1.4.1.14988.1.1.3.100.1"]
    psu1 = float(raw["3.7401"])
    dev = _dev(_reading("health:psu1-state", psu1, label="psu1-state"))
    breaches, _ = evaluate_device(dev, T)
    assert breaches == []  # the live healthy reading does not breach the default

    failed = _dev(_reading("health:psu2-state", psu1 + 1, label="psu2-state"))
    breaches, _ = evaluate_device(failed, T)
    assert _classes(breaches) == {"status"}

    flipped = evaluate_device(dev, HealthThresholds(status_healthy_value=int(psu1) + 1))
    assert _classes(flipped[0]) == {"status"}  # override makes the live value a breach


def test_fan_speed_zero_breaches_nonzero_does_not() -> None:
    stopped = _dev(_reading("health:fan1-speed", 0.0, unit="rpm", label="fan1-speed"))
    breaches, _ = evaluate_device(stopped, T)
    assert _classes(breaches) == {"fan"}
    spinning = _dev(_reading("health:fan1-speed", 3870.0, unit="rpm", label="fan1-speed"))
    assert evaluate_device(spinning, T)[0] == []


def test_rx_power_floor_and_flags() -> None:
    dim = _dev(_reading("optics:rxPowerMdbm", -25.5, unit="dBm", label="sfp-sfpplus1"))
    breaches, _ = evaluate_device(dim, T)
    assert _classes(breaches) == {"optics"}
    fine = _dev(_reading("optics:rxPowerMdbm", -6.0, unit="dBm", label="sfp-sfpplus1"))
    assert evaluate_device(fine, T)[0] == []
    loss = _dev(_reading("optics:rxLoss", 1.0, label="sfp-sfpplus1"))
    assert _classes(evaluate_device(loss, T)[0]) == {"optics"}


def test_cpu_over_limit_breaches_for_both_fleets() -> None:
    ros = _dev(_reading("cpuLoad", 95.0, unit="%"))
    assert _classes(evaluate_device(ros, T)[0]) == {"cpu"}
    vm = _dev(_reading("linux:cpuUtilPercent", 97.0, unit="%"), device="core-vms-1")
    assert _classes(evaluate_device(vm, T)[0]) == {"cpu"}


def test_memory_ratio_for_routeros_and_direct_percent_for_linux() -> None:
    ros = _dev(
        _reading("memUsedKb", 950_000.0, unit="kB"),
        _reading("memTotalKb", 1_000_000.0, unit="kB"),
    )
    breaches, tracked = evaluate_device(ros, T)
    assert _classes(breaches) == {"memory"}
    assert breaches[0].value == 95.0 and breaches[0].unit == "%"
    vm = _dev(_reading("linux:memUsedPercent", 93.0, unit="%"), device="core-vms-1")
    assert _classes(evaluate_device(vm, T)[0]) == {"memory"}
    healthy = _dev(
        _reading("memUsedKb", 300_000.0, unit="kB"),
        _reading("memTotalKb", 1_000_000.0, unit="kB"),
    )
    assert evaluate_device(healthy, T)[0] == []


def test_disk_per_filesystem() -> None:
    vm = _dev(
        _reading("linux:diskUsedPercent", 91.0, unit="%", instance="/", label="/"),
        _reading("linux:diskUsedPercent", 40.0, unit="%", instance="/var", label="/var"),
        device="core-vms-1",
    )
    breaches, tracked = evaluate_device(vm, T)
    assert [b.instance for b in breaches] == ["/"]
    assert len(tracked) == 2  # both filesystems are baseline-tracked


def test_untracked_metrics_never_breach_or_track() -> None:
    dev = _dev(
        _reading("optics:supplyVoltageMv", 3225.0, unit="mV", label="sfp-sfpplus1"),
        _reading("health:2pin-voltage", 54.5, unit="V", label="2pin-voltage"),
        _reading("memTotalKb", 1_000_000.0, unit="kB"),
    )
    breaches, tracked = evaluate_device(dev, T)
    assert breaches == [] and tracked == []


def test_sustained_requires_n_consecutive_breaching_samples() -> None:
    assert sustained([95.0, 96.0, 94.0], limit=90.0, n=3) is True
    assert sustained([95.0, 80.0, 96.0], limit=90.0, n=3) is False  # 2 of 3 hot ⇒ not yet
    assert sustained([95.0, 96.0], limit=90.0, n=3) is False  # thin history never alerts
    assert sustained([], limit=90.0, n=3) is False
