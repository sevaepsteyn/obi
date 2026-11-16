"""Fleet health assembly and evaluation (feature 096).

Assembles the newest reading of every health series — the RouterOS ``health:``/``optics:``
SNMP profiles and the Linux collector ``linux:`` probe — into per-device summaries with
honest staleness, ordered worst-first. Shared by the CLI, the watch pass, and the web
route so the surfaces can never disagree (plan: Layered Architecture). Pure functions over
a read-only store connection; absence is absence (a sensor a device does not expose simply
has no series here — nothing is fabricated, FR-003); no LLM anywhere.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from obi.core import metricstore

#: The store metric-name prefixes that constitute "health" (data-model.md §1). RouterOS
#: CPU load and memory ride the existing ``system`` profile (FR-001), so those exact
#: metric names join the three 096 prefixes — a prefix that IS the full name matches only
#: that series.
HEALTH_METRIC_PREFIXES: tuple[str, ...] = (
    "health:",
    "optics:",
    "linux:",
    "cpuLoad",
    "memUsedKb",
    "memTotalKb",
)

#: Display surfaces show a recently-dark device's last readings (marked stale) for this
#: long before it drops to "no data" — one day, matching the store's freshest useful past.
DEFAULT_LOOKBACK_S = 24 * 3600

#: A device is displayed stale after this many seconds without a new sample: six missed
#: 5-minute collection cycles. (The watch pass has its own, cadence-derived freshness gate.)
DEFAULT_STALE_AFTER_S = 1800


@dataclass(frozen=True)
class HealthReading:
    """One series' newest reading, display-scaled (value agrees with ``unit``)."""

    metric: str
    instance: str
    label: str | None
    value: float
    unit: str
    ts_us: int


@dataclass(frozen=True)
class DeviceHealth:
    """One device's newest health readings plus display staleness."""

    device: str
    kind: str  # "routeros" | "linux"
    newest_us: int
    stale: bool
    address: str | None = None  # the newest sample's address (row-click drill-down)
    readings: list[HealthReading] = field(default_factory=list)


def fleet_status(
    conn: sqlite3.Connection,
    *,
    now_us: int,
    lookback_s: int = DEFAULT_LOOKBACK_S,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    device: str | None = None,
) -> list[DeviceHealth]:
    """Every device's newest health readings within the lookback window, worst-first.

    US1 ordering is staleness/presence (stale devices float to the top, then by name);
    US2's threshold evaluation refines "worst". A device with no sample in the window does
    not appear — surfaces render that as absence, never as a healthy row.
    """

    latest = metricstore.latest_samples(
        conn,
        prefixes=HEALTH_METRIC_PREFIXES,
        since_us=now_us - lookback_s * 1_000_000,
        device=device,
    )
    by_device: dict[str, list[metricstore.LatestSample]] = {}
    for sample in latest:
        by_device.setdefault(sample.device, []).append(sample)

    devices: list[DeviceHealth] = []
    for name in sorted(by_device):
        samples = by_device[name]
        newest_us = max(s.ts_us for s in samples)
        address = next((s.address for s in samples if s.ts_us == newest_us), None)
        kind = "linux" if all(s.metric.startswith("linux:") for s in samples) else "routeros"
        readings = [
            HealthReading(
                metric=s.metric,
                instance=s.ifindex,
                label=s.ifname,
                value=metricstore.display_value(s.metric, s.value),
                unit=metricstore.display_unit(s.metric),
                ts_us=s.ts_us,
            )
            for s in samples
        ]
        devices.append(
            DeviceHealth(
                device=name,
                kind=kind,
                newest_us=newest_us,
                stale=(now_us - newest_us) > stale_after_s * 1_000_000,
                address=address,
                readings=readings,
            )
        )
    devices.sort(key=lambda d: (not d.stale, d.device))
    return devices


# --- threshold evaluation (US2: the watch pass and the web route share these) ------------------

#: Breach classes that must hold across consecutive samples before alerting (FR-014).
SUSTAINED_CLASSES: tuple[str, ...] = ("cpu", "memory")


@dataclass(frozen=True)
class HealthThresholds:
    """The deterministic decision inputs (contracts/watch-health.md). Config-sourced; the
    optics floor is the fallback that governs in v1 (vendor DDM levels are not exposed by
    the platform — research D3)."""

    temperature_max_c: float = 75.0
    sfp_temperature_max_c: float = 70.0
    cpu_max_percent: float = 90.0
    memory_max_percent: float = 90.0
    sustained_samples: int = 3
    disk_max_percent: float = 85.0
    rx_power_min_dbm: float = -24.0
    status_healthy_value: int = 0


@dataclass(frozen=True)
class Breach:
    """One reading outside its healthy range (display-scaled value; human-readable limit)."""

    device: str
    metric: str
    instance: str
    label: str | None
    klass: str  # temperature | status | fan | optics | cpu | memory | disk
    value: float
    unit: str
    limit: str
    series_key: str  # f"{device}|{metric}|{instance}" — the baseline domain key


def thresholds_from_settings(settings: object | None) -> HealthThresholds:
    """Map ``[watch.health]`` settings (or ``None`` ⇒ shipped defaults) to thresholds.

    Duck-typed so this pure module needs no config import; the CLI, the watch pass, and
    the web route all call THIS — one mapping, no drift."""

    if settings is None:
        return HealthThresholds()
    return HealthThresholds(
        temperature_max_c=getattr(settings, "temperature_max_c", 75.0),
        sfp_temperature_max_c=getattr(settings, "sfp_temperature_max_c", 70.0),
        cpu_max_percent=getattr(settings, "cpu_max_percent", 90.0),
        memory_max_percent=getattr(settings, "memory_max_percent", 90.0),
        sustained_samples=getattr(settings, "sustained_samples", 3),
        disk_max_percent=getattr(settings, "disk_max_percent", 85.0),
        rx_power_min_dbm=getattr(settings, "rx_power_min_dbm", -24.0),
        status_healthy_value=getattr(settings, "status_healthy_value", 0),
    )


def series_key(device: str, metric: str, instance: str) -> str:
    """The baseline key one evaluated series is remembered under (data-model.md §3)."""

    return f"{device}|{metric}|{instance}"


def sustained(values: list[float], *, limit: float, n: int) -> bool:
    """FR-014: True iff the newest ``n`` samples all exceed ``limit`` (newest first).

    Thin history (fewer than ``n`` samples) never alerts — a spike between gaps cannot
    fake persistence."""

    return len(values) >= n and all(v > limit for v in values[:n])


def _breach(
    dev: DeviceHealth, r: HealthReading, klass: str, limit: str,
    *, value: float | None = None, unit: str | None = None,
) -> Breach:
    return Breach(
        device=dev.device,
        metric=r.metric,
        instance=r.instance,
        label=r.label,
        klass=klass,
        value=value if value is not None else r.value,
        unit=unit if unit is not None else r.unit,
        limit=limit,
        series_key=series_key(dev.device, r.metric, r.instance),
    )


def evaluate_device(
    dev: DeviceHealth, t: HealthThresholds
) -> tuple[list[Breach], list[str]]:
    """Evaluate one device's newest readings against the thresholds.

    Returns ``(breaches, tracked_series_keys)`` — ``tracked`` is every series a rule
    LOOKED AT (the transition baseline's domain), breached or not. Metrics with no rule
    (voltages, consumption, totals) are neither breached nor tracked. Pure; the sustained
    gate for :data:`SUSTAINED_CLASSES` is the caller's job (it needs store history)."""

    breaches: list[Breach] = []
    tracked: list[str] = []
    mem_total = next((r.value for r in dev.readings if r.metric == "memTotalKb"), None)

    for r in dev.readings:
        key = series_key(dev.device, r.metric, r.instance)
        if r.unit == "°C":
            tight = r.metric == "optics:temperature" or (
                r.metric.startswith("health:") and "sfp" in r.metric
            )
            limit = t.sfp_temperature_max_c if tight else t.temperature_max_c
            tracked.append(key)
            if r.value > limit:
                breaches.append(_breach(dev, r, "temperature", f"> {limit:g} °C"))
        elif r.metric.startswith("health:") and r.metric.endswith("-state"):
            tracked.append(key)
            if r.value != t.status_healthy_value:
                breaches.append(_breach(dev, r, "status", f"≠ {t.status_healthy_value}"))
        elif r.metric.startswith("health:") and r.metric.endswith("-speed"):
            tracked.append(key)
            if r.value == 0:
                breaches.append(_breach(dev, r, "fan", "= 0 rpm (stopped)"))
        elif r.metric == "optics:rxPowerMdbm":
            tracked.append(key)
            if r.value < t.rx_power_min_dbm:
                breaches.append(_breach(dev, r, "optics", f"< {t.rx_power_min_dbm:g} dBm"))
        elif r.metric in ("optics:rxLoss", "optics:txFault"):
            tracked.append(key)
            if r.value != 0:
                flag = "rx-loss" if r.metric == "optics:rxLoss" else "tx-fault"
                breaches.append(_breach(dev, r, "optics", f"{flag} asserted"))
        elif r.metric in ("cpuLoad", "linux:cpuUtilPercent"):
            tracked.append(key)
            if r.value > t.cpu_max_percent:
                breaches.append(_breach(dev, r, "cpu", f"> {t.cpu_max_percent:g} %"))
        elif r.metric == "linux:memUsedPercent":
            tracked.append(key)
            if r.value > t.memory_max_percent:
                breaches.append(_breach(dev, r, "memory", f"> {t.memory_max_percent:g} %"))
        elif r.metric == "memUsedKb":
            if not mem_total:
                continue  # no total ⇒ no ratio ⇒ nothing evaluable (absence stays absence)
            tracked.append(key)
            percent = round(100.0 * r.value / mem_total, 1)
            if percent > t.memory_max_percent:
                breaches.append(
                    _breach(dev, r, "memory", f"> {t.memory_max_percent:g} %",
                            value=percent, unit="%")
                )
        elif r.metric == "linux:diskUsedPercent":
            tracked.append(key)
            if r.value > t.disk_max_percent:
                breaches.append(_breach(dev, r, "disk", f"> {t.disk_max_percent:g} %"))
    return breaches, tracked


@dataclass(frozen=True)
class DeviceSummary:
    """The compact per-device fleet-row facts (each ``None`` when the device exposes no
    series of that class — absence stays absence, FR-003)."""

    temperature_c: float | None
    cpu_percent: float | None
    memory_percent: float | None
    rx_power_min_dbm: float | None
    disk_max_percent: float | None
    state_alerts: list[str]  # names of *-state sensors reading a non-zero (non-ok) value


def summarize(dev: DeviceHealth) -> DeviceSummary:
    """Reduce a device's readings to the fleet row: hottest temperature, CPU %, memory %,
    worst optics rx power, fullest disk, and any status sensors off their healthy value
    (0 = ok on live evidence — research D2; US2's thresholds make this configurable)."""

    temps = [r.value for r in dev.readings if r.unit == "°C"]
    cpu = next(
        (
            r.value
            for r in dev.readings
            if r.metric in ("cpuLoad", "linux:cpuUtilPercent")
        ),
        None,
    )
    memory: float | None = next(
        (r.value for r in dev.readings if r.metric == "linux:memUsedPercent"), None
    )
    if memory is None:
        used = next((r.value for r in dev.readings if r.metric == "memUsedKb"), None)
        total = next((r.value for r in dev.readings if r.metric == "memTotalKb"), None)
        if used is not None and total:
            memory = round(100.0 * used / total, 1)
    rx = [r.value for r in dev.readings if r.metric == "optics:rxPowerMdbm"]
    disks = [r.value for r in dev.readings if r.metric == "linux:diskUsedPercent"]
    states = [
        r.label or r.metric.removeprefix("health:")
        for r in dev.readings
        if r.metric.startswith("health:")
        and r.metric.endswith("-state")
        and r.value != 0
    ]
    return DeviceSummary(
        temperature_c=max(temps) if temps else None,
        cpu_percent=cpu,
        memory_percent=memory,
        rx_power_min_dbm=min(rx) if rx else None,
        disk_max_percent=max(disks) if disks else None,
        state_alerts=states,
    )
