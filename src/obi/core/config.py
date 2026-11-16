"""Load local TOML config: discovery scope + connection settings + plaintext credentials.

Credentials are stored in plaintext per the operator's choice (see research.md §3); the
safeguards are file permissions (``0600``), a git-ignored path, and a least-privilege
read-only RouterOS user. Credentials are wrapped in :class:`Secret` so they never appear in
``repr`` output or logs, and are never written to the audit trail.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from obi.core.models import DiscoveryScope

# 121: config is the one place that knows about BOTH [radio] and [meshtastic], so the
# cross-subsystem serial-port exclusion is resolved here (see the excluded_ports field).
# radio.devices imports nothing from obi, so this adds no cycle — and it keeps the mesh
# driver free of any radio import (contracts/mesh-port-safety.md G7).
from obi.radio import devices as radio_devices


class ConfigError(Exception):
    """Raised for usage/config errors (maps to CLI exit code 2)."""


class Secret:
    """A string that refuses to reveal itself in ``repr``/``str`` (no accidental logging)."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the plaintext value. Call only at the moment of use, never to log."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass
class RouterosSettings:
    """Connection settings for the Mikrotik RouterOS API."""

    username: str
    password: Secret
    use_tls: bool = True
    api_port: int | None = None  # defaults to 8729 (TLS) / 8728 (plain) when unset

    @property
    def port(self) -> int:
        if self.api_port is not None:
            return self.api_port
        return 8729 if self.use_tls else 8728


@dataclass
class RouterosLiveSettings:
    """Bounds for the live RouterOS query/ping/traceroute tools (feature 028).

    Parsed from ``[routeros.live]``; every field has a **safe default**, so the tools work with no
    config beyond the existing ``[routeros]`` section (they gate on the Mikrotik driver). This table
    only *tunes* the caps — there is no ``enabled`` flag. The model may request a smaller
    ``count``/``limit`` but the handlers clamp to these maxima (FR-012); ``max_records``/
    ``max_output_chars`` keep a large read from overflowing the model context (FR-010).
    """

    max_records: int = 100
    max_output_chars: int = 8000
    ping_count: int = 4
    ping_count_max: int = 20
    # Backstops sized for a full INTERNET traceroute: real paths run ~15–30 hops and unresponsive
    # (`*`) hops each burn a per-probe timeout, so the old 20 s / 48-probe caps cut off short of a
    # public destination (e.g. `traceroute google.com` stopped ~hop 12). A reachable target still
    # returns as soon as it is hit; the deadline only bounds a black-hole tail.
    traceroute_max_probes: int = 64
    traceroute_deadline_s: float = 45.0
    timeout_s: float | None = None  # None ⇒ inherit the run-level [connection] timeout


@dataclass
class UispSettings:
    """Connection settings for a UISP controller's REST API (feature 003).

    The token is wrapped in :class:`Secret` so it never appears in logs/audit. ``timeout``
    defaults higher than the per-device Mikrotik timeout because one read covers the whole
    fleet. ``name_filter`` is an optional default device-name substring filter (FR-016).

    Feature 019 adds optional log-ingestion settings: ``ingest_events`` / ``ingest_outages`` pull
    the controller's event log (``GET /logs``) and device outage records (``GET /outages``) into the
    feature-013 store. They default off, so a status-only feature-003 ``[uisp]`` config is unchanged
    (``ingest_enabled`` is false). ``min_level`` optionally drops events below a level
    (``info``/``warning``/``error``); ``first_run_lookback`` is the events first-run backfill window
    **and** the outages per-cycle rescan window (outages have no incremental cursor — see
    specs/019-uisp-logs/research.md §2).

    Feature 035 adds ``ignore_names``: the exclude companion to ``name_filter``. Case-insensitive
    name substrings marking UISP entities as OUT OF SCOPE for alerting and listing — the watcher's
    UISP log/outage pass and link pass, and the ``obi uisp links``/``obi uisp status``/
    ``uisp_links`` surfaces all drop matching entities. It never gates ingest: logs keep
    collecting the excluded events (queryable via ``obi logs query``). Empty ⇒ excludes nothing.
    """

    base_url: str
    token: Secret
    verify_tls: bool = True
    timeout: float = 15.0
    name_filter: str | None = None
    ignore_names: list[str] = field(default_factory=list)
    ingest_events: bool = False
    ingest_outages: bool = False
    min_level: str | None = None
    first_run_lookback: str = "30d"
    # 079: geometry export site-anchoring radius (meters). When a UISP device pin has strayed
    # farther than this from its site pin (a stale copy-at-placement value on a GPS-less radio),
    # the exported link endpoint is placed at the site pin instead. 0 or negative disables the
    # correction (always trust device pins). Lives in [uisp] because the quirk is UISP-specific.
    location_sanity_m: float = 75.0

    @property
    def ingest_enabled(self) -> bool:
        """Whether UISP log ingestion (feature 019) is configured (events and/or outages)."""

        return self.ingest_events or self.ingest_outages


@dataclass
class NetboxSettings:
    """Connection settings for a NetBox instance's REST API (feature 007).

    NetBox is a **source of truth** (DCIM) — obi reads its documented inventory, read-only. The
    token is wrapped in :class:`Secret` so it never appears in logs/audit. ``verify_tls`` defaults
    on (only meaningful for an ``https://`` instance). ``name_filter`` is an optional default
    device-name substring filter (FR-016).
    """

    base_url: str
    token: Secret
    verify_tls: bool = True
    timeout: float = 15.0
    name_filter: str | None = None


def default_logs_mirror_root() -> Path:
    """XDG state location for the rsync-populated syslog mirror (feature 013).

    ``$XDG_STATE_HOME/obi/logs`` (``~/.local/state`` default), beside the audit log. obi only
    READS this tree; a scheduled rsync (see the quickstart) keeps it fresh.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "logs"


def default_logs_db_path() -> Path:
    """XDG state location for the ingested syslog SQLite store: ``$XDG_STATE_HOME/obi/logs.db``."""

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "logs.db"


def default_metrics_db_path() -> Path:
    """XDG state location for the SNMP time-series store (feature 021): ``…/obi/metrics.db``.

    Deliberately a **separate** database from :func:`default_logs_db_path` — the two stores have
    unrelated schemas, write rates, and retention policies (logs never prune; metrics self-prune).
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "metrics.db"


def default_icmp_db_path() -> Path:
    """XDG state location for the ICMP reachability state store (feature 072): ``…/obi/icmp.db``.

    A **separate**, tiny database from the metrics/logs stores — it holds one current-state row per
    device (up/down + last-seen), rewritten each collector cycle; no time-series.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "icmp.db"


def default_weather_db_path() -> Path:
    """XDG state location for the BRC weather snapshot store (feature 110): ``…/obi/weather.db``.

    A **separate** database — hourly forecast/alert snapshots with their own season-scoped
    retention; nothing device-keyed, so it shares no schema with the metrics/logs stores.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "weather.db"


def default_traffic_db_path() -> Path:
    """XDG state location for the BRC gate-traffic store (feature 112): ``…/obi/traffic.db``.

    A **separate** database — Travel Time Log entries, gate statuses, and poll records with
    their own season-scoped retention; nothing device-keyed, so it shares no schema with the
    metrics/logs stores.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "traffic.db"


def default_radio_db_path() -> Path:
    """XDG state location for the radio transcript store (feature 088): ``…/obi/radio.db``.

    A **separate** database — transmissions/calls/outbound have their own schema and the
    keep-everything season retention (like logs, unlike metrics/audit which prune).
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "radio.db"


def default_radio_clips_dir() -> Path:
    """XDG state location for retained per-transmission audio clips (feature 088).

    ``…/obi/radio/clips`` — 16 kHz mono PCM16 WAVs beside the radio store, laid out
    ``YYYY/MM/DD/<id>.wav``. Channel audio is retained locally by operator decision and
    never leaves the host (constitution 1.12.0).
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "radio" / "clips"


def default_config_repo_dir() -> Path:
    """XDG state location for the config-backup git repo (feature 026): ``…/obi/config-repo``.

    A git working tree (not a database) — configs are versioned text and git natively gives history,
    diff, and as-of retrieval. Beside the audit log and the other stores.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "config-repo"


@dataclass
class LogsSettings:
    """Local syslog store settings (feature 013).

    ``mirror_root`` is the rsync-populated tree obi PARSES (read-only); ``db_path`` is the SQLite
    store obi ingests into and queries. obi never performs the sync — a scheduled rsync keeps the
    mirror fresh, exactly as the Slack listener leaves daemonizing to the operator.
    ``log_tz`` is the IANA timezone the collectors' RouterOS clocks use (BSD syslog lines carry no
    offset); query results normalize to it. ``collectors`` is documentation only — it names the
    syslog-ng hosts for the sync helper and is ignored at runtime.
    """

    mirror_root: Path = field(default_factory=default_logs_mirror_root)
    db_path: Path = field(default_factory=default_logs_db_path)
    default_limit: int = 200
    max_limit: int = 2000
    timeout: float = 5.0
    log_tz: str = "UTC"
    collectors: list[str] = field(default_factory=list)

    def tzinfo(self) -> tzinfo:
        """Resolve ``log_tz`` to a :class:`~datetime.tzinfo`, falling back to UTC if unknown."""

        try:
            return ZoneInfo(self.log_tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return UTC


def default_watch_dir() -> Path:
    """XDG state location for the autonomous watcher's state + reports (feature 014).

    ``$XDG_STATE_HOME/obi/watch`` (``~/.local/state`` default), beside the audit log and the
    syslog store. Holds ``state.json`` (cursor + last run) and ``reports/`` (per-finding files).
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "watch"


@dataclass
class WatchMetricsSettings:
    """Optional metrics-watch settings (feature 027), parsed from ``[watch.metrics]``.

    Presence + ``enabled`` opts the metrics-evaluation pass into ``obi watch run``. Detection is a
    deterministic **offline** rule — a watched error counter's increase over ``window_seconds``
    exceeding ``threshold`` (default 0 = any new error) — so it needs no cloud model. Reads the
    feature-021 metrics store at ``db_path``, read-only.
    """

    enabled: bool = False
    threshold: int = 0
    window_seconds: int = 3600
    counters: list[str] = field(default_factory=lambda: ["ifInErrors", "ifOutErrors"])
    # Interface-name substrings to skip (case-insensitive). Defaults to WireGuard tunnels, whose
    # out-errors are typically benign (no-route-to-peer drops), not a failing physical link.
    ignore_interfaces: list[str] = field(default_factory=lambda: ["wireguard"])
    db_path: Path = field(default_factory=default_metrics_db_path)


@dataclass
class WatchReachabilitySettings:
    """Optional device-reachability settings (feature 030), parsed from ``[watch.reachability]``.

    Presence + ``enabled`` opts the device-reachability pass into ``obi watch run``. Detection is a
    deterministic **offline** rule — a device whose newest reading in the feature-021 metrics store
    has aged past ``stale_after_cycles`` × its observed cadence has stopped responding to SNMP — so
    it needs no cloud model. A single missed cycle is always tolerated (the blip safeguard). When at
    least ``mass_outage_ratio`` of the watched devices are stale at once (collector/host stopped,
    not one device), the pass coalesces them into a single finding. Reads the metrics store at
    ``db_path``, read-only.
    """

    enabled: bool = False
    stale_after_cycles: int = 2
    mass_outage_ratio: float = 0.80
    ignore_devices: list[str] = field(default_factory=list)
    db_path: Path = field(default_factory=default_metrics_db_path)


@dataclass
class IcmpSettings:
    """Optional ICMP reachability collector settings (feature 072), parsed from ``[icmp]``.

    Presence is optional — ``obi icmp run`` works with all-defaults. The collector pings every
    ``present_in_latest`` device in the discovery inventory (``Config.inventory_path``) plus, when
    ``include_siklu``, the ``[siklu]`` hosts, **from the obi host** (system ``ping``, no
    credentials), and writes only current up/down state to ``db_path``. A device is declared down
    after ``down_after_cycles`` consecutive missed cycles (a cycle is a miss only when all
    ``pings_per_cycle`` probes are lost). ``ignore_devices`` are case-insensitive substrings
    excluded from probing (matched on identity or address).
    """

    db_path: Path = field(default_factory=default_icmp_db_path)
    pings_per_cycle: int = 3
    ping_timeout_ms: int = 1000
    deadline_s: float = 5.0
    concurrency: int = 16
    down_after_cycles: int = 1
    include_siklu: bool = True
    ignore_devices: list[str] = field(default_factory=list)
    # Hand-listed extra hosts to probe beyond the discovered RouterOS roster + [siklu] radios —
    # e.g. the Linux collector/NetBox VMs (feature 072). DNS name or IP; the short name is derived
    # by stripping the domain (like a Siklu host), the full value stays the ping address.
    extra_hosts: list[str] = field(default_factory=list)
    # feature 095: also probe the UISP backhaul radios (Wave, `-ptp-` names) derived from the web
    # snapshot's uisp.json — no live controller call. The derived roster is cached beside the
    # snapshot consumer's state so a missing snapshot NEVER shrinks the probe set (finding U1).
    include_uisp_ptp: bool = True


@dataclass
class BrcSettings:
    """The event city plan (feature 098/127 geometry, made config by feature 137), ``[brc]``.

    Presence enables the city grid (XR layer) and clock-and-distance address features
    (``playa_locate``, mesh answers). ``origin_lat``/``origin_lon`` are the plan's survey origin;
    ``year`` selects the plan edition; ``source_url`` (``{year}`` substituted) is the ONLY place
    the plan is fetched from — unset ⇒ the local cache only; ``az12_deg`` is the fallback azimuth
    of the 12:00 axis when the cached plan lacks one.
    """

    origin_lat: float
    origin_lon: float
    year: int
    source_url: str | None = None
    az12_deg: float = 45.0

    @property
    def cache_name(self) -> str:
        return f"brc-{self.year}"


@dataclass
class PlayaSettings:
    """The event's public camp/art API (feature 127), ``[playa]``; ``base_url`` required."""

    base_url: str


@dataclass
class SiteSettings:
    """Deployment-wide site facts (feature 137), ``[site]``. ``timezone`` is the IANA zone used
    for local-time labels (weather, traffic, tool answers); default UTC."""

    timezone: str = "UTC"


@dataclass
class WeatherSettings:
    """Optional BRC weather collector settings (feature 110), parsed from ``[weather]``.

    Absent ⇒ weather is OFF (feature 137: no built-in location). Present, it needs ``latitude``
    and ``longitude`` — the collector fetches that point's current conditions + a 24-hour hourly
    forecast (Open-Meteo, keyless) and, when ``nws_zone`` is set, the active NWS alerts for it,
    snapshotting both into ``db_path`` hourly; the ``weather_forecast`` chat tool reads only that
    store. ``place_name`` is how prompts name the location ("the configured location" if unset).
    """

    latitude: float
    longitude: float
    db_path: Path = field(default_factory=default_weather_db_path)
    nws_zone: str | None = None
    place_name: str | None = None
    timeout_s: float = 20.0
    retention_days: int = 120


@dataclass
class TrafficSettings:
    """Optional BRC gate-traffic collector settings (feature 112), parsed from ``[traffic]``.

    Absent ⇒ traffic is OFF (feature 137). Present, it needs ``base_url``: the event dashboard
    whose Travel Time Log feed and gate status the collector polls every 5 minutes into
    ``db_path``; the ``gate_traffic`` chat tool reads only that store. ``base_url`` is the ONE
    host the fetcher may contact (spec FR-009 — no caller anywhere supplies an address).
    """

    base_url: str
    db_path: Path = field(default_factory=default_traffic_db_path)
    timeout_s: float = 20.0
    retention_days: int = 120


@dataclass
class WatchIcmpSettings:
    """Optional ICMP-reachability watch settings (feature 072), parsed from ``[watch.icmp]``.

    Presence + ``enabled`` opts the ICMP down/up pass into ``obi watch run``. Detection is a
    deterministic **offline** rule — no cloud model: a device that was ``up`` at the held baseline
    and is ``down`` in the feature-072 state store (populated by the ``*/5`` ``obi icmp run``
    collector) transitioned down and raises a finding. The first run seeds the baseline silently and
    recovery is a silent clear (both mirror feature 030). When at least ``mass_outage_ratio`` of the
    watched devices transition down in the same run (host-side / site-wide outage, not one device),
    the pass coalesces them into a single finding. Reads the state store at ``db_path`` read-only.
    """

    enabled: bool = False
    mass_outage_ratio: float = 0.80
    severity: str = "warning"
    db_path: Path = field(default_factory=default_icmp_db_path)


@dataclass
class HealthLinuxSettings:
    """Optional Linux collector-VM health probe settings (feature 096), parsed from
    ``[health.linux]``.

    Presence (with a non-empty ``hosts`` list) activates ``obi health probe``: a read-only SSH
    command probe (static allowlist — /proc/stat, /proc/meminfo, df, hwmon) of each collector
    over the operator's EXISTING key trust (the log-shipping SSH identity named by ``username``;
    obi's config holds no secret for this driver). Readings land in the feature-021 metrics
    store at ``db_path`` as ``linux:*`` series. A capability section (like ``[icmp]``) — it does
    NOT satisfy the ">=1 source" gate.
    """

    username: str
    hosts: list[str] = field(default_factory=list)
    timeout: float = 15.0
    concurrency: int = 5
    # None ⇒ follow the metrics store ([snmp.history].db_path, or the default location) so
    # probe writes and every health read can never silently diverge.
    db_path: Path | None = None


@dataclass
class WatchUispLinksSettings:
    """Optional UISP link-watch settings (feature 034), parsed from ``[watch.uisp_links]``.

    Presence + ``enabled`` opts the wireless link pass into ``obi watch run``. Detection is a
    deterministic rule — no LLM: a link that was active and no longer is, or whose link-level
    signal fell more than ``signal_drop_db`` below the persisted baseline, raises a finding.
    Unlike the metrics/reachability passes this one reads the UISP controller live (two
    read-only GETs); an unreachable controller is a recorded degraded mode. Requires ``[uisp]``
    (validated at load). ``ignore_links`` holds case-insensitive substrings matched against
    either endpoint name or the link label.
    """

    enabled: bool = False
    signal_drop_db: float = 6.0
    ignore_links: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WatchConfigCapturesSettings:
    """Optional capture-freshness watch settings (feature 087), parsed from
    ``[watch.config_captures]``.

    Presence + ``enabled`` opts the capture-staleness pass into ``obi watch run``. Detection is
    a deterministic **offline** rule over the feature-085 capture-state sidecar: a device whose
    last SUCCESSFUL config capture was within ``stale_after`` at the held baseline and is now
    older transitioned to capture-stale (obi can no longer read it over SSH — dead device,
    rotated credential, lost ssh policy) and raises a finding. First run seeds the baseline
    silently; recovery is a silent clear; when at least ``mass_outage_ratio`` of the tracked
    devices go stale in the same run (laptop asleep, fleet-wide cred rotation) they coalesce
    into one finding. ``ignore_devices`` holds case-insensitive identity substrings. Reads only
    the sidecar + roster — no device I/O, no cloud model (sibling of 030/072).
    """

    enabled: bool = False
    stale_after: str = "1d"
    mass_outage_ratio: float = 0.80
    severity: str = "warning"
    ignore_devices: list[str] = field(default_factory=list)
    repo_path: Path = field(default_factory=default_config_repo_dir)


@dataclass
class WatchHealthSettings:
    """Optional hardware-health watch settings (feature 096), parsed from ``[watch.health]``.

    Presence + ``enabled`` opts the health pass into ``obi watch run``. Detection is a
    deterministic **offline** rule over the feature-021 metrics store's health series
    (RouterOS ``health:``/``optics:`` profiles + the Linux collector probe): a series that
    was healthy at the held baseline and now breaches its threshold transitioned unhealthy
    and raises a finding — overheat, PSU/fan status off its healthy value, optics rx power
    under the floor (config-fallback thresholds; the platform exposes no vendor DDM
    levels), sustained CPU/memory pressure (``sustained_samples`` consecutive readings),
    or a filesystem over ``disk_max_percent``. First run seeds silently; recovery is a
    silent clear; a device with no fresh sample is skipped (staleness belongs to 030/072);
    ≥ ``mass_outage_ratio`` of tracked series breaching at once coalesce into one finding.
    ``ignore_devices``/``ignore_sensors`` hold case-insensitive substrings. Reads the store
    at ``db_path`` read-only — no device I/O, no cloud model (sibling of 027/030/072/087).
    """

    enabled: bool = False
    temperature_max_c: float = 75.0
    sfp_temperature_max_c: float = 70.0
    cpu_max_percent: float = 90.0
    memory_max_percent: float = 90.0
    sustained_samples: int = 3
    disk_max_percent: float = 85.0
    rx_power_min_dbm: float = -24.0
    status_healthy_value: int = 0
    severity: str = "warning"
    mass_outage_ratio: float = 0.80
    ignore_devices: list[str] = field(default_factory=list)
    ignore_sensors: list[str] = field(default_factory=list)
    db_path: Path = field(default_factory=default_metrics_db_path)


@dataclass
class WatchConfigChangesSettings:
    """Optional config-change-watch settings (feature 070), parsed from ``[watch.config_changes]``.

    Presence + ``enabled`` opts the config-change pass into ``obi watch run``. Detection is a
    deterministic **offline** rule — a device whose stored config (the feature-026 git repo at
    ``repo_path``) advanced to a new committed version since the watcher last saw it — so it needs
    no cloud model. On first enable it seeds a per-device baseline silently (no backlog flood). Each
    finding carries the **redacted** diff (feature 026 redacts at rest), bounded to
    ``max_diff_lines`` with a pointer to ``obi configs diff`` for the full change. Read-only over
    the store; it never triggers an ingest. ``ignore_devices`` holds case-insensitive substrings
    matched against the device identity.
    """

    enabled: bool = False
    severity: str = "warning"
    max_diff_lines: int = 120
    ignore_devices: list[str] = field(default_factory=list)
    repo_path: Path = field(default_factory=default_config_repo_dir)


@dataclass
class WatchSettings:
    """Settings for the autonomous log watcher (feature 014).

    Additive and off by default: absent or ``enabled=false`` ⇒ ``obi watch run`` is a no-op.
    ``obi watch`` is a cloud-reasoning feature (Principle VI carve-out): the LLM judges each new
    log window, and a cycle with no connectivity is a clean no-op. ``model`` defaults to
    ``[chat].model`` when unset.
    """

    enabled: bool = False
    investigation_max_steps: int = 4
    # feature 062: post filter-before-LIMIT (A1) this bounds CANDIDATE events (the judged class),
    # not raw rows, so ignorable noise can no longer consume the budget. Default 500→2000 gives
    # ~7-20x headroom over the live candidate rate and guarantees ≥2000 candidates/run during
    # catch-up (research D4). ``lag_alarm_*``/``heartbeat_hours`` drive the self-monitoring (A4).
    max_window_events: int = 2000
    retention_days: int = 30
    watch_dir: Path = field(default_factory=default_watch_dir)
    model: str | None = None  # None ⇒ reuse [chat].model
    max_tokens: int = 2048
    # Alert-once dedup: suppress a finding already open for the same device/severity; it re-alerts
    # after dedup_ttl_hours of not recurring. Set dedup=false to report every cycle.
    dedup: bool = True
    dedup_ttl_hours: int = 6
    # Window filters applied BEFORE the model sees anything.
    # routeros_only: only analyze logs whose host matches a RouterOS device in the inventory
    # (by identity OR address), excluding non-RouterOS gear and unknown hosts (FR-010).
    routeros_only: bool = True
    # ignore_topics: drop events whose RouterOS topics match any pattern (token-subset match) —
    # e.g. "dhcp,info" drops events tagged both 'dhcp' and 'info'. Noisy-topic suppression.
    ignore_topics: list[str] = field(default_factory=list)
    # ignore_collectors: drop every event from these whole log collectors (feature 039), by
    # collector name — e.g. ["unifi"] removes all UniFi-sourced rows regardless of their (varied,
    # category-based) topics, which ignore_topics cannot target as a single rule. Matched
    # case-insensitively against each event's ``collectors`` list and applied before routeros_only,
    # so a collector the curated-source gate would otherwise admit (unifi/uisp) is still dropped.
    # Empty ⇒ exclude nothing.
    ignore_collectors: list[str] = field(default_factory=list)
    # Slack delivery (feature 015): push qualifying findings to the operator as a DM. Off by
    # default. ``slack_min_severity`` gates which findings are sent (info|warning|critical);
    # ``slack_user`` overrides the recipient (default: the first [slack] allowlist entry). Requires
    # a [slack] bot token; delivery uses ONLY the bot token at runtime — no app token, no listener.
    slack_notify: bool = False
    slack_min_severity: str = "warning"
    slack_user: str | None = None
    # Metrics-watch (feature 027): a deterministic offline pass alerting on climbing error counters.
    # None ⇒ the pass is inactive and log-watching is unchanged.
    metrics: WatchMetricsSettings | None = None
    # Device-reachability watch (feature 030): a deterministic offline pass alerting when a device
    # stops reporting SNMP metrics. None ⇒ the pass is inactive; other passes are unchanged.
    reachability: WatchReachabilitySettings | None = None
    # UISP link watch (feature 034): a deterministic pass alerting when a wireless link goes down
    # or its signal drops vs baseline. None ⇒ the pass is inactive; other passes are unchanged.
    uisp_links: WatchUispLinksSettings | None = None
    # Config-change watch (feature 070): a deterministic offline pass alerting when a device's
    # stored config (feature-026 git repo) changes. None ⇒ inactive; other passes are unchanged.
    config_changes: WatchConfigChangesSettings | None = None
    config_captures: WatchConfigCapturesSettings | None = None  # feature 087
    # ICMP-reachability watch (feature 072): a deterministic offline pass alerting on up→down
    # transitions in the feature-072 ICMP state store. None ⇒ inactive; other passes are unchanged.
    icmp: WatchIcmpSettings | None = None
    # None ⇒ the feature-096 hardware-health pass is inactive; other passes unchanged.
    health: WatchHealthSettings | None = None
    # Live-tool budget (feature 028): the max number of live device-contacting calls (routeros_query
    # + routeros_ping + routeros_traceroute) the autonomous investigation may issue per run. On
    # reaching it the watcher stops making live calls but still produces findings (FR-015). 0 ⇒ the
    # watcher gets no live tools (chat/voice keep them). Bounds only the watcher, not chat.
    investigation_live_call_budget: int = 8
    # Self-monitoring (feature 062, A4). Each run measures how far behind it is and DMs the
    # operator when it falls past a threshold, so silence means healthy rather than blind.
    # lag_alarm_minutes: alert when the oldest unjudged candidate (or, when nothing is being
    #   ingested at all, the newest stored event) is older than this. 0 disables the time floor.
    # lag_alarm_events: alert when more than this many candidates remain unjudged. 0 disables
    #   the count floor. Both 0 ⇒ no lag alarm.
    # heartbeat_hours: cadence of the "watcher healthy — N events, 0 findings" status DM, so
    #   no-news is confirmed-good. 0 disables heartbeats. Both need [slack] delivery enabled.
    lag_alarm_minutes: int = 30
    lag_alarm_events: int = 2000
    heartbeat_hours: int = 24


@dataclass
class UnifiNetworkSettings:
    """Connection settings for one UniFi console's Network Integration API (feature 011).

    Reads up to two per-console surfaces, whichever are configured: the official Network Integration
    API (``api_key`` → ``x-api-key``) and the undocumented controller API (``token`` → a browser
    ``TOKEN`` cookie, with CSRF derived from it). At least one of ``api_key``/``token`` is required;
    each is :class:`Secret`-wrapped. ``sites`` selects which site(s) to query — one, several, or all
    (an empty list, the default); entries match a site's name / internalReference / id.
    ``verify_tls`` defaults on (cloud certs are valid; false for a self-signed local console). The
    ``name_filter``
    is an optional default device-name substring filter (FR-011).
    """

    base_url: str
    api_key: Secret | None = None
    token: Secret | None = None
    sites: list[str] = field(default_factory=list)  # empty = all sites
    verify_tls: bool = True
    timeout: float = 15.0
    name_filter: str | None = None


@dataclass
class UnifiSiteManagerSettings:
    """Connection settings for Ubiquiti's account-level UniFi Site Manager API (feature 011).

    A single account-wide read of ``api.ui.com/v1/devices`` with an ``X-API-KEY`` — aggregates every
    site/console in the account (inventory + reachability; no per-device cpu/memory). ``api_key`` is
    required and :class:`Secret`-wrapped; ``base_url`` defaults to the public Site Manager endpoint.
    ``name_filter`` is an optional default device-name substring filter (FR-011).
    """

    api_key: Secret
    base_url: str = "https://api.ui.com"
    verify_tls: bool = True
    timeout: float = 15.0
    name_filter: str | None = None


# feature 016: UniFi controller event categories ingested by default — every category EXCEPT
# ``CLIENT_DEVICES`` (client connect/disconnect/roam churn is high-volume noise and carries
# client-identifying data, so it is excluded both for signal and privacy, FR-005).
DEFAULT_UNIFI_EVENT_CATEGORIES = [
    "UNIFI_DEVICES",
    "POWER",
    "SECURITY",
    "INTERNET_AND_WAN",
    "SOFTWARE_UPDATES",
    "UNIFI_ETHERNET_PORTS",
    "VPN",
    "AUDIT",
    "UNKNOWN",
]


@dataclass
class UnifiInternalSettings:
    """Credentials for the reverse-engineered UniFi controller API — event ingestion (feature 016).

    The official Network Integration API key (feature 011) exposes inventory/health only, not logs;
    controller events come from the same undocumented controller API authenticated by a
    console-**local** admin account (exempt from the cloud account's MFA, so it supports unattended
    re-login). ``username``/``password`` are :class:`Secret`-wrapped. ``base_url``/``sites``/
    ``verify_tls`` are the per-console coordinates shared with feature 011 (resolved from the
    ``[unifi]`` common section, falling back to ``[unifi-network]`` for back-compat). ``categories``
    defaults to :data:`DEFAULT_UNIFI_EVENT_CATEGORIES` (all but ``CLIENT_DEVICES``).
    ``retention_window`` is the first-run backfill lookback (FR-017). obi uses the credential
    strictly read-only (FR-010).
    """

    base_url: str
    username: Secret
    password: Secret
    sites: list[str] = field(default_factory=list)  # empty = all sites
    verify_tls: bool = True
    timeout: float = 15.0
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_UNIFI_EVENT_CATEGORIES))
    retention_window: str = "7d"


@dataclass
class SikluSettings:
    """Connection settings for the Siklu EtherHaul radios (feature 020).

    Siklu is a device-per-address vendor (no controller): ``hosts`` is the operator-maintained
    roster of radio addresses/hostnames obi queries over SSH. The shared Tech-level credentials are
    assumed fleet-wide; ``password`` is wrapped in :class:`Secret` so it never appears in
    logs/audit/output. ``timeout`` is the per-radio SSH login + reads budget (SSH login is slower
    than the RouterOS API, so it defaults higher). ``pairs`` is an optional list of explicit
    ``(name_a, name_b)`` pairing overrides for radios whose names do not follow the
    ``<site>-ptp-<far>-<band>`` convention. ``name_filter`` is an optional default device-name
    substring filter. An empty/missing ``hosts`` is a clean "no radios configured" (FR-010), not an
    error; a non-empty ``hosts`` without ``username``/``password`` raises a :class:`ConfigError`.
    """

    hosts: list[str] = field(default_factory=list)
    username: str = ""
    password: Secret = field(default_factory=lambda: Secret(""))
    port: int = 22
    timeout: float = 6.0
    concurrency: int = 8
    pairs: list[tuple[str, str]] | None = None
    name_filter: str | None = None


def default_web_dir() -> Path:
    """XDG state location for the web viewer's artifacts (features 022/033).

    ``$XDG_STATE_HOME/obi/ar`` (``~/.local/state`` default), beside the audit log. Holds the
    geometry export (``links.json``), the durable elevation cache (``elevations.json``), and
    the generated self-signed TLS material for ``obi web serve``. The on-disk path keeps the
    pre-033 ``ar`` segment DELIBERATELY: renaming it would invalidate the certificate every
    device has already accepted and orphan the durable elevation/DEM/GNIS caches (033 R3).
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "ar"


def default_service_dir() -> Path:
    """XDG state location for daemon-management state (feature 081): ``…/obi/service``.

    Holds the Slack listener's liveness heartbeat (and any future per-service state), beside the
    other stores under ``$XDG_STATE_HOME/obi``. The launchd plists live separately under
    ``~/Library/LaunchAgents``; the daemon logs stay at their existing ``…/obi/*.log`` paths.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "service"


@dataclass
class WebSettings:
    """Settings for the web viewer (feature 022 as ``[ar]``; renamed ``[web]`` by 033).

    Additive; every field has a default. ``amsl_tolerance`` / ``default_mast_height`` drive
    the mixed-datum height heuristic (022 research §3); ``default_ground_elevation`` is the
    EPQS-unreachable fallback (the playa, ~1191 m); ``ground_overrides`` maps a site name to
    a hand-set ground elevation that always wins over EPQS. ``degraded_link_score`` is the
    link-health threshold applied to the worse endpoint's linkScore. ``port``/``bind`` shape
    ``obi web serve`` (HTTPS — WebXR and browser mic capture require a secure context). Data
    freshness is a cron concern (029): schedule ``obi web export`` like the other ingests;
    both viewer versions re-read the export every 5 min. ``voice`` (032) mounts the 024 voice
    turn engine on this origin; **on by default** since feature 048 (the ``--voice`` CLI flag was
    removed — the web surface's LAN capabilities are simply on). It pulls in the local STT model
    and the chat runtime; a build failure degrades to a static+tools server (never a startup
    failure), and ``voice = false`` disables it for a host that wants no voice engine at all.
    """

    export_path: Path = field(default_factory=lambda: default_web_dir() / "links.json")
    amsl_tolerance: float = 150.0
    default_mast_height: float = 10.0
    default_ground_elevation: float = 0.0  # feature 137: no built-in site elevation
    degraded_link_score: float = 0.5
    ground_overrides: dict[str, float] = field(default_factory=dict)
    port: int = 8443
    bind: str = "0.0.0.0"
    # 031: states whose GNIS gazetteer names the peak labels; empty = peaks omitted
    # (named in the export summary), never an error. Live network: ["NV"].
    gnis_states: list[str] = field(default_factory=list)
    voice: bool = True  # 048: on by default (the --voice flag was removed); voice=false opts out
    # 044: merge the Siklu e-band logical links into the export when [siklu] is configured
    # (read-only SSH shows, ~9 s for the live 2-radio roster). Opt-out, not opt-in: the
    # 70 GHz toggle should have content wherever the radios are set up.
    include_siklu: bool = True
    # 086: optional blessed-layout file for the OSPF map — the operator-exported positions
    # document (widget position-store shape) overlaid on the computed layout seeds at every
    # ospf snapshot build. Unset ⇒ feature off, computed layout exactly as before.
    ospf_layout_path: Path | None = None
    # 097: declared tower height per site (meters above site ground) — every endpoint of a
    # listed site exports at ONE shared point (site position, ground + tower). Values in the
    # TOML table may be bare numbers (meters) or unit-suffixed strings ("60 ft", "18.3 m");
    # stored here in meters. Empty ⇒ feature off, per-device placement as before.
    site_towers: dict[str, float] = field(default_factory=dict)
    # 098: build the BRC city-grid artifact (brc.json) during export. Feature 137: defaults to
    # whether a [brc] section exists (the city plan is config); brc = false skips the step
    # entirely (no fetch, no write).
    brc: bool = False
    # 137: routers whose OSPF LSDB the snapshot reads, in preference order; empty ⇒ roster order.
    ospf_sources: list[str] = field(default_factory=list)


@dataclass
class VoiceSettings:
    """Settings for the ``obi voice`` push-to-talk interface (feature 023) + web surface (024).

    Additive; every field has a default and the ``[voice]`` section is optional. Never a
    "source". ``stt_model`` is a HuggingFace repo id for the local (on-device) Whisper model;
    ``tts_voice``/``tts_rate`` map to macOS ``say -v``/``-r``; ``rms_threshold`` is the
    no-speech pre-gate on the full-scale float32 capture (research R4). The remaining
    ``web_*`` keys shape the voice ENGINE on the unified web origin (024/033): conversation
    idle expiry and the longest accepted browser recording. The 024 hosting keys
    (``web_port``/``web_bind``) were retired by 033 — the one origin is ``[web]``.
    """

    stt_model: str = "mlx-community/whisper-large-v3-turbo"
    tts_voice: str | None = None
    tts_rate: int | None = None
    rms_threshold: float = 0.01
    web_idle_minutes: int = 30
    web_max_seconds: int = 90


@dataclass
class SnmpOverride:
    """Per-address SNMP override (012). Either field may be ``None`` to inherit the default."""

    community: Secret | None = None
    version: str | None = None


@dataclass
class SnmpHistorySettings:
    """Optional time-series collection settings (feature 021), parsed from ``[snmp.history]``.

    Its mere presence opts the operator into scheduled collection (``obi metrics ingest``); auth
    (community / v3 keys) is inherited from the enclosing :class:`SnmpSettings`. ``targets`` is a
    name-substring filter on roster **identity** (default ``-gw`` — the gateways). ``profiles`` are
    validated against the curated profile registry at parse time. ``retain_days`` is the self-prune
    horizon (metrics grow continuously, unlike the log store). ``query_buckets`` is the default
    downsample target for ``obi metrics history``.
    """

    db_path: Path = field(default_factory=default_metrics_db_path)
    targets: str = ""  # feature 137: no built-in naming convention; "" ⇒ every identified device
    profiles: list[str] = field(default_factory=lambda: ["interfaces", "system"])
    retain_days: int = 90
    query_buckets: int = 60


@dataclass
class SnmpSettings:
    """Connection settings for read-only SNMP querying of known devices (feature 012).

    SNMP is an additional capability layered over the devices obi already knows; it is NOT a vendor
    source and does not satisfy the ">=1 source" gate. ``version`` defaults to community-based
    ``v2c``; ``v3`` (authPriv) is opt-in. The community string and all v3 keys are wrapped in
    :class:`Secret` so they never appear in logs/audit/output (FR-007).
    """

    version: str = "v2c"
    community: Secret | None = None
    port: int = 161
    timeout: float = 5.0
    retries: int = 1
    profile: str = "system"
    v3_username: str | None = None
    v3_auth_proto: str = "SHA"
    v3_auth_key: Secret | None = None
    v3_priv_proto: str = "AES"
    v3_priv_key: Secret | None = None
    overrides: dict[str, SnmpOverride] = field(default_factory=dict)
    history: SnmpHistorySettings | None = None  # feature 021: time-series collection (opt-in)


@dataclass
class RadioSettings:
    """Walkie-talkie radio station settings (feature 088), parsed from ``[radio]``.

    Presence of the section enables the radio surface; it does NOT satisfy the ">=1 source"
    gate. All level/timing defaults are the criticalrf 2026-08-19 calibrated reference for
    this exact hardware (C-Media codec + FTDI FT232R). ``tx_enabled`` is THE transmit gate
    (constitution 1.12.0, Principle I RF-transmission class): default off, and with it off
    the PTT line is never asserted — ``shadow_tx`` additionally DMs drafted replies to Slack
    while still never keying. ``lead_ms`` is deliberately 300 ms, not the 1 s human
    guideline: obi's audio starts deterministically after key-up. ``model=None`` inherits
    ``[chat].model`` (the watch pattern); ``max_tokens`` is small because on-air answers are
    at most two short sentences. ``retention_days=0`` keeps everything for the season.
    """

    audio_name: str = "USB PnP Sound Device"
    # feature 137: the PTT cable is identified from config only — ftdi_serial or serial_port.
    ftdi_serial: str | None = None
    serial_port: str | None = None  # explicit /dev node; overrides ftdi_serial resolution
    sample_rate: int = 48000
    squelch_dbfs: float = -45.0
    min_active_ms: int = 250
    hang_ms: int = 1200
    # The two-floor splitter (field 2026-08-20): quiet BETWEEN transmissions is true
    # silence (-74..-85 dB rms), 20+ dB below the keyed carrier floor (~-53). A run of
    # split_quiet_ms at/below this floor closes the segment immediately — separating
    # back-to-back stations that hang would bridge, and emitting segments ~900 ms sooner.
    inter_tx_floor_dbfs: float = -60.0
    split_quiet_ms: int = 300
    clear_window_ms: int = 700  # listen-before-transmit window; raise for repeater tail
    lead_ms: int = 300
    tail_ms: int = 200
    tx_cap_s: int = 15
    patience_s: int = 60
    call_timeout_s: int = 45
    compose_deadline_s: int = 20
    max_tx_per_minute: int = 6
    max_tx_per_hour: int = 60
    # Digital TX scaling. Full scale by default (feature 092): the reference chain applies NO
    # digital attenuation and puts the trim on the HOST output gain (0.90, -2.9 dB). The old
    # 0.298 — and the 0.45/0.64 ladder that chased it — assumed hot drive would over-deviate
    # the radio's mic input. The 2026-08-21 acoustic drive-response measurement overturned
    # that: handset A's mic path gates below ~-44 dB drive and compresses ~2.5:1 above it, so
    # 29 dB more drive bought only ~11 dB more received level, and 0.90 with a -1 dBFS tone
    # showed no harshness. Loudness comes from DENSITY (tx_loudness), not from level.
    tx_gain: float = 1.0
    # The TX loudness/density stage (092). Off ⇒ transmit the raw TTS render, which measures
    # ~14 dB crest factor and reads noticeably quieter than human talkers through the mic AGC.
    tx_loudness: bool = True
    # How long an `obi radio say` request stays valid before the station refuses to air it.
    # Without an expiry, a request enqueued against a stopped daemon would transmit at the
    # next start-up — possibly hours later, out of context, on a live channel.
    operator_tx_ttl_s: int = 120
    # The calibrated HOST codec gains (092). obi never sets these on its own; it compares the
    # live device against them and warns, because they silently revert to power-on defaults
    # (input 0.504 = +12 dB and clipping, output 1.000) whenever the USB device re-enumerates.
    # The third leg of the calibration — handset A's volume knob at 1/10 — obi cannot observe.
    # See docs/development/radio-audio-chain.md.
    host_input_gain: float = 0.0
    host_output_gain: float = 0.90
    host_gain_tolerance: float = 0.02
    tx_enabled: bool = False
    shadow_tx: bool = False
    name_variants: list[str] = field(default_factory=lambda: ["obi", "obie", "obee", "o b", "opie"])
    # Feature 109: the WEAK station-name tier — transcription mis-hearings that fire only
    # in the doubled-hail shape ("Okie, Okie, …"), never on a single mention ("okie
    # dokie" must not open a call). Seeded from mined radio.db history; empty = opt out
    # (strong variants above are unaffected).
    name_variants_weak: list[str] = field(default_factory=lambda: ["ovi", "obini", "okie"])
    operator_handles: list[str] = field(default_factory=list)
    # The channel's known callsigns (field lesson 2026-08-19): fed verbatim into the STT
    # vocabulary prompt (so configure display casing — "Quill", "ZFX", "Hi-Fi") and used as
    # the fuzzy-match roster for caller extraction. operator_handles are implicitly part of
    # the roster; this list is everyone else on the channel.
    known_callers: list[str] = field(default_factory=list)
    # Domain vocabulary for the STT prompt beyond callsigns — site and device names spoken
    # on the channel ("NOC" transcribed as "NOG" until it was in the prompt). Verbatim
    # casing; keep it to words people actually say, the prompt budget is small.
    vocab_terms: list[str] = field(default_factory=list)
    ack_text: str = "go for obi"
    unavailable_text: str = "unable to comply, obi out"
    say_again_text: str = "ten nine, say again"
    # 093: the on-air give-up when a garbled exchange spends its "say again" — the call
    # closes on the air instead of in silence. The machine guarantees the trailing "out".
    garble_close_text: str = "unable to copy, out"
    # Feature 109: channel-specific sign-off phrases ("back on IT") that close an open
    # call instead of being answered as requests — joins the built-in clearing shapes
    # now that the widened in-window gate composes everything else. Substring-matched
    # case-insensitively by the call machine.
    sign_offs: list[str] = field(default_factory=list)
    # The radio "thinking…" (2026-08-20): transmitted once per call when composing runs
    # longer than standby_after_s — the caller knows obi is working, not gone.
    standby_text: str = "stand by"
    standby_after_s: int = 4
    # TTS pronunciation guide (2026-08-20: 'NOC' was spelled out as EN-OH-CEE; it is
    # pronounced 'knock'). Applied to the TRANSMITTED AUDIO only, word-boundary and
    # case-insensitive; stored transcripts and the board keep the canonical spelling.
    pronunciations: dict[str, str] = field(default_factory=dict)
    # The inbound twin (2026-08-20: whisper heard 'knock'/'North' for NOC): heard-word →
    # canonical rewrites applied to the REQUEST TEXT the composer sees, word-boundary and
    # case-insensitive. Stored transcripts keep what was actually heard — the record is
    # honest; only the LLM gets the charitable reading.
    speech_aliases: dict[str, str] = field(default_factory=dict)
    #: feature 137: how the radio persona names its channel (no built-in event channel).
    channel_name: str = "the operations radio channel"
    # Known CALLSIGN mis-hearings ("quilt" → Quill, field 2026-08-20) that fuzzy roster
    # matching can't reach. Applied only in callsign position (trigger + speaker
    # attribution), never inside request text — site-specific, so no defaults.
    caller_aliases: dict[str, str] = field(default_factory=dict)
    stt_model: str = "mlx-community/whisper-large-v3-turbo"
    tts_voice: str | None = None
    tts_rate: int | None = None
    model: str | None = None
    max_tokens: int = 600
    live_calls: int = 3
    slack_user: str | None = None
    # Per-call Slack DMs (dropped/garbled/abandoned/failed exchanges). Off ⇒ only the
    # malfunction alerts remain (rate backstop, hardware outage) — those signal the
    # STATION is broken, not that a call went badly (operator direction 2026-08-20).
    slack_call_notes: bool = True
    db_path: Path = field(default_factory=default_radio_db_path)
    clips_dir: Path = field(default_factory=default_radio_clips_dir)
    retention_days: int = 0
    # Capture watchdog (feature 113). Defaults derive from field data Aug 22–29 (see
    # specs/113-radio-capture-watchdog/research.md): a real codec on a live handset never
    # reads below ~−85 dBFS rms (flatline floor −100 ⇒ zombie stream); healthy segment
    # peaks pin near 0 dBFS via the PTT click, so a 20-segment window whose MAX peak is
    # under −10 is a faded input path (fires ≈ hour 36 of the worst recorded session,
    # never on a healthy day). The floor keeps recoveries spaced (never back-to-back —
    # field 2026-08-29); a third fire inside the escalation window exits for launchd.
    watchdog: bool = True
    watchdog_flatline_dbfs: float = -100.0
    watchdog_flatline_s: int = 120
    watchdog_fade_window: int = 20
    watchdog_fade_min: int = 10
    watchdog_fade_peak_dbfs: float = -10.0
    watchdog_fade_span_s: int = 43200
    watchdog_floor_s: int = 600
    watchdog_max_reopens: int = 2
    watchdog_escalation_s: int = 3600
    watchdog_stuck_s: int = 300
    # Preventive capture reopen (feature 122). The capture path demonstrably degrades
    # with stream uptime (garble; 113 research — root cause not pinned); closing and
    # reopening the stream after each transmission bounds any lifetime to roughly one
    # exchange, RF-silently (~1 s deaf, whisper stays loaded). Cadence is the operator's
    # 2026-09-01 call: per-transmission, overriding the back-to-back lesson as scoped to
    # full-process/FTDI restarts — gap_ms + host-gain re-log per reopen are the
    # tripwire, `after_tx = false` (backstop-only) the rollback. Independent of
    # `watchdog` — that flag gates the DETECTORS. after_tx=false + max_uptime_s=0 ⇒
    # feature fully off (pre-122 behavior). Replaces the external 04:20/16:20 restarts.
    capture_reopen_after_tx: bool = True
    capture_reopen_floor_s: int = 60
    capture_reopen_quiet_ms: int = 2000
    capture_reopen_max_uptime_s: int = 7200
    # Garble-triggered capture reopen (feature 133). A garbled in-call transmission —
    # the whisper hallucination shapes of a sick capture chain (looping blobs, Cyrillic,
    # "Ó, píða, og fór í hana." on 2026-09-07 21:00:51 PT) — is treated as device evidence:
    # the capture stream is reopened at the block-loop seam BEFORE obi's "say again" keys,
    # so the caller's repeat lands on a fresh stream. Operator decisions 2026-09-07: the
    # widened garble judgement is always on (garble.py — not behind this switch); the
    # once-per-call say-again budget is unchanged; no automatic process restart, ever.
    # Independent of `watchdog`; shares `capture_reopen_floor_s` (floor of ANY cause).
    # min_age_s: a stream younger than this is not suspected (garble on it is RF/caller
    # trouble). max_per_hour: executed garble reopens in a rolling hour; beyond it the
    # candidate is suppressed and the operator is noted once per hour.
    garble_reopen: bool = True
    garble_reopen_min_age_s: int = 60
    garble_reopen_max_per_hour: int = 6
    # Outbound calls (feature 124): operator-queued message delivery. Obi hails
    # "<Name>, <Name>, obi", listens outbound_ack_window_s for a go-ahead, re-hails at
    # most outbound_max_rehails times, and gives up honestly. outbound_ttl_s bounds the
    # whole request (enqueue → terminal); outbound_max_chars gates the message length at
    # submission — obi never truncates someone's message on the air (the tx duration cap
    # stays the physical belt underneath).
    outbound_ack_window_s: int = 30
    outbound_max_rehails: int = 2
    outbound_ttl_s: int = 600
    outbound_max_chars: int = 320

    def stage(self) -> str:
        """The rollout stage this config expresses: ``shadow`` / ``shadow-tx`` / ``live``.

        Software knows exactly these three; the simplex-test vs repeater distinction is
        human radio tuning and has no enum value. ``tx_enabled`` wins over ``shadow_tx``.
        """

        if self.tx_enabled:
            return "live"
        return "shadow-tx" if self.shadow_tx else "shadow"


@dataclass
class ChatSettings:
    """Settings for the ``obi chat`` cloud reasoning layer (feature 005).

    Cloud-dependent in v1 — the deliberate, time-bounded carve-out in constitution v1.3.0
    (Principle VI). ``api_key`` is wrapped in :class:`Secret`; when unset, the key is resolved
    from the ``ANTHROPIC_API_KEY`` environment variable at the point of use. It is never logged,
    audited, or sent to devices.
    """

    api_key: Secret | None = None
    model: str = "claude-sonnet-5"
    max_tokens: int = 1024
    # feature 008: optional operator-provided network-context document. Its free-form content is
    # injected into the chat system prompt as interpretation-only background (chat/context.py).
    # A path, not a secret; absent ⇒ chat is unchanged. Validity is decided at load time, so a
    # bad/missing path never fails load_config.
    context_file: Path | None = None


@dataclass
class SlackSettings:
    """Settings for the obi Slack interface (feature 009).

    Cloud-dependent like chat (constitution v1.3.0 carve-out). Both tokens are wrapped in
    :class:`Secret` so they never appear in logs/audit and are never sent to the reasoning model;
    the cloud-model settings themselves are reused from :class:`ChatSettings`. Authorization is
    the union of two sources (feature 104): ``allowlist`` — authorized **Slack user IDs** (the
    immutable per-user identifier) — plus current membership of any ``authorized_channels``.
    Channels are configured by conversation **ID** (``C…``/``G…``), never display name, so a
    rename can't revoke or re-grant access. An empty ``authorized_channels`` is exactly the
    pre-104 allowlist-only behavior.
    """

    bot_token: Secret  # xoxb-… — WebClient: post replies, read channel type
    app_token: Secret  # xapp-… — SocketModeClient: open the outbound Socket Mode connection
    allowlist: list[str]  # authorized Slack user IDs; non-empty
    # Conversation IDs whose members are authorized and where direct @-mentions are answered
    # (feature 104). Optional — default [] keeps the feature off.
    authorized_channels: list[str] = field(default_factory=list)


@dataclass
class ConfigBackupSettings:
    """RouterOS config-backup settings (feature 026), parsed from ``[configs]``.

    Its mere presence opts the operator into ``obi configs ingest`` (the 013/021 convention). SSH
    credentials are resolved at parse time: ``username``/``password`` from ``[configs]`` if given,
    else inherited from ``[routeros]`` (the same account, once its RouterOS group has the ``ssh``
    policy). Capture is read-only — a hardcoded ``/export`` over SSH, redacted (never
    ``show-sensitive``). ``targets`` is a roster-identity substring filter; the default ``""`` means
    **every identified device** (backup wants the whole fleet, unlike metrics' ``-gw``).
    """

    username: str
    password: Secret
    ssh_port: int = 22
    timeout: float = 10.0
    concurrency: int = 8
    repo_path: Path = field(default_factory=default_config_repo_dir)
    targets: str = ""
    # ``terse`` = one command per line (no ``\`` wrapping) ⇒ clean one-line diffs. ``verbose`` also
    # emits default values (bigger + noisier across RouterOS versions) — opt-in. Both compose into a
    # fixed ``/export [verbose] [terse]`` (no operator-supplied command text; Principle I).
    terse: bool = True
    verbose: bool = False


def default_audit_db_path() -> Path:
    """XDG state location for the audit event store (feature 042).

    ``$XDG_STATE_HOME/obi/audit.db`` (``~/.local/state`` default), beside the JSONL audit log
    and the other stores. Deliberately a **separate** database from logs/metrics — the audit
    trail has its own growth, retention, and payload profile.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "audit.db"


@dataclass
class AuditSettings:
    """The audit event store (feature 042): full-payload capture of everything obi does.

    ON by default (Constitution Principle V — every action recorded); the section exists to
    tune retention/payload bounds or to disable capture. ``max_payload_bytes`` bounds each
    stored payload field (truncated with an explicit marker beyond it); ``retention_days``
    drives the store's self-pruning.
    """

    enabled: bool = True
    db_path: Path = field(default_factory=default_audit_db_path)
    retention_days: int = 90
    max_payload_bytes: int = 500_000


@dataclass
class ServiceSettings:
    """Daemon-management settings (feature 081), parsed from ``[service]``.

    Additive and all-default: the section is optional and its absence leaves every command's
    behavior unchanged. ``run_at_login`` is the default ``RunAtLoad`` for ``obi <daemon> start``
    (a macOS LaunchAgent), overridable per invocation by ``--login`` / ``--no-login``.
    ``environment`` is an escape hatch of extra environment variables merged into the generated
    plist — needed only for a host whose secrets live in the environment rather than the config
    file. The plist itself never carries a token; supervised daemons read the config at runtime
    exactly as the foreground process does.
    """

    run_at_login: bool = True
    environment: dict[str, str] = field(default_factory=dict)


def default_dash_state_dir() -> Path:
    """XDG state location for the status dashboard's state files (feature 095).

    ``$XDG_STATE_HOME/obi/dash`` — holds ``board.json`` (the last evaluated board, what the web
    route and ``obi dash show`` render), ``netwatch-state.json`` (counter memory between cycles),
    and ``pager-state.json`` (the held red-set). JSON files, not a database: every signal needs
    only current state plus one short window of memory, and there is exactly one writer.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "dash"


# Mirrors obi.core.dash.SIGNALS; inlined to avoid a config→dash import cycle (the
# slack_min_severity precedent). A test pins the two tuples equal.
_DASH_SIGNALS = ("fleet", "netwatch", "ping", "internet", "pipeline", "mesh", "lighting")

# 121: the signals that may PAGE — deliberately a separate, smaller tuple. `signals` used to
# default to every signal, so adding mesh/lighting to _DASH_SIGNALS alone would have made
# both page on their first red. The operator asked for these two to be display-only, and a
# default is not a property: listing a non-pageable signal in [dash.pager] signals is a
# load-time error, so there is no configuration by which they alert.
# Contract: specs/121-mesh-hardening/contracts/dash-signals.md E1/E2.
_DASH_PAGEABLE_SIGNALS = ("fleet", "netwatch", "ping", "internet", "pipeline")


@dataclass(frozen=True)
class DashInternetLink:
    """One monitored uplink for the dashboard's INTERNET tile (``[[dash.internet_links]]``).

    ``capacity_mbps`` is operator knowledge (the provisioned rate), deliberately without a
    default — utilization percentages are meaningless against a guessed denominator.
    """

    device: str
    interface: str
    capacity_mbps: float
    label: str = ""


@dataclass(frozen=True)
class DashPagerSettings:
    """The dashboard's red-transition pager (``[dash.pager]``), OFF by default (feature 095).

    Deterministic — no LLM: an item transitioning into red pages the operator once (a terse
    Slack DM over the watch delivery path) and sends a one-line recovery notice when it clears.
    Yellow and stale never page. ``signals`` limits which tiles may page; ``mass_ratio`` is the
    per-signal coalesce threshold (that fraction of a signal's items transitioning in one cycle
    collapses into a single DM). While enabled, ``obi watch run`` skips its hourly 072 ICMP pass
    (same store, paged an hour earlier here) but keeps refreshing that pass's baseline.

    Feature 106 (link incidents): ``link_grouping`` correlates reds whose labels share a link
    identity (two known site tokens + optional band) into one incident — one page, silent
    folds, one recovery when the whole group clears. ``announce_hold`` > 0 delays a NEW
    incident's first page that long and then sends one full-picture message; ``"0s"`` pages on
    the first symptom. ``extra_sites`` supplies site tokens with no ``<site>-gw`` inventory
    identity (e.g. kiosk, depot7) so their links can group too.
    """

    enabled: bool = False
    mass_ratio: float = 0.80
    # 121: the PAGEABLE set, not every signal. Both this default and the parser's must use it —
    # a DashPagerSettings() built directly (defaults, tests, callers) would otherwise page on
    # the display-only signals even though the config path refuses them.
    signals: list[str] = field(default_factory=lambda: list(_DASH_PAGEABLE_SIGNALS))
    link_grouping: bool = True
    announce_hold: str = "0s"
    extra_sites: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DashMeshSettings:
    """The dashboard's MESH tile thresholds (``[dash.mesh]``, feature 121).

    Display-only: this tile can never page (see :data:`_DASH_PAGEABLE_SIGNALS`) and its
    collector can never transmit — it reads the node table passively and the 117 position
    store, never the locate engine. It exists because a mesh radio that went dark on
    2026-08-31 stayed unnoticed for 29 hours: the failure wrote log lines nobody reads.
    """

    heard_yellow_after: str = "2h"
    heard_red_after: str = "12h"
    fix_yellow_after: str = "6h"
    # How often the SERIAL read actually happens. Deliberately slower than the 1-minute dash
    # cycle: the mesh port is exclusive-access, and `obi mesh locate` needs it to answer. A
    # per-minute open would make a real answer fail with PORT_BUSY a few percent of the time
    # to keep a status tile fresh — a bad trade for a signal whose job is catching outages
    # that last hours. The position-history half is read from local disk every cycle.
    read_interval: str = "5m"


@dataclass(frozen=True)
class DashLightingSettings:
    """The dashboard's LIGHTING tile (``[dash.lighting]``, feature 121).

    Reachability of the ``[wled]`` allowlisted controllers, read through the existing
    read-only status path. A controller that is reachable but switched OFF is green: the
    portal being dark is an operator decision, not a fault.
    """

    #: Per-controller read deadline; ``None`` ⇒ inherit ``[wled] timeout``.
    timeout: float | None = None


@dataclass(frozen=True)
class DashStatusBandSettings:
    """The dash cycle's status-band reconciler (``[dash.status_band]``, feature 128).

    OFF by default — the deliberate second enable (beside a declared
    ``[wled.segments.<device>]`` layout) behind obi's first autonomous device write:
    the per-minute cycle reconciling the tower's status segments to pageable network
    health (constitution 2.3.0). Deterministic, edge-triggered, fail-quiet; the
    reasoning model has no path to it.
    """

    enabled: bool = False
    #: Per-device read/write deadline; ``None`` ⇒ inherit ``[wled] timeout``.
    timeout: float | None = None


@dataclass(frozen=True)
class DashSettings:
    """Status dashboard settings (feature 095), parsed from ``[dash]``.

    Presence opts the operator into ``obi dash run`` (the 1-minute status cycle) and the DASH
    web board; absent ⇒ the feature is inactive everywhere. All thresholds are judgment inputs
    for the pure evaluator — none is contractual (spec FR-014). Durations (``flap_window``,
    ``netwatch_loss_window``) use the ``"10m"``/``"1h"`` suffix grammar and are validated at
    load. The PING tile's reference probe runs FROM ``ping_router`` (a roster identity or
    address) via the read-only 028 ping seam; ``ping_down_after`` consecutive all-refs-lost
    cycles turn that tile red. The INTERNET tile judges the ``internet_links`` uplinks against
    their provisioned capacity, both directions.
    """

    enabled: bool = False
    state_dir: Path = field(default_factory=default_dash_state_dir)
    ping_router: str = ""  # feature 137: no built-in device; empty ⇒ PING tile "not configured"
    ping_refs: list[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1"])
    ping_rtt_warn_ms: float = 50.0
    ping_loss_warn_pct: float = 25.0
    ping_down_after: int = 2
    netwatch_loss_warn_pct: float = 5.0
    netwatch_rtt_warn_ms: float = 25.0
    # Loss is judged over this window of accumulated counter deltas, not one cycle — at
    # netwatch's default 10 s interval a single minute holds ~6 samples, where one lost probe
    # already reads ~17 %. The window smooths that to a real signal (analysis finding U2).
    netwatch_loss_window: str = "5m"
    flap_window: str = "10m"
    internet_warn_pct: float = 70.0
    internet_down_pct: float = 90.0
    internet_links: list[DashInternetLink] = field(default_factory=list)
    pager: DashPagerSettings = field(default_factory=DashPagerSettings)
    # 121: display-only signals. Both tiles are omitted entirely when their subsystem is
    # unconfigured — absent, not stale, since "no mesh radio on this install" is not a gap.
    mesh: DashMeshSettings = field(default_factory=DashMeshSettings)
    lighting: DashLightingSettings = field(default_factory=DashLightingSettings)
    # 128: the status-band reconciler; enabled additionally requires a [wled.segments]
    # layout (cross-section rule in load_config — a half-configured autonomous writer
    # must fail at load, never silently no-op in the minute cron).
    status_band: DashStatusBandSettings = field(default_factory=DashStatusBandSettings)


def default_paging_roster_path() -> Path:
    """XDG state location for the paging roster snapshot (feature 102).

    ``$XDG_STATE_HOME/obi/paging/roster.csv`` — operator-local by design: the roster holds
    real ESD names and never enters the repository.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "paging" / "roster.csv"


@dataclass(frozen=True)
class PagingAlertSettings:
    """The dash pager's paging transport (``[paging.alerts]``), OFF by default (feature 102).

    The ONLY automated path that can send a page. ``people`` is a static list of PlayaPage
    person IDs — person IDs only, enforced at load: a group here could page ~100 real
    responders on a malfunction, so groups are impossible by construction on this path (they
    remain available to a human at the CLI). ``max_pages_per_hour`` is a hard sliding-window
    cap; at the cap pages are suppressed (audited, never queued) while Slack carries on.
    """

    enabled: bool = False
    people: list[str] = field(default_factory=list)
    max_pages_per_hour: int = 6


@dataclass(frozen=True)
class PagingSlackSettings:
    """Slack-DM member paging (``[paging.slack]``, feature 130; constitution 3.0.0).

    The section's PRESENCE is the feature switch: absent ⇒ the ``page_send`` tool exists
    nowhere and behavior is exactly pre-130. ``channels`` lists the conversation IDs
    (``C…``/``G…``, ids never names — the 104 rename-safety rule) whose current members may
    page obi from a DM; an empty list means operators only (the ``[slack] allowlist`` is
    always authorized). ``recipients`` is the REQUIRED recipient allowlist (operator decision
    2026-09-03): roster tokens — names or numeric ids — naming who member pages may reach
    (e.g. the WAN team individuals, the operator, and the on-call rotations); anyone outside it
    refuses, and a GROUP is pageable only by being listed here. ``max_pages_per_hour`` is a
    per-SENDER sliding-hour cap, deliberately independent of the dash-pager's cap (an alert
    storm must not consume the humans' budget, nor vice versa).
    """

    channels: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    max_pages_per_hour: int = 4


@dataclass
class PagingSettings:
    """PlayaPage paging egress (``[paging]``, feature 102).

    Credentials ride on every uAPI request (no sessions); both are operator secrets. The
    section's presence opts into the ``obi page`` CLI verbs only — the automated transport
    additionally needs ``[paging.alerts] enabled`` AND ``[dash.pager] enabled`` (a deliberate
    two-key design, spec FR-005). No reasoning-model surface can reach this channel.
    """

    username: str
    password: Secret
    url: str = ""  # required in [paging] (feature 137: no built-in endpoint)
    roster_path: Path = field(default_factory=default_paging_roster_path)
    message_max_chars: int = 200
    send_spacing_s: float = 2.0
    alerts: PagingAlertSettings = field(default_factory=PagingAlertSettings)
    # feature 130: Slack-DM member paging opt-in. None (section absent) ⇒ pre-130 behavior.
    slack: PagingSlackSettings | None = None


@dataclass(frozen=True)
class SegmentLayout:
    """Role split of one lighting device's segments (``[wled.segments.<device>]``, feature 128).

    ``status`` names the segment ids reserved for the tower's status bands (dash-reconciled,
    off-limits to operator look writes); ``main`` names the pattern area operator writes are
    scoped to. Declared per device because the physical layout was hand-discovered and may
    change; a device with NO declared layout keeps the pre-128 uniform-write behavior on every
    path and the status pipeline stays inert for it.
    """

    status: tuple[int, ...]
    main: tuple[int, ...]


@dataclass
class WledSettings:
    """The ``[wled]`` LED lighting controllers (feature 103).

    ``devices`` (name → base URL) is the COMPLETE write allowlist for obi's decorative-lighting
    write class (constitution 1.15.0): a lighting request can only ever target a name listed
    here, and no other device class gains any write capability. The controllers are
    intentionally unauthenticated on the trusted LAN/tailnet — obi's guardrails are this
    allowlist, the static ``low`` risk class, pre-change snapshots, and the audit trail, not
    device-side auth (hence no credentials). Empty/missing ``devices`` ⇒ the section parses to
    ``None`` and lighting is simply not configured, never an error.
    """

    devices: dict[str, str] = field(default_factory=dict)
    timeout: float = 5.0
    verify_tls: bool = True
    #: 137: optional per-device description for prompts/tool text (``{url=…, description=…}``).
    descriptions: dict[str, str] = field(default_factory=dict)
    #: 128: per-device segment role split; empty ⇒ no device has a layout (legacy behavior).
    segments: dict[str, SegmentLayout] = field(default_factory=dict)


@dataclass
class MeshtasticSettings:
    """The ``[meshtastic]`` mesh-locate opt-in (feature 111).

    The SECTION'S PRESENCE is the enable — an empty ``[meshtastic]`` table is a valid
    all-defaults opt-in. Deliberate: the mesh_locate tool can transmit a position request on
    shared LoRa airtime, and a transmitting capability is operator-enabled, never ambient
    (the default-off posture Principle I takes for egress). ``port`` overrides USB discovery;
    ``fresh_within_s`` bounds when a stored position still answers passively; the two limits
    bound how often obi may key the mesh, durably across processes (ratelimit.json under
    :func:`default_mesh_state_path`).

    **Worst-case answer time is ``snapshot_timeout_s + request_timeout_s``** — the node-table
    read AND the position request, in series. Before feature 121 the read's deadline was a
    hardcoded driver default that no config comment counted, so tuning ``request_timeout_s``
    alone understated the real bound.
    """

    port: str | None = None
    # 121: the node-table read deadline. Was a hardcoded `timeout: float = 10.0` parameter
    # default on MeshDriver.snapshot that neither caller ever passed, so it was invisible to
    # the operator and absent from the worst-case arithmetic above.
    snapshot_timeout_s: float = 10.0
    # 121: 900 → 300. obi was answering from fixes up to ~14 min old — for a moving tracker
    # that can be a mile stale, and it is exactly when people ask — while spending about 1 of
    # its 30 permitted transmissions per hour. The airtime was there; the freshness was not.
    fresh_within_s: int = 300
    request_timeout_s: float = 30.0
    node_rate_limit_s: int = 60
    hourly_tx_cap: int = 30
    # 111 field lesson (2026-08-30): position requests must go out on the channel the
    # tracker actually talks on. The primary community channel shares moduleSettings with
    # positionPrecision 0 (position sharing off), so a request there is never answered —
    # the same request on the private secondary channel answered instantly. 0 = primary.
    request_channel_index: int = 0
    # 117: the position-history store (recorded fixes for report_only nodes). Lives in the
    # mesh state dir beside ratelimit.json — a per-node privacy artifact, deletable by hand.
    positions_db_path: Path = field(
        default_factory=lambda: default_mesh_state_path() / "positions.db"
    )
    # 123: the mesh daemon's Unix RPC socket. Every consumer reaches the radio through the
    # daemon that listens here — no other obi process opens the serial port (contract O1).
    daemon_socket: Path = field(default_factory=lambda: default_mesh_state_path() / "daemon.sock")
    # 123: how often the daemon's backstop sweep re-reads the node table (an in-memory read
    # of the held interface — no serial command) and records allowlisted fixes. Live packet
    # callbacks record sooner; the sweep catches anything they miss.
    daemon_sweep_s: float = 60.0
    # 123: default standby lease — how long the daemon leaves the port on loan for a direct
    # CLI experiment before taking it back on its own. The self-healing bound that replaces
    # "pause the crontab and remember to restore it" (the 2026-09-02 outage).
    standby_lease_s: int = 1800
    # 125: the daemon's automatic freshness pass — when a tracked node's newest RECORDED
    # fix is older than this, the daemon itself sends the one rate-limited position
    # request (audited as meshd:auto) and records the reply. 0 disables automatic
    # requesting. Default-ON at 5 minutes is the operator's 2026-09-02 directive: lookups
    # never transmit (they answer instantly from recorded data), so freshness must be
    # bought on a schedule by the process that owns the radio.
    auto_request_s: int = 300
    # feature 111 follow-on: operator-chosen names for nodes, the way people actually ask
    # ("art car" → the tracker's node id). Alias → node id or mesh name; matched case-insensitively
    # BEFORE the mesh-DB lookup, so an alias is an operator decision that beats fuzzy matching.
    aliases: dict[str, str] = field(default_factory=dict)
    # 111 field follow-on (2026-08-29): the name obi SAYS for a node ([meshtastic.names],
    # node id → spoken name). Mesh names are device labels ("Tumble 9831 (TMBL)"); the
    # answer should just say "Tumble". The inverse of aliases: many ways in, one name out.
    display_names: dict[str, str] = field(default_factory=dict)
    # 111 privacy gate (operator decision 2026-08-29): when non-empty, mesh_locate's whole
    # resolution universe shrinks to these nodes (ids or mesh names) — other stations'
    # positions AND names are undisclosable (radio is any-caller; strangers' trackers are
    # not obi's to report). Empty = unrestricted.
    report_only: list[str] = field(default_factory=list)
    # 126: art-installation landmarks. The locally stored Burning Man public API art
    # dataset (written by `obi playa fetch` since 127, only ever READ by answer paths — a
    # location answer never waits on the internet) and the radius within which a located
    # position appends ", near <art name>". Absent file = answers without references.
    # 127: the default moved to the shared playa data dir — ONE art.json serves both the
    # mesh landmarks and the playa_locate lookup (an explicit override serves both too).
    art_path: Path = field(default_factory=lambda: default_playa_state_path() / "art.json")
    art_near_ft: int = 400
    # 121 SAFETY: serial devices another obi subsystem has claimed — today the radio
    # station's PTT half. DERIVED at load from [radio], never read from a TOML key: an
    # operator-typed list could be emptied, and empty here means the mesh driver may open the
    # transmitter. Opening it asserts DTR+RTS, both of which key the radio (radio/ptt.py,
    # measured on air), and the mesh path has no de-key step — so this is a safety guard, not
    # a tidiness one. See specs/121-mesh-hardening/contracts/mesh-port-safety.md.
    excluded_ports: tuple[str, ...] = ()


@dataclass
class VictoriaSettings:
    """The ``[victoria]`` section (feature 108): the external team's VictoriaMetrics store.

    A **second-tier**, read-only metrics source — obi's own stores stay primary for anything
    they cover; this one holds what obi doesn't collect (playa backbone RF, weather, fleet
    optics, Linux server vitals). The store is deliberately credential-free on the ops network
    (hence no ``Secret`` anywhere); obi's guardrails are the driver's five-endpoint GET
    allowlist and per-request timeouts, not auth. Named ``[victoria]`` for the technology; the
    tool names say what the data is (backbone_rf_health, weather_observed,
    victoria_query/discover — the team's name is kept out of tool names: it collides with a
    credential string and gets audit-redacted).
    """

    base_url: str
    timeout: float = 10.0  # httpx deadline AND the store's own timeout= parameter
    rf_window: str = "24h"  # default backbone_rf_health trend window
    weather_stale_s: int = 900  # weather_observed leads with staleness above this reading age
    #: 137: how tool text names the store, and the PromQL label matcher selecting RF series.
    display_name: str = "the external metrics store"
    rf_matcher: str = '{role="ptp-rf"}'


# 135: the operator's maintenance declaration. Signals are the non-device subsystems that can
# be switched off whole; "radio" is the walkie-talkie station (088), not the mesh radio.
MAINTENANCE_SIGNALS = ("radio", "mesh", "lighting")
_MAINTENANCE_KEYS = ("sites", "devices", "uisp_sites", "signals", "reason")


@dataclass(frozen=True)
class MaintenanceSettings:
    """``[maintenance]`` (feature 135) — what the operator has declared deliberately off.

    Matched items stay visible (labeled) but never alarm: out of tile rollups and the board
    state, never paged, never on the status band, skipped by the watch passes, and (``radio``)
    no station outage DMs. Unioned with the UISP controller's per-device maintenance flag.
    Values are casefolded at parse; matching rules live in :mod:`obi.core.maintenance`.
    """

    sites: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    uisp_sites: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.sites or self.devices or self.uisp_sites or self.signals)


@dataclass
class Config:
    """Resolved obi configuration for one run.

    At least one source (``routeros``, ``uisp``, ``netbox``, ``unifi_network``, and/or ``logs``)
    is configured. ``scope`` is present only when ``routeros`` is (a UISP-only, NetBox-only,
    UniFi-only, or logs-only config needs no discovery scope).
    """

    scope: DiscoveryScope | None = None
    routeros: RouterosSettings | None = None
    # feature 028: bounds for the live RouterOS query/ping/traceroute tools. Always present (safe
    # defaults); the tools themselves gate on `routeros` being configured.
    routeros_live: RouterosLiveSettings = field(default_factory=RouterosLiveSettings)
    uisp: UispSettings | None = None
    netbox: NetboxSettings | None = None
    unifi_network: UnifiNetworkSettings | None = None
    unifi_sm: UnifiSiteManagerSettings | None = None
    # feature 016: reverse-engineered UniFi controller-event ingestion. Additive (like snmp/watch);
    # absent ⇒ `obi unifi logs ingest` is inactive and obi behaves exactly as feature 011 (FR-013).
    unifi_internal: UnifiInternalSettings | None = None
    logs: LogsSettings | None = None
    timeout: float = 4.0
    concurrency: int = 16
    budget: float = 30.0  # total crawl wall-clock budget in seconds (feature 002, FR-007)
    audit_path: Path = field(default_factory=lambda: default_audit_path())
    inventory_path: Path = field(default_factory=lambda: default_inventory_path())
    # feature 004: opt-in resolution of 169.254.x link-local peers to their OSPF router-id.
    # Default OFF ⇒ discover behaves exactly as 002 and issues zero OSPF reads (FR-001, FR-002).
    resolve_link_local_via_ospf: bool = False
    chat: ChatSettings = field(default_factory=ChatSettings)
    # feature 009: the Slack interface, present only when a [slack] section is configured. The
    # listener requires it; every other obi command ignores it.
    slack: SlackSettings | None = None
    # feature 012: optional read-only SNMP capability over the known rosters. Independent of the
    # ">=1 source" gate; absent ⇒ every existing command behaves exactly as before (FR-013).
    snmp: SnmpSettings | None = None
    # feature 014: the autonomous log watcher's settings. Additive and off by default; the watcher
    # reads the [logs] store and (optionally) the cloud model. Never affects the ">=1 source" gate.
    watch: WatchSettings = field(default_factory=WatchSettings)
    # feature 020: the Siklu EtherHaul radio roster + credentials, present only when a [siklu]
    # section with hosts is configured. Additive: absent ⇒ `obi siklu status` exits 2 with
    # guidance and `obi status`/chat behave exactly as before.
    siklu: SikluSettings | None = None
    # feature 022 (renamed by 033): the web viewer. Additive and all-default ([web] section
    # optional); export/serve additionally require [uisp] (the geometry source) at run time.
    web: WebSettings = field(default_factory=WebSettings)
    # feature 023: the voice interface. Additive and all-default ([voice] section optional);
    # `obi voice` additionally requires the `voice` extras and chat's model access at run time.
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    # feature 026: read-only RouterOS config backup into a local git repo. Additive; present only
    # when a [configs] section is configured. A capability over the mikrotik roster (like [snmp]),
    # so it does NOT satisfy the ">=1 source" gate; absent ⇒ `obi configs` is inactive and obi
    # behaves exactly as before.
    configs: ConfigBackupSettings | None = None
    # feature 042: the audit event store. Always present (on by default per Principle V); the
    # [audit] section tunes or disables it. The legacy JSONL trail path stays in `audit_path`.
    audit: AuditSettings = field(default_factory=AuditSettings)
    # feature 072: the ICMP reachability collector. Additive and optional ([icmp] section optional —
    # `obi icmp run` works with all-defaults); a capability over the inventory (like [snmp]), so it
    # does NOT satisfy the ">=1 source" gate. Absent ⇒ defaults; the [watch.icmp] pass is separate.
    icmp: IcmpSettings | None = None
    # feature 096: the Linux collector-VM health probe, present only when [health.linux]
    # names hosts. Additive capability (like [icmp]) — does NOT satisfy the ">=1 source"
    # gate; absent ⇒ `obi health probe` exits with guidance and nothing else changes.
    health_linux: HealthLinuxSettings | None = None
    # feature 081: launchd daemon-management settings. Always present (all-default); the [service]
    # section only tunes start-at-login and the plist environment. Absent ⇒ defaults; no existing
    # command is affected.
    service: ServiceSettings = field(default_factory=ServiceSettings)
    # feature 088: the walkie-talkie radio station, present only when a [radio] section is
    # configured. Additive — a capability surface, so it does NOT satisfy the ">=1 source" gate;
    # absent ⇒ `obi radio` exits 2 with guidance and every other command behaves exactly as before.
    radio: RadioSettings | None = None
    # feature 095: the status dashboard (1-minute cycle + DASH board + red-transition pager).
    # Additive — a capability surface over existing stores/rosters, so it does NOT satisfy the
    # ">=1 source" gate; absent ⇒ `obi dash` exits with guidance and nothing else changes.
    dash: DashSettings | None = None
    # feature 102: the PlayaPage paging egress channel. Additive capability surface (does NOT
    # satisfy the ">=1 source" gate); absent ⇒ `obi page` exits 2 with guidance and the dash
    # pager stays Slack-only.
    paging: PagingSettings | None = None
    # feature 108: the external VictoriaMetrics store, present only when [victoria] is configured.
    # Additive capability (does NOT satisfy the ">=1 source" gate); absent ⇒ its tools
    # answer with a configure-me sentence.
    victoria: VictoriaSettings | None = None
    # feature 103: the WLED lighting-device allowlist, present only when [wled] names devices.
    # Additive capability (does NOT satisfy the ">=1 source" gate); absent ⇒ the wled tools/CLI
    # answer "no lighting device configured" and nothing else changes.
    wled: WledSettings | None = None
    # feature 110: the BRC weather snapshot collector. Additive and optional ([weather] section
    # optional — `obi weather run` works with all-defaults); a capability like [icmp], so it does
    # NOT satisfy the ">=1 source" gate. Absent ⇒ defaults; no watch pass exists for it.
    weather: WeatherSettings | None = None
    # feature 112: the BRC gate-traffic collector. Additive and optional ([traffic] section
    # optional — `obi traffic run` works with all-defaults); a capability like [weather], so it
    # does NOT satisfy the ">=1 source" gate. Absent ⇒ defaults; no watch pass exists for it.
    traffic: TrafficSettings | None = None
    # feature 137: the event city plan ([brc]) and public API ([playa]) — absent ⇒ those
    # features are off; [site] carries deployment-wide facts (timezone).
    brc: BrcSettings | None = None
    playa: PlayaSettings | None = None
    site: SiteSettings = field(default_factory=SiteSettings)
    # feature 111: the Meshtastic mesh-locate opt-in, present only when a [meshtastic] section
    # exists (an empty table is a valid all-defaults opt-in). Additive capability (does NOT
    # satisfy the ">=1 source" gate); absent ⇒ mesh_locate/`obi mesh` answer "not configured"
    # and nothing else changes.
    meshtastic: MeshtasticSettings | None = None
    # feature 135: absent section ⇒ empty declaration (nothing in maintenance).
    maintenance: MaintenanceSettings = field(default_factory=MaintenanceSettings)


def default_config_path() -> Path:
    """XDG config location: ``$XDG_CONFIG_HOME/obi/config.toml`` (``~/.config`` default)."""

    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "obi" / "config.toml"


def default_audit_path() -> Path:
    """XDG state location: ``$XDG_STATE_HOME/obi/audit.jsonl`` (``~/.local/state`` default)."""

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "audit.jsonl"


def default_inventory_path() -> Path:
    """XDG state location for the persisted inventory (feature 002, FR-010).

    ``$XDG_STATE_HOME/obi/inventory.json`` (``~/.local/state`` default), beside the audit log.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "inventory.json"


def _warn_if_insecure_perms(path: Path) -> str | None:
    """Return a warning string if the config is group/other-readable; ``None`` otherwise.

    Skips the check on platforms without POSIX permission bits.
    """

    if os.name != "posix":
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return (
            f"config file {path} is readable by group/other (mode {mode:04o}); "
            f"it stores a plaintext password — run: chmod 600 {path}"
        )
    return None


def load_config(path: Path) -> Config:
    """Load and validate config from ``path``.

    Raises :class:`ConfigError` on a missing/unreadable file, an empty discovery scope, or
    missing Mikrotik credentials. Returns the warning about insecure file permissions on the
    config object's behalf via the logger is left to the caller; here we surface it as part
    of validation only when the file is unreadable.
    """

    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            "Create it with a [routeros] and/or [uisp] vendor section "
            "(see quickstart.md)."
        )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from exc

    routeros_data = data.get("routeros")
    uisp_data = data.get("uisp")
    netbox_data = data.get("netbox")
    unifi_network_data = data.get("unifi-network")
    unifi_sm_data = data.get("unifi-sm")
    unifi_internal_data = data.get("unifi-internal")
    logs_data = data.get("logs")
    # feature 020: [siklu] with a non-empty hosts roster is a first-class source (a Siklu-only
    # config is valid). An empty/missing roster doesn't count (it's a clean "no radios", FR-010).
    siklu_data = data.get("siklu")
    siklu_is_source = isinstance(siklu_data, dict) and bool(siklu_data.get("hosts"))
    # [logs] is a first-class source: a logs-only config is valid (so `obi logs ingest`/`query`
    # and a logs-only chat work). An empty [logs] section ({}) still counts — hence `is not None`.
    if not (
        routeros_data
        or uisp_data
        or netbox_data
        or unifi_network_data
        or unifi_sm_data
        or unifi_internal_data
        or logs_data is not None
        or siklu_is_source
    ):
        raise ConfigError(
            "no source configured: add a [routeros], [uisp], [netbox], [unifi-network], "
            "[unifi-sm], [unifi-internal], [logs], and/or [siklu] section"
        )

    # The discovery scope is required ONLY when Mikrotik is configured (FR-007); a UISP-only
    # config needs no scope — the controller is the source.
    scope: DiscoveryScope | None = None
    routeros: RouterosSettings | None = None
    if routeros_data:
        if "username" not in routeros_data or "password" not in routeros_data:
            raise ConfigError("[routeros] section requires both 'username' and 'password'")
        discovery = data.get("discovery", {})
        ranges = [str(r) for r in discovery.get("ranges", [])]
        seeds = [str(s) for s in discovery.get("seeds", [])]
        try:
            scope = DiscoveryScope(ranges=ranges, seeds=seeds)
        except ValueError as exc:
            raise ConfigError(
                "discovery scope is empty: configure [discovery] ranges or seeds"
            ) from exc
        api_port_raw = routeros_data.get("api_port")
        routeros = RouterosSettings(
            username=str(routeros_data["username"]),
            password=Secret(str(routeros_data["password"])),
            use_tls=bool(routeros_data.get("use_tls", True)),
            api_port=int(api_port_raw) if api_port_raw is not None else None,
        )
    # feature 028: [routeros.live] tunes the live-tool caps (safe defaults when absent).
    routeros_live = _parse_routeros_live(
        routeros_data.get("live") if isinstance(routeros_data, dict) else None
    )

    uisp: UispSettings | None = None
    if uisp_data:
        if "base_url" not in uisp_data or "token" not in uisp_data:
            raise ConfigError("[uisp] section requires both 'base_url' and 'token'")
        uisp_timeout = uisp_data.get("timeout")
        uisp_filter = uisp_data.get("name_filter")
        uisp_min_level = uisp_data.get("min_level")
        uisp_ignore_raw = uisp_data.get("ignore_names")
        if isinstance(uisp_ignore_raw, list):
            uisp_ignore_names = [str(pattern) for pattern in uisp_ignore_raw]
        elif uisp_ignore_raw is None:
            uisp_ignore_names = []
        else:
            uisp_ignore_names = [str(uisp_ignore_raw)]
        uisp_sanity_raw = uisp_data.get("location_sanity_m")
        if uisp_sanity_raw is None:
            uisp_location_sanity_m = 75.0
        else:
            try:
                uisp_location_sanity_m = float(uisp_sanity_raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    "[uisp] location_sanity_m must be a number (meters); "
                    "0 or negative disables the site-anchoring correction"
                ) from exc
        uisp = UispSettings(
            base_url=str(uisp_data["base_url"]).rstrip("/"),
            token=Secret(str(uisp_data["token"])),
            verify_tls=bool(uisp_data.get("verify_tls", True)),
            timeout=float(uisp_timeout) if uisp_timeout is not None else 15.0,
            name_filter=str(uisp_filter) if uisp_filter else None,
            ignore_names=uisp_ignore_names,
            ingest_events=bool(uisp_data.get("ingest_events", False)),
            ingest_outages=bool(uisp_data.get("ingest_outages", False)),
            min_level=str(uisp_min_level).strip().lower() if uisp_min_level else None,
            first_run_lookback=str(uisp_data.get("first_run_lookback", "30d")),
            location_sanity_m=uisp_location_sanity_m,
        )

    netbox: NetboxSettings | None = None
    if netbox_data:
        if "base_url" not in netbox_data or "token" not in netbox_data:
            raise ConfigError("[netbox] section requires both 'base_url' and 'token'")
        netbox_timeout = netbox_data.get("timeout")
        netbox_filter = netbox_data.get("name_filter")
        netbox = NetboxSettings(
            base_url=str(netbox_data["base_url"]).rstrip("/"),
            token=Secret(str(netbox_data["token"])),
            verify_tls=bool(netbox_data.get("verify_tls", True)),
            timeout=float(netbox_timeout) if netbox_timeout is not None else 15.0,
            name_filter=str(netbox_filter) if netbox_filter else None,
        )

    # feature 016: [unifi] holds the connection coordinates common to ONE console, shared by the
    # official api-key surface ([unifi-network], 011) and the reverse-engineered surface
    # ([unifi-internal], 016). For back-compat a config with only [unifi-network] (no [unifi]) still
    # resolves base_url/sites/verify_tls/timeout/name_filter from [unifi-network] (FR-012).
    _unifi_common_raw = data.get("unifi")
    unifi_common_data: dict[str, Any] = (
        _unifi_common_raw if isinstance(_unifi_common_raw, dict) else {}
    )

    def _unifi_common(key: str, default: Any = None) -> Any:
        if key in unifi_common_data:
            return unifi_common_data[key]
        if isinstance(unifi_network_data, dict) and key in unifi_network_data:
            return unifi_network_data[key]
        return default

    def _unifi_sites(raw: object) -> list[str]:
        if isinstance(raw, list):
            return [str(s) for s in raw]
        return [str(raw)] if raw not in (None, "") else []

    # feature 011: per-console UniFi Network Integration API. Requires base_url plus at least one
    # credential (the official integration api_key and/or the controller-API token). `sites` selects
    # one, several, or all sites (an empty list, the default).
    unifi_network: UnifiNetworkSettings | None = None
    if unifi_network_data:
        net_base_url = _unifi_common("base_url")
        if not net_base_url:
            raise ConfigError(
                "[unifi-network] requires 'base_url' (set it in [unifi] or [unifi-network])"
            )
        if not unifi_network_data.get("api_key") and not unifi_network_data.get("token"):
            raise ConfigError(
                "[unifi-network] section requires at least one of 'api_key' (integration API) "
                "or 'token' (controller API)"
            )
        un_timeout = _unifi_common("timeout")
        un_filter = _unifi_common("name_filter")
        unifi_network = UnifiNetworkSettings(
            base_url=str(net_base_url).rstrip("/"),
            api_key=(
                Secret(str(unifi_network_data["api_key"]))
                if unifi_network_data.get("api_key")
                else None
            ),
            token=(
                Secret(str(unifi_network_data["token"]))
                if unifi_network_data.get("token")
                else None
            ),
            sites=_unifi_sites(_unifi_common("sites", [])),
            verify_tls=bool(_unifi_common("verify_tls", True)),
            timeout=float(un_timeout) if un_timeout is not None else 15.0,
            name_filter=str(un_filter) if un_filter else None,
        )

    # feature 016: reverse-engineered controller API credentials (console-local admin). Additive;
    # requires base_url (from [unifi]/[unifi-network]) + username+password; inherits the common
    # coordinates so one console is configured once. obi uses the credential strictly read-only.
    unifi_internal: UnifiInternalSettings | None = None
    if unifi_internal_data:
        int_base_url = _unifi_common("base_url")
        if not int_base_url:
            raise ConfigError(
                "[unifi-internal] requires 'base_url' (set it in [unifi] or [unifi-network])"
            )
        if not unifi_internal_data.get("username") or not unifi_internal_data.get("password"):
            raise ConfigError(
                "[unifi-internal] section requires both 'username' and 'password' "
                "(a console-local admin account)"
            )
        ui_timeout = _unifi_common("timeout")
        cats_raw = unifi_internal_data.get("categories")
        categories = (
            [str(c).upper() for c in cats_raw]
            if isinstance(cats_raw, list) and cats_raw
            else list(DEFAULT_UNIFI_EVENT_CATEGORIES)
        )
        unifi_internal = UnifiInternalSettings(
            base_url=str(int_base_url).rstrip("/"),
            username=Secret(str(unifi_internal_data["username"])),
            password=Secret(str(unifi_internal_data["password"])),
            sites=_unifi_sites(_unifi_common("sites", [])),
            verify_tls=bool(_unifi_common("verify_tls", True)),
            timeout=float(ui_timeout) if ui_timeout is not None else 15.0,
            categories=categories,
            retention_window=str(unifi_internal_data.get("retention_window", "7d")),
        )

    # feature 011: account-level UniFi Site Manager API. Requires api_key; base_url defaults to the
    # public endpoint (api.ui.com).
    unifi_sm: UnifiSiteManagerSettings | None = None
    if unifi_sm_data:
        if not unifi_sm_data.get("api_key"):
            raise ConfigError("[unifi-sm] section requires 'api_key'")
        sm_timeout = unifi_sm_data.get("timeout")
        sm_filter = unifi_sm_data.get("name_filter")
        unifi_sm = UnifiSiteManagerSettings(
            api_key=Secret(str(unifi_sm_data["api_key"])),
            base_url=str(unifi_sm_data.get("base_url", "https://api.ui.com")).rstrip("/"),
            verify_tls=bool(unifi_sm_data.get("verify_tls", True)),
            timeout=float(sm_timeout) if sm_timeout is not None else 15.0,
            name_filter=str(sm_filter) if sm_filter else None,
        )

    # feature 013: the local syslog store section is optional and additive. Both paths default to
    # XDG state; an empty [logs] section is valid (uses every default). obi only READS the mirror.
    logs: LogsSettings | None = None
    if logs_data is not None:
        logs = LogsSettings(
            mirror_root=Path(
                str(logs_data.get("mirror_root", default_logs_mirror_root()))
            ).expanduser(),
            db_path=Path(str(logs_data.get("db_path", default_logs_db_path()))).expanduser(),
            default_limit=int(logs_data.get("default_limit", 200)),
            max_limit=int(logs_data.get("max_limit", 2000)),
            timeout=float(logs_data.get("timeout", 5.0)),
            log_tz=str(logs_data.get("log_tz", "UTC")),
            collectors=[str(c) for c in logs_data.get("collectors", [])],
        )

    audit_path = (
        Path(str(data["audit"]["path"]))
        if isinstance(data.get("audit"), dict) and "path" in data["audit"]
        else default_audit_path()
    )
    audit = _parse_audit(data.get("audit"))
    inventory_path = (
        Path(str(data["inventory"]["path"])).expanduser()
        if isinstance(data.get("inventory"), dict) and "path" in data["inventory"]
        else default_inventory_path()
    )

    chat_data = data.get("chat", {})
    chat = ChatSettings(
        api_key=Secret(str(chat_data["api_key"])) if chat_data.get("api_key") else None,
        model=str(chat_data.get("model", "claude-sonnet-5")),
        max_tokens=int(chat_data.get("max_tokens", 1024)),
        context_file=(
            Path(str(chat_data["context_file"])).expanduser()
            if chat_data.get("context_file")
            else None
        ),
    )

    # feature 009: the Slack interface section is optional; when present it requires both tokens
    # and a non-empty allowlist of Slack user IDs. Feature 104 adds a second authorization
    # source: optional channel IDs whose current members are also authorized.
    slack_data = data.get("slack")
    slack: SlackSettings | None = None
    if slack_data:
        if "bot_token" not in slack_data or "app_token" not in slack_data:
            raise ConfigError("[slack] section requires both 'bot_token' and 'app_token'")
        allow_raw = slack_data.get("allowlist")
        allowlist = [str(user) for user in allow_raw] if isinstance(allow_raw, list) else []
        if not allowlist:
            raise ConfigError("[slack] section requires a non-empty 'allowlist' of Slack user IDs")
        channels_raw = slack_data.get("authorized_channels", [])
        if not isinstance(channels_raw, list) or not all(
            isinstance(channel, str) and channel for channel in channels_raw
        ):
            raise ConfigError(
                "[slack] 'authorized_channels' must be a list of Slack conversation IDs"
            )
        slack = SlackSettings(
            bot_token=Secret(str(slack_data["bot_token"])),
            app_token=Secret(str(slack_data["app_token"])),
            allowlist=allowlist,
            authorized_channels=list(channels_raw),
        )

    snmp = _parse_snmp(data.get("snmp"))

    siklu = _parse_siklu(data.get("siklu"))

    watch = _parse_watch(data.get("watch"))
    # Cross-section rule (feature 034): the UISP link pass reads the controller live, so enabling
    # it without a [uisp] section is a config mistake — fail loud at load, not silently in the
    # cron. Checked here, where both [watch] and [uisp] are visible.
    if watch.uisp_links is not None and watch.uisp_links.enabled and uisp is None:
        raise ConfigError("[watch.uisp_links] requires a [uisp] section (base_url + token)")
    # Cross-section rule (FR-013): opting into Slack delivery requires a usable [slack] bot token.
    # Checked here, where both [watch] and [slack] are visible (not in _parse_watch, which sees
    # only [watch]). Delivery uses only the bot token at runtime; the app token is not required.
    if watch.slack_notify and (slack is None or not slack.bot_token.reveal()):
        raise ConfigError(
            "[watch] slack_notify is true but no usable [slack] bot_token is configured"
        )

    # 033: hard section rename [ar] → [web] (fleet precedent: [mikrotik] → [routeros]).
    # Unknown sections are otherwise ignored, but silently ignoring a leftover [ar] would
    # silently drop the operator's port/voice/GNIS settings — fail loudly instead.
    if "ar" in data:
        raise ConfigError(
            "[ar] was renamed to [web] (feature 033) — rename the section in your config; "
            "its keys are unchanged"
        )
    web = _parse_web(data.get("web"))

    voice = _parse_voice(data.get("voice"))

    # feature 026: read-only RouterOS config backup. Opt-in via [configs]; SSH creds resolve from
    # [configs] or fall back to the [routeros] account (needs both parsed, hence here).
    configs = _parse_config_backup(data.get("configs"), routeros)

    connection = data.get("connection", {})
    icmp = _parse_icmp(data.get("icmp"))
    maintenance = parse_maintenance(data.get("maintenance"))
    weather = _parse_weather(data.get("weather"))
    traffic = _parse_traffic(data.get("traffic"))
    brc = _parse_brc(data.get("brc"))
    playa = _parse_playa(data.get("playa"))
    site = _parse_site(data.get("site"))
    if isinstance(data.get("web"), dict) and "brc" in data["web"]:
        web.brc = bool(data["web"]["brc"])
    else:
        web.brc = brc is not None
    service = _parse_service(data.get("service"))

    health_data = data.get("health")
    health_linux = _parse_health_linux(
        health_data.get("linux") if isinstance(health_data, dict) else None
    )

    dash = _parse_dash(data.get("dash"))
    wled = _parse_wled(data.get("wled"))
    meshtastic = _parse_meshtastic(data.get("meshtastic"))
    victoria = _parse_victoria(data.get("victoria"))
    # Cross-section rule (feature 095): the dash pager delivers over the watch Slack DM path, so
    # enabling it without a usable [slack] bot token is a config mistake — fail loud at load,
    # never silently in the minute cron (the [watch] slack_notify precedent).
    # Slack is the pager's only wired transport today; feature 102 (playa paging) relaxes this
    # to "Slack OR a configured paging transport" when it lands its secondary channel.
    if dash is not None and dash.pager.enabled and (slack is None or not slack.bot_token.reveal()):
        raise ConfigError(
            "[dash.pager] enabled is true but no usable [slack] bot_token is configured"
        )

    # Cross-section rule (feature 128): the status-band reconciler is an autonomous device
    # write — enabling it without any [wled.segments.<device>] layout is a config mistake
    # that must fail at load, never silently no-op in the minute cron.
    if dash is not None and dash.status_band.enabled and (wled is None or not wled.segments):
        raise ConfigError(
            "[dash.status_band] enabled is true but no [wled.segments.<device>] layout is "
            "declared — the reconciler needs to know which segments are the status bands"
        )

    paging = _parse_paging(data.get("paging"))

    radio = _parse_radio(data.get("radio"))
    # Cross-section rule (feature 088): TX and shadow-TX compose on-air replies with the cloud
    # model, so enabling either without a resolvable Anthropic key is a config mistake — fail at
    # load, never mid-call on the air. The pure listener stays fully local and needs no key.
    if (
        radio is not None
        and (radio.tx_enabled or radio.shadow_tx)
        and chat.api_key is None
        and not os.environ.get("ANTHROPIC_API_KEY")
    ):
        raise ConfigError(
            "[radio] tx_enabled/shadow_tx compose replies with the cloud model but no "
            "[chat] api_key is configured and ANTHROPIC_API_KEY is unset"
        )

    # Cross-section SAFETY rule (feature 121): the mesh driver must never open a serial
    # device the radio station claims — opening it asserts DTR+RTS and keys the transmitter
    # (radio/ptt.py). This is resolved HERE, the one place both sections are in scope, and
    # handed to the mesh settings as data, so the mesh driver needs no import of obi.radio
    # (contract G7). No [radio] section ⇒ empty exclusion ⇒ mesh behaves exactly as before.
    if meshtastic is not None and radio is not None:
        meshtastic = replace(
            meshtastic,
            excluded_ports=tuple(
                sorted(radio_devices.claimed_port_candidates(radio.ftdi_serial, radio.serial_port))
            ),
        )

    return Config(
        scope=scope,
        routeros=routeros,
        routeros_live=routeros_live,
        uisp=uisp,
        netbox=netbox,
        unifi_network=unifi_network,
        unifi_sm=unifi_sm,
        unifi_internal=unifi_internal,
        logs=logs,
        timeout=float(connection.get("timeout", 4.0)),
        concurrency=int(connection.get("concurrency", 16)),
        budget=float(connection.get("budget", 30.0)),
        audit_path=audit_path,
        inventory_path=inventory_path,
        resolve_link_local_via_ospf=bool(
            data.get("discovery", {}).get("resolve_link_local_via_ospf", False)
        ),
        chat=chat,
        slack=slack,
        snmp=snmp,
        watch=watch,
        siklu=siklu,
        web=web,
        voice=voice,
        configs=configs,
        audit=audit,
        icmp=icmp,
        service=service,
        radio=radio,
        health_linux=health_linux,
        dash=dash,
        paging=paging,
        wled=wled,
        weather=weather,
        traffic=traffic,
        brc=brc,
        playa=playa,
        site=site,
        victoria=victoria,
        meshtastic=meshtastic,
        maintenance=maintenance,
    )


def _parse_service(service_data: object) -> ServiceSettings:
    """Parse the ``[service]`` section (feature 081); all-default when absent.

    ``environment`` values are coerced to strings (plist ``EnvironmentVariables`` are strings);
    a non-mapping ``environment`` is ignored rather than raising, keeping the section forgiving.
    """

    if not isinstance(service_data, dict):
        return ServiceSettings()
    env_raw = service_data.get("environment", {})
    environment = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
    return ServiceSettings(
        run_at_login=bool(service_data.get("run_at_login", True)),
        environment=environment,
    )


def _parse_audit(audit_data: object) -> AuditSettings:
    """Parse the ``[audit]`` section (feature 042); all-default when absent.

    The pre-042 ``path`` key (the JSONL trail location) is parsed separately into
    ``Config.audit_path`` and is ignored here.
    """

    if not isinstance(audit_data, dict):
        return AuditSettings()
    retention_days = int(audit_data.get("retention_days", 90))
    if retention_days < 1:
        raise ConfigError(f"[audit] retention_days must be >= 1 (got {retention_days})")
    max_payload_bytes = int(audit_data.get("max_payload_bytes", 500_000))
    if max_payload_bytes < 4_096:
        raise ConfigError(f"[audit] max_payload_bytes must be >= 4096 (got {max_payload_bytes})")
    db_raw = audit_data.get("db_path")
    return AuditSettings(
        enabled=bool(audit_data.get("enabled", True)),
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_audit_db_path(),
        retention_days=retention_days,
        max_payload_bytes=max_payload_bytes,
    )


def _parse_config_backup(
    configs_data: object, routeros: RouterosSettings | None
) -> ConfigBackupSettings | None:
    """Parse the optional ``[configs]`` section (feature 026); ``None`` when absent.

    Presence opts into config backup. SSH credentials come from ``[configs]`` (``username`` +
    ``password``) when both are given, else are inherited from ``[routeros]``; a ``[configs]`` with
    neither its own creds nor a ``[routeros]`` section to inherit from is a :class:`ConfigError`.
    """

    if not isinstance(configs_data, dict):
        return None

    username_raw = configs_data.get("username")
    password_raw = configs_data.get("password")
    if username_raw and password_raw is not None:
        username = str(username_raw)
        password = Secret(str(password_raw))
    elif routeros is not None:
        username = routeros.username
        password = routeros.password
    else:
        raise ConfigError(
            "[configs] needs SSH credentials: add 'username' and 'password', or a [routeros] "
            "section to inherit from (its account needs the RouterOS 'ssh' group policy)."
        )

    repo_raw = configs_data.get("repo_path")
    concurrency = int(configs_data.get("concurrency", 8))
    if concurrency < 1:
        raise ConfigError(f"[configs] concurrency must be at least 1 (got {concurrency})")
    ssh_port = int(configs_data.get("ssh_port", 22))
    if not 1 <= ssh_port <= 65535:
        raise ConfigError(f"[configs] ssh_port must be between 1 and 65535 (got {ssh_port})")
    return ConfigBackupSettings(
        username=username,
        password=password,
        ssh_port=ssh_port,
        timeout=float(configs_data.get("timeout", 10.0)),
        concurrency=concurrency,
        repo_path=Path(str(repo_raw)).expanduser() if repo_raw else default_config_repo_dir(),
        targets=str(configs_data.get("targets", "")),
        terse=bool(configs_data.get("terse", True)),
        verbose=bool(configs_data.get("verbose", False)),
    )


# 097: "<number> <unit>" tower heights — the operator quotes big towers in feet and small
# masts in meters, so both units are accepted; a unitless string is ambiguous and rejected.
_TOWER_HEIGHT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ft|feet|m)\s*$", re.IGNORECASE)
_METERS_PER_FOOT = 0.3048  # international foot, exact


def _parse_tower_height(site: str, value: object) -> float:
    """One ``[web] site_towers`` value → meters above site ground (097).

    A bare number is meters; a string must carry a unit (``"60 ft"``, ``"18.3 m"``). Raises
    :class:`ConfigError` naming the entry for any other shape or a non-positive height.
    """

    if isinstance(value, bool):
        raise ConfigError(f"[web] site_towers[{site!r}] must be a height, not a boolean")
    if isinstance(value, (int, float)):
        meters = float(value)
    elif isinstance(value, str):
        match = _TOWER_HEIGHT_RE.match(value)
        if match is None:
            raise ConfigError(
                f"[web] site_towers[{site!r}] must be meters or a unit-suffixed string "
                f'like "60 ft" or "18.3 m" (got {value!r})'
            )
        meters = float(match.group(1))
        if match.group(2).lower() in ("ft", "feet"):
            meters *= _METERS_PER_FOOT
    else:
        raise ConfigError(
            f"[web] site_towers[{site!r}] must be a number of meters or a unit-suffixed "
            f"string (got {value!r})"
        )
    if meters <= 0:
        raise ConfigError(f"[web] site_towers[{site!r}] must be a positive height (got {value!r})")
    return meters


def _parse_web(web_data: object) -> WebSettings:
    """Parse the optional ``[web]`` section (022's ``[ar]``, renamed by 033); defaults when
    absent/empty.

    ``ground_overrides`` is a TOML table of ``site name = elevation_m``; a non-numeric value
    raises :class:`ConfigError` (a silently-dropped override would quietly mis-place a site).
    ``site_towers`` (097) is a TOML table of ``site name = tower height`` — a bare number is
    meters, a string must carry a unit (``"60 ft"``, ``"18.3 m"``; feet convert exactly at
    0.3048 m/ft); anything else, and any height that is not positive, raises
    :class:`ConfigError` naming the entry (a silently-dropped tower would quietly scatter a
    site's radios again).
    """

    if not isinstance(web_data, dict):
        return WebSettings()
    overrides: dict[str, float] = {}
    overrides_raw = web_data.get("ground_overrides")
    if isinstance(overrides_raw, dict):
        for site, value in overrides_raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(
                    f"[web] ground_overrides[{site!r}] must be a number of meters (got {value!r})"
                )
            overrides[str(site)] = float(value)
    site_towers: dict[str, float] = {}
    towers_raw = web_data.get("site_towers")
    if towers_raw is not None and not isinstance(towers_raw, dict):
        raise ConfigError(
            '[web] site_towers must be a table of site name = height (e.g. "Showers" = "100 ft")'
        )
    if isinstance(towers_raw, dict):
        for site, value in towers_raw.items():
            site_towers[str(site)] = _parse_tower_height(str(site), value)
    gnis_states: list[str] = []
    gnis_raw = web_data.get("gnis_states")
    if gnis_raw is not None:
        if not isinstance(gnis_raw, list) or not all(
            isinstance(state, str) and len(state.strip()) == 2 and state.strip().isalpha()
            for state in gnis_raw
        ):
            raise ConfigError(
                '[web] gnis_states must be a list of two-letter state codes (e.g. ["NV"])'
            )
        gnis_states = [str(state).strip().upper() for state in gnis_raw]
    layout_raw = web_data.get("ospf_layout_path")
    if layout_raw is not None and not isinstance(layout_raw, str):
        raise ConfigError(f"[web] ospf_layout_path must be a path string (got {layout_raw!r})")
    export_raw = web_data.get("export_path")
    return WebSettings(
        export_path=(
            Path(str(export_raw)).expanduser() if export_raw else default_web_dir() / "links.json"
        ),
        amsl_tolerance=float(web_data.get("amsl_tolerance", 150.0)),
        default_mast_height=float(web_data.get("default_mast_height", 10.0)),
        default_ground_elevation=float(web_data.get("default_ground_elevation", 0.0)),
        degraded_link_score=float(web_data.get("degraded_link_score", 0.5)),
        ground_overrides=overrides,
        port=int(web_data.get("port", 8443)),
        bind=str(web_data.get("bind", "0.0.0.0")),
        gnis_states=gnis_states,
        voice=bool(web_data.get("voice", True)),  # 048: on by default; voice=false opts out
        include_siklu=bool(web_data.get("include_siklu", True)),
        ospf_layout_path=Path(layout_raw).expanduser() if layout_raw else None,
        site_towers=site_towers,
        brc=bool(web_data.get("brc", False)),  # 137: load_config defaults it to [brc] presence
        ospf_sources=[str(x) for x in (web_data.get("ospf_sources") or [])],
    )


def _parse_voice(voice_data: object) -> VoiceSettings:
    """Parse the optional ``[voice]`` section (feature 023); all defaults when absent/empty."""

    if not isinstance(voice_data, dict):
        return VoiceSettings()
    stt_model = str(voice_data.get("stt_model", "mlx-community/whisper-large-v3-turbo"))
    if not stt_model:
        raise ConfigError("[voice] stt_model must be a non-empty HuggingFace model id")
    tts_voice_raw = voice_data.get("tts_voice")
    tts_rate: int | None = None
    if "tts_rate" in voice_data:
        tts_rate = int(voice_data["tts_rate"])
        if tts_rate <= 0:
            raise ConfigError(
                f"[voice] tts_rate must be a positive words-per-minute (got {tts_rate})"
            )
    rms_threshold = float(voice_data.get("rms_threshold", 0.01))
    if not 0.0 < rms_threshold < 1.0:
        raise ConfigError(
            f"[voice] rms_threshold must be between 0 and 1 exclusive (got {rms_threshold})"
        )
    # 033: the standalone talk page (8444) is retired — the voice surface now lives on the
    # unified [web] origin. A leftover hosting key would silently do nothing; fail loudly.
    if "web_port" in voice_data:
        raise ConfigError(
            "[voice] web_port was retired by feature 033 — the talk page merged into the "
            "unified web origin; its port is [web] port (default 8443)"
        )
    if "web_bind" in voice_data:
        raise ConfigError(
            "[voice] web_bind was retired by feature 033 — the talk page merged into the "
            "unified web origin; its bind address is [web] bind"
        )
    web_idle_minutes = int(voice_data.get("web_idle_minutes", 30))
    if web_idle_minutes < 1:
        raise ConfigError(
            f"[voice] web_idle_minutes must be a positive minute count (got {web_idle_minutes})"
        )
    web_max_seconds = int(voice_data.get("web_max_seconds", 90))
    if not 1 <= web_max_seconds <= 600:
        raise ConfigError(
            f"[voice] web_max_seconds must be between 1 and 600 (got {web_max_seconds})"
        )
    return VoiceSettings(
        stt_model=stt_model,
        tts_voice=str(tts_voice_raw) if tts_voice_raw else None,
        tts_rate=tts_rate,
        rms_threshold=rms_threshold,
        web_idle_minutes=web_idle_minutes,
        web_max_seconds=web_max_seconds,
    )


def _parse_routeros_live(live_data: object) -> RouterosLiveSettings:
    """Parse the optional ``[routeros.live]`` subsection (feature 028); all defaults when absent.

    Positive-integer / positive-float coercion with clamping to sane floors, so a fat-fingered
    ``max_records = 0`` can't disable truncation nor a negative timeout wedge a call.
    """

    if not isinstance(live_data, dict):
        return RouterosLiveSettings()
    defaults = RouterosLiveSettings()
    timeout_raw = live_data.get("timeout_s")
    return RouterosLiveSettings(
        max_records=max(1, int(live_data.get("max_records", defaults.max_records))),
        max_output_chars=max(
            500, int(live_data.get("max_output_chars", defaults.max_output_chars))
        ),
        ping_count=max(1, int(live_data.get("ping_count", defaults.ping_count))),
        ping_count_max=max(1, int(live_data.get("ping_count_max", defaults.ping_count_max))),
        traceroute_max_probes=max(
            1, int(live_data.get("traceroute_max_probes", defaults.traceroute_max_probes))
        ),
        traceroute_deadline_s=max(
            1.0, float(live_data.get("traceroute_deadline_s", defaults.traceroute_deadline_s))
        ),
        timeout_s=(
            float(timeout_raw) if isinstance(timeout_raw, (int, float)) and timeout_raw else None
        ),
    )


def _parse_watch(watch_data: object) -> WatchSettings:
    """Parse the optional ``[watch]`` section (feature 014); all defaults when absent/empty."""

    if not isinstance(watch_data, dict):
        return WatchSettings()
    model_raw = watch_data.get("model")
    watch_dir_raw = watch_data.get("watch_dir")
    ignore_raw = watch_data.get("ignore_topics")
    ignore_topics = [str(t) for t in ignore_raw] if isinstance(ignore_raw, list) else []
    collectors_raw = watch_data.get("ignore_collectors")
    ignore_collectors = [str(c) for c in collectors_raw] if isinstance(collectors_raw, list) else []
    # Mirrors obi.core.watch.SEVERITIES; inlined to avoid a config→watch import cycle.
    slack_min_severity = str(watch_data.get("slack_min_severity", "warning")).lower()
    if slack_min_severity not in ("info", "warning", "critical"):
        raise ConfigError(
            "[watch] slack_min_severity must be one of info/warning/critical "
            f"(got {slack_min_severity!r})"
        )
    slack_user_raw = watch_data.get("slack_user")

    def _non_negative(key: str, default: int) -> int:
        value = int(watch_data.get(key, default))
        if value < 0:
            raise ConfigError(f"[watch] {key} must be >= 0 (got {value})")
        return value

    return WatchSettings(
        enabled=bool(watch_data.get("enabled", False)),
        investigation_max_steps=int(watch_data.get("investigation_max_steps", 4)),
        max_window_events=int(watch_data.get("max_window_events", 2000)),
        retention_days=int(watch_data.get("retention_days", 30)),
        watch_dir=Path(str(watch_dir_raw)).expanduser() if watch_dir_raw else default_watch_dir(),
        model=str(model_raw) if model_raw else None,
        max_tokens=int(watch_data.get("max_tokens", 2048)),
        dedup=bool(watch_data.get("dedup", True)),
        dedup_ttl_hours=int(watch_data.get("dedup_ttl_hours", 6)),
        routeros_only=bool(watch_data.get("routeros_only", True)),
        ignore_topics=ignore_topics,
        ignore_collectors=ignore_collectors,
        slack_notify=bool(watch_data.get("slack_notify", False)),
        slack_min_severity=slack_min_severity,
        slack_user=str(slack_user_raw) if slack_user_raw else None,
        metrics=_parse_watch_metrics(watch_data.get("metrics")),
        reachability=_parse_watch_reachability(watch_data.get("reachability")),
        uisp_links=_parse_watch_uisp_links(watch_data.get("uisp_links")),
        config_changes=_parse_watch_config_changes(watch_data.get("config_changes")),
        icmp=_parse_watch_icmp(watch_data.get("icmp")),
        config_captures=_parse_watch_config_captures(watch_data.get("config_captures")),
        health=_parse_watch_health(watch_data.get("health")),
        investigation_live_call_budget=int(watch_data.get("investigation_live_call_budget", 8)),
        lag_alarm_minutes=_non_negative("lag_alarm_minutes", 30),
        lag_alarm_events=_non_negative("lag_alarm_events", 2000),
        heartbeat_hours=_non_negative("heartbeat_hours", 24),
    )


_KNOWN_ERROR_COUNTERS = ("ifInErrors", "ifOutErrors", "ifInDiscards", "ifOutDiscards")


def duration_to_seconds(value: object, default: int, *, context: str = "duration") -> int:
    """Parse a window given as ``"1h"``/``"30m"``/``"2d"`` (or bare seconds) → seconds."""

    if value is None:
        return default
    text = str(value).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(text) > 1 and text[-1] in units and text[:-1].isdigit():
        return int(text[:-1]) * units[text[-1]]
    if text.isdigit():
        return int(text)
    raise ConfigError(f"{context}: bad duration {value!r}")


def _duration_to_seconds(value: object, default: int) -> int:
    return duration_to_seconds(value, default, context="[watch.metrics] window")


def _parse_watch_metrics(metrics_data: object) -> WatchMetricsSettings | None:
    """Parse the optional ``[watch.metrics]`` subsection (feature 027); ``None`` when absent."""

    if not isinstance(metrics_data, dict):
        return None

    counters_raw = metrics_data.get("counters")
    if counters_raw is None:
        counters = ["ifInErrors", "ifOutErrors"]
    elif isinstance(counters_raw, list):
        counters = [str(c) for c in counters_raw]
    else:
        counters = [str(counters_raw)]
    if not counters:
        raise ConfigError("[watch.metrics] counters must name at least one counter")
    for counter in counters:
        if counter not in _KNOWN_ERROR_COUNTERS:
            raise ConfigError(
                f"[watch.metrics] unknown counter {counter!r} — choose from "
                f"{', '.join(_KNOWN_ERROR_COUNTERS)}"
            )

    threshold = int(metrics_data.get("threshold", 0))
    if threshold < 0:
        raise ConfigError(f"[watch.metrics] threshold must be >= 0 (got {threshold})")

    ignore_raw = metrics_data.get("ignore_interfaces")
    if ignore_raw is None:
        ignore_interfaces = ["wireguard"]
    elif isinstance(ignore_raw, list):
        ignore_interfaces = [str(i) for i in ignore_raw]
    else:
        ignore_interfaces = [str(ignore_raw)]

    db_raw = metrics_data.get("db_path")
    return WatchMetricsSettings(
        enabled=bool(metrics_data.get("enabled", False)),
        threshold=threshold,
        window_seconds=_duration_to_seconds(metrics_data.get("window"), 3600),
        counters=counters,
        ignore_interfaces=ignore_interfaces,
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_metrics_db_path(),
    )


def _parse_watch_reachability(data: object) -> WatchReachabilitySettings | None:
    """Parse the optional ``[watch.reachability]`` subsection (feature 030); ``None`` if absent."""

    if not isinstance(data, dict):
        return None

    stale_after_cycles = int(data.get("stale_after_cycles", 2))
    if stale_after_cycles < 1:
        raise ConfigError(
            f"[watch.reachability] stale_after_cycles must be >= 1 (got {stale_after_cycles})"
        )

    mass_outage_ratio = float(data.get("mass_outage_ratio", 0.80))
    if not 0.0 < mass_outage_ratio <= 1.0:
        raise ConfigError(
            f"[watch.reachability] mass_outage_ratio must be in (0, 1] (got {mass_outage_ratio})"
        )

    ignore_raw = data.get("ignore_devices")
    if isinstance(ignore_raw, list):
        ignore_devices = [str(d) for d in ignore_raw]
    elif ignore_raw is None:
        ignore_devices = []
    else:
        ignore_devices = [str(ignore_raw)]

    db_raw = data.get("db_path")
    return WatchReachabilitySettings(
        enabled=bool(data.get("enabled", False)),
        stale_after_cycles=stale_after_cycles,
        mass_outage_ratio=mass_outage_ratio,
        ignore_devices=ignore_devices,
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_metrics_db_path(),
    )


def _parse_watch_config_captures(data: object) -> WatchConfigCapturesSettings | None:
    """Parse the optional ``[watch.config_captures]`` subsection (087); ``None`` if absent."""

    if not isinstance(data, dict):
        return None

    stale_after = str(data.get("stale_after", "1d"))
    duration_to_seconds(stale_after, 86400, context="[watch.config_captures] stale_after")

    mass_outage_ratio = float(data.get("mass_outage_ratio", 0.80))
    if not 0.0 < mass_outage_ratio <= 1.0:
        raise ConfigError(
            f"[watch.config_captures] mass_outage_ratio must be in (0, 1] (got {mass_outage_ratio})"
        )

    severity = str(data.get("severity", "warning")).lower()
    if severity not in ("info", "warning", "critical"):
        raise ConfigError(
            "[watch.config_captures] severity must be one of info/warning/critical "
            f"(got {severity!r})"
        )

    ignore_raw = data.get("ignore_devices", [])
    if not isinstance(ignore_raw, list):
        raise ConfigError("[watch.config_captures] ignore_devices must be a list of strings")

    repo_raw = data.get("repo_path")
    return WatchConfigCapturesSettings(
        enabled=bool(data.get("enabled", False)),
        stale_after=stale_after,
        mass_outage_ratio=mass_outage_ratio,
        severity=severity,
        ignore_devices=[str(v) for v in ignore_raw],
        repo_path=Path(str(repo_raw)).expanduser() if repo_raw else default_config_repo_dir(),
    )


def _parse_watch_icmp(data: object) -> WatchIcmpSettings | None:
    """Parse the optional ``[watch.icmp]`` subsection (feature 072); ``None`` if absent."""

    if not isinstance(data, dict):
        return None

    mass_outage_ratio = float(data.get("mass_outage_ratio", 0.80))
    if not 0.0 < mass_outage_ratio <= 1.0:
        raise ConfigError(
            f"[watch.icmp] mass_outage_ratio must be in (0, 1] (got {mass_outage_ratio})"
        )

    severity = str(data.get("severity", "warning")).lower()
    if severity not in ("info", "warning", "critical"):
        raise ConfigError(
            f"[watch.icmp] severity must be one of info/warning/critical (got {severity!r})"
        )

    db_raw = data.get("db_path")
    return WatchIcmpSettings(
        enabled=bool(data.get("enabled", False)),
        mass_outage_ratio=mass_outage_ratio,
        severity=severity,
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_icmp_db_path(),
    )


def _parse_health_linux(data: object) -> HealthLinuxSettings | None:
    """Parse the optional ``[health.linux]`` subsection (feature 096); ``None`` if absent
    or when ``hosts`` is empty (an empty roster is absence, not an error — the siklu rule)."""

    if not isinstance(data, dict):
        return None
    raw_hosts = data.get("hosts", [])
    if not isinstance(raw_hosts, list):
        raise ConfigError(f"[health.linux] hosts must be a list of strings (got {raw_hosts!r})")
    hosts: list[str] = []
    for h in raw_hosts:
        name = str(h).strip()
        if name and name not in hosts:
            hosts.append(name)
    if not hosts:
        return None
    try:
        timeout = float(data.get("timeout", 15.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("[health.linux] timeout must be a number") from exc
    if timeout <= 0:
        raise ConfigError(f"[health.linux] timeout must be > 0 (got {timeout})")
    concurrency = int(data.get("concurrency", 5))
    if concurrency < 1:
        raise ConfigError(f"[health.linux] concurrency must be >= 1 (got {concurrency})")
    if not data.get("username"):
        raise ConfigError("[health.linux] requires 'username' (the SSH identity to probe as)")
    db_raw = data.get("db_path")
    return HealthLinuxSettings(
        hosts=hosts,
        username=str(data["username"]),
        timeout=timeout,
        concurrency=concurrency,
        db_path=Path(str(db_raw)).expanduser() if db_raw else None,
    )


def _parse_watch_health(data: object) -> WatchHealthSettings | None:
    """Parse the optional ``[watch.health]`` subsection (feature 096); ``None`` if absent."""

    if not isinstance(data, dict):
        return None

    def _number(key: str, default: float) -> float:
        raw = data.get(key, default)
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"[watch.health] {key} must be a number (got {raw!r})") from exc

    def _str_list(key: str) -> list[str]:
        raw = data.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ConfigError(f"[watch.health] {key} must be a list of strings (got {raw!r})")
        return [str(v) for v in raw]

    sustained_samples = int(_number("sustained_samples", 3))
    if sustained_samples < 1:
        raise ConfigError(
            f"[watch.health] sustained_samples must be >= 1 (got {sustained_samples})"
        )

    mass_outage_ratio = _number("mass_outage_ratio", 0.80)
    if not 0.0 < mass_outage_ratio <= 1.0:
        raise ConfigError(
            f"[watch.health] mass_outage_ratio must be in (0, 1] (got {mass_outage_ratio})"
        )

    severity = str(data.get("severity", "warning")).lower()
    if severity not in ("info", "warning", "critical"):
        raise ConfigError(
            f"[watch.health] severity must be one of info/warning/critical (got {severity!r})"
        )

    db_raw = data.get("db_path")
    return WatchHealthSettings(
        enabled=bool(data.get("enabled", False)),
        temperature_max_c=_number("temperature_max_c", 75.0),
        sfp_temperature_max_c=_number("sfp_temperature_max_c", 70.0),
        cpu_max_percent=_number("cpu_max_percent", 90.0),
        memory_max_percent=_number("memory_max_percent", 90.0),
        sustained_samples=sustained_samples,
        disk_max_percent=_number("disk_max_percent", 85.0),
        rx_power_min_dbm=_number("rx_power_min_dbm", -24.0),
        status_healthy_value=int(_number("status_healthy_value", 0)),
        severity=severity,
        mass_outage_ratio=mass_outage_ratio,
        ignore_devices=_str_list("ignore_devices"),
        ignore_sensors=_str_list("ignore_sensors"),
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_metrics_db_path(),
    )


def _parse_radio(radio_data: object) -> RadioSettings | None:
    """Parse the optional ``[radio]`` section (feature 088); ``None`` if absent.

    Bounds are validated so a bad value fails at load, never mid-call on the air. The
    cross-section rule (TX/shadow-TX needs a chat model key) lives in :func:`load_config`,
    where both sections are visible.
    """

    if not isinstance(radio_data, dict):
        return None

    def _positive_int(key: str, default: int, *, maximum: int | None = None) -> int:
        value = int(radio_data.get(key, default))
        if value < 1:
            raise ConfigError(f"[radio] {key} must be >= 1 (got {value})")
        if maximum is not None and value > maximum:
            raise ConfigError(f"[radio] {key} must be <= {maximum} (got {value})")
        return value

    def _non_negative_int(key: str, default: int) -> int:
        value = int(radio_data.get(key, default))
        if value < 0:
            raise ConfigError(f"[radio] {key} must be >= 0 (got {value})")
        return value

    def _str_list(key: str, default: list[str]) -> list[str]:
        raw = radio_data.get(key)
        if raw is None:
            return default
        if isinstance(raw, list):
            return [str(v) for v in raw]
        return [str(raw)]

    squelch_dbfs = float(radio_data.get("squelch_dbfs", -45.0))
    if not -120.0 <= squelch_dbfs < 0.0:
        raise ConfigError(
            f"[radio] squelch_dbfs must be a negative dBFS level in [-120, 0) (got {squelch_dbfs})"
        )
    inter_tx_floor_dbfs = float(radio_data.get("inter_tx_floor_dbfs", -60.0))
    if not -120.0 <= inter_tx_floor_dbfs < 0.0:
        raise ConfigError(
            f"[radio] inter_tx_floor_dbfs must be a negative dBFS level in [-120, 0) "
            f"(got {inter_tx_floor_dbfs})"
        )
    if inter_tx_floor_dbfs >= squelch_dbfs:
        # A floor at/above squelch would split on every speech pause — fail at load.
        raise ConfigError(
            f"[radio] inter_tx_floor_dbfs ({inter_tx_floor_dbfs}) must be strictly below "
            f"squelch_dbfs ({squelch_dbfs})"
        )
    tx_gain = float(radio_data.get("tx_gain", 1.0))
    if not 0.0 < tx_gain <= 1.0:
        raise ConfigError(f"[radio] tx_gain must be in (0, 1] (got {tx_gain})")

    def _scalar_gain(key: str, default: float) -> float:
        value = float(radio_data.get(key, default))
        if not 0.0 <= value <= 1.0:
            raise ConfigError(f"[radio] {key} must be a scalar in [0, 1] (got {value})")
        return value

    host_input_gain = _scalar_gain("host_input_gain", 0.0)
    host_output_gain = _scalar_gain("host_output_gain", 0.90)
    host_gain_tolerance = float(radio_data.get("host_gain_tolerance", 0.02))
    if not 0.0 < host_gain_tolerance <= 0.5:
        raise ConfigError(
            f"[radio] host_gain_tolerance must be in (0, 0.5] (got {host_gain_tolerance})"
        )
    name_variants = _str_list("name_variants", ["obi", "obie", "obee", "o b", "opie"])
    if not name_variants:
        raise ConfigError("[radio] name_variants must not be empty — the trigger could never fire")
    stt_model = str(radio_data.get("stt_model", "mlx-community/whisper-large-v3-turbo"))
    if not stt_model:
        raise ConfigError("[radio] stt_model must be a non-empty HuggingFace model id")
    tts_rate: int | None = None
    if "tts_rate" in radio_data:
        tts_rate = int(radio_data["tts_rate"])
        if tts_rate <= 0:
            raise ConfigError(
                f"[radio] tts_rate must be a positive words-per-minute (got {tts_rate})"
            )
    raw_pronunciations = radio_data.get("pronunciations")
    _pronunciations = (
        {str(k): str(v) for k, v in raw_pronunciations.items()}
        if isinstance(raw_pronunciations, dict)
        else {}
    )
    raw_speech_aliases = radio_data.get("speech_aliases")
    _speech_aliases = (
        {str(k): str(v) for k, v in raw_speech_aliases.items()}
        if isinstance(raw_speech_aliases, dict)
        else {}
    )
    raw_caller_aliases = radio_data.get("caller_aliases")
    _caller_aliases = (
        {str(k): str(v) for k, v in raw_caller_aliases.items()}
        if isinstance(raw_caller_aliases, dict)
        else {}
    )

    # Capture watchdog levels (feature 113): dBFS bounds fail at load, and the fade
    # window arithmetic must be self-consistent or the detector could never fire.
    def _dbfs_level(key: str, default: float) -> float:
        value = float(radio_data.get(key, default))
        if not -120.0 <= value < 0.0:
            raise ConfigError(
                f"[radio] {key} must be a negative dBFS level in [-120, 0) (got {value})"
            )
        return value

    watchdog_flatline_dbfs = _dbfs_level("watchdog_flatline_dbfs", -100.0)
    watchdog_fade_peak_dbfs = _dbfs_level("watchdog_fade_peak_dbfs", -10.0)
    watchdog_fade_window = _positive_int("watchdog_fade_window", 20)
    watchdog_fade_min = _positive_int("watchdog_fade_min", 10)
    if watchdog_fade_min > watchdog_fade_window:
        raise ConfigError(
            f"[radio] watchdog_fade_min ({watchdog_fade_min}) must be <= "
            f"watchdog_fade_window ({watchdog_fade_window})"
        )
    # Preventive capture reopen (feature 122): the floor keeps reopens from
    # machine-gunning on a chatty channel, the quiet window must fit inside the
    # LevelMonitor's 10 s ring or clear_for could never return True (same
    # self-consistency rule as the fade window above), and a backstop below the floor
    # would contradict it.
    capture_reopen_floor_s = _positive_int("capture_reopen_floor_s", 60)
    if capture_reopen_floor_s < 10:
        raise ConfigError(
            f"[radio] capture_reopen_floor_s must be >= 10 (got {capture_reopen_floor_s})"
        )
    capture_reopen_quiet_ms = _positive_int("capture_reopen_quiet_ms", 2000, maximum=10_000)
    capture_reopen_max_uptime_s = _non_negative_int("capture_reopen_max_uptime_s", 7200)
    if 0 < capture_reopen_max_uptime_s < capture_reopen_floor_s:
        raise ConfigError(
            f"[radio] capture_reopen_max_uptime_s ({capture_reopen_max_uptime_s}) must be 0 "
            f"or >= capture_reopen_floor_s ({capture_reopen_floor_s})"
        )
    tts_voice_raw = radio_data.get("tts_voice")
    model_raw = radio_data.get("model")
    serial_port_raw = radio_data.get("serial_port")
    if not radio_data.get("ftdi_serial") and not serial_port_raw:
        raise ConfigError(
            "[radio] requires 'ftdi_serial' (the PTT cable's FTDI serial) or 'serial_port'"
        )
    slack_user_raw = radio_data.get("slack_user")
    db_raw = radio_data.get("db_path")
    clips_raw = radio_data.get("clips_dir")
    return RadioSettings(
        audio_name=str(radio_data.get("audio_name", "USB PnP Sound Device")),
        ftdi_serial=str(radio_data["ftdi_serial"]) if radio_data.get("ftdi_serial") else None,
        serial_port=str(serial_port_raw) if serial_port_raw else None,
        sample_rate=_positive_int("sample_rate", 48000),
        squelch_dbfs=squelch_dbfs,
        min_active_ms=_positive_int("min_active_ms", 250),
        hang_ms=_positive_int("hang_ms", 1200),
        inter_tx_floor_dbfs=inter_tx_floor_dbfs,
        split_quiet_ms=_positive_int("split_quiet_ms", 300),
        clear_window_ms=_positive_int("clear_window_ms", 700),
        lead_ms=_non_negative_int("lead_ms", 300),
        tail_ms=_non_negative_int("tail_ms", 200),
        # A 2-minute transmission is never legitimate on a working channel; cap the cap.
        tx_cap_s=_positive_int("tx_cap_s", 15, maximum=60),
        patience_s=_positive_int("patience_s", 60),
        call_timeout_s=_positive_int("call_timeout_s", 45),
        compose_deadline_s=_positive_int("compose_deadline_s", 20),
        max_tx_per_minute=_positive_int("max_tx_per_minute", 6),
        max_tx_per_hour=_positive_int("max_tx_per_hour", 60),
        tx_gain=tx_gain,
        tx_loudness=bool(radio_data.get("tx_loudness", True)),
        operator_tx_ttl_s=_positive_int("operator_tx_ttl_s", 120),
        host_input_gain=host_input_gain,
        host_output_gain=host_output_gain,
        host_gain_tolerance=host_gain_tolerance,
        tx_enabled=bool(radio_data.get("tx_enabled", False)),
        shadow_tx=bool(radio_data.get("shadow_tx", False)),
        name_variants=name_variants,
        name_variants_weak=_str_list("name_variants_weak", ["ovi", "obini", "okie"]),
        operator_handles=_str_list("operator_handles", []),
        known_callers=_str_list("known_callers", []),
        vocab_terms=_str_list("vocab_terms", []),
        ack_text=str(radio_data.get("ack_text", "go for obi")),
        unavailable_text=str(radio_data.get("unavailable_text", "unable to comply, obi out")),
        say_again_text=str(radio_data.get("say_again_text", "ten nine, say again")),
        garble_close_text=str(radio_data.get("garble_close_text", "unable to copy, out")),
        sign_offs=_str_list("sign_offs", []),
        standby_text=str(radio_data.get("standby_text", "stand by")),
        # 0 = transmit the standby as soon as composing starts (operator direction
        # 2026-08-20: an answer always takes seconds — say "stand by" right away).
        standby_after_s=_non_negative_int("standby_after_s", 4),
        pronunciations=_pronunciations,
        speech_aliases=_speech_aliases,
        caller_aliases=_caller_aliases,
        channel_name=str(radio_data.get("channel_name", "the operations radio channel")),
        slack_call_notes=bool(radio_data.get("slack_call_notes", True)),
        stt_model=stt_model,
        tts_voice=str(tts_voice_raw) if tts_voice_raw else None,
        tts_rate=tts_rate,
        model=str(model_raw) if model_raw else None,
        max_tokens=_positive_int("max_tokens", 600),
        live_calls=_non_negative_int("live_calls", 3),
        slack_user=str(slack_user_raw) if slack_user_raw else None,
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_radio_db_path(),
        clips_dir=Path(str(clips_raw)).expanduser() if clips_raw else default_radio_clips_dir(),
        retention_days=_non_negative_int("retention_days", 0),
        watchdog=bool(radio_data.get("watchdog", True)),
        watchdog_flatline_dbfs=watchdog_flatline_dbfs,
        watchdog_flatline_s=_positive_int("watchdog_flatline_s", 120),
        watchdog_fade_window=watchdog_fade_window,
        watchdog_fade_min=watchdog_fade_min,
        watchdog_fade_peak_dbfs=watchdog_fade_peak_dbfs,
        watchdog_fade_span_s=_positive_int("watchdog_fade_span_s", 43200),
        watchdog_floor_s=_non_negative_int("watchdog_floor_s", 600),
        watchdog_max_reopens=_positive_int("watchdog_max_reopens", 2),
        watchdog_escalation_s=_positive_int("watchdog_escalation_s", 3600),
        watchdog_stuck_s=_positive_int("watchdog_stuck_s", 300),
        capture_reopen_after_tx=bool(radio_data.get("capture_reopen_after_tx", True)),
        capture_reopen_floor_s=capture_reopen_floor_s,
        capture_reopen_quiet_ms=capture_reopen_quiet_ms,
        capture_reopen_max_uptime_s=capture_reopen_max_uptime_s,
        # Garble-triggered reopen (feature 133): min age may be 0 (always suspect); the
        # hourly cap must allow at least one.
        garble_reopen=bool(radio_data.get("garble_reopen", True)),
        garble_reopen_min_age_s=_non_negative_int("garble_reopen_min_age_s", 60),
        garble_reopen_max_per_hour=_positive_int("garble_reopen_max_per_hour", 6),
        # Outbound calls (feature 124). max_rehails is non-negative (0 = single hail);
        # everything else must be positive. The chars gate is the submission-time bound —
        # the tx duration cap remains the physical belt.
        outbound_ack_window_s=_positive_int("outbound_ack_window_s", 30),
        outbound_max_rehails=_non_negative_int("outbound_max_rehails", 2),
        outbound_ttl_s=_positive_int("outbound_ttl_s", 600),
        outbound_max_chars=_positive_int("outbound_max_chars", 320),
    )


def parse_maintenance(raw: object) -> MaintenanceSettings:
    """Parse ``[maintenance]`` (feature 135) STRICTLY; absent ⇒ empty declaration.

    Unlike the lenient ``ignore_*`` lists, every malformed value is a :class:`ConfigError`:
    a typo'd key or signal must fail loudly at load, never silently leave something alarming
    (or, worse, silently put nothing in maintenance while the operator believes otherwise).
    """

    if raw is None:
        return MaintenanceSettings()
    if not isinstance(raw, dict):
        raise ConfigError("[maintenance] must be a table")
    unknown = sorted(set(raw) - set(_MAINTENANCE_KEYS))
    if unknown:
        raise ConfigError(
            f"[maintenance] unknown key(s) {', '.join(unknown)} "
            f"(valid: {', '.join(_MAINTENANCE_KEYS)})"
        )

    def _names(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"[maintenance] {key} must be a list of strings")
        cleaned = [" ".join(v.split()).casefold() for v in value]
        if any(not v for v in cleaned):
            raise ConfigError(f"[maintenance] {key} must not contain empty names")
        return tuple(dict.fromkeys(cleaned))

    signals = _names("signals")
    bad = [s for s in signals if s not in MAINTENANCE_SIGNALS]
    if bad:
        raise ConfigError(
            f"[maintenance] unknown signal(s) {', '.join(bad)} "
            f"(valid: {', '.join(MAINTENANCE_SIGNALS)})"
        )
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        raise ConfigError("[maintenance] reason must be a string")
    return MaintenanceSettings(
        sites=_names("sites"),
        devices=_names("devices"),
        uisp_sites=_names("uisp_sites"),
        signals=signals,
        reason=reason.strip(),
    )


def _parse_icmp(icmp_data: object) -> IcmpSettings | None:
    """Parse the optional ``[icmp]`` collector section (feature 072); ``None`` if absent.

    ``None`` ⇒ ``obi icmp run`` still works with an all-default :class:`IcmpSettings`; the section
    only exists to tune the probe. Bounds are validated so a bad value fails fast, not mid-cron.
    """

    if not isinstance(icmp_data, dict):
        return None

    def _positive(key: str, default: int) -> int:
        value = int(icmp_data.get(key, default))
        if value < 1:
            raise ConfigError(f"[icmp] {key} must be >= 1 (got {value})")
        return value

    deadline_s = float(icmp_data.get("deadline_s", 5.0))
    if deadline_s <= 0:
        raise ConfigError(f"[icmp] deadline_s must be > 0 (got {deadline_s})")

    def _str_list(key: str) -> list[str]:
        raw = icmp_data.get(key)
        if isinstance(raw, list):
            return [str(v) for v in raw]
        if raw is None:
            return []
        return [str(raw)]

    db_raw = icmp_data.get("db_path")
    return IcmpSettings(
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_icmp_db_path(),
        pings_per_cycle=_positive("pings_per_cycle", 3),
        ping_timeout_ms=_positive("ping_timeout_ms", 1000),
        deadline_s=deadline_s,
        concurrency=_positive("concurrency", 16),
        down_after_cycles=_positive("down_after_cycles", 1),
        include_siklu=bool(icmp_data.get("include_siklu", True)),
        ignore_devices=_str_list("ignore_devices"),
        extra_hosts=_str_list("extra_hosts"),
        include_uisp_ptp=bool(icmp_data.get("include_uisp_ptp", True)),
    )


def _parse_brc(data: object) -> BrcSettings | None:
    """``[brc]`` (feature 137): ``None`` when absent ⇒ city-grid and address features are off."""

    if not isinstance(data, dict):
        return None
    for key in ("origin_lat", "origin_lon", "year"):
        if data.get(key) is None:
            raise ConfigError(f"[brc] requires '{key}'")
    lat, lon = float(data["origin_lat"]), float(data["origin_lon"])
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ConfigError("[brc] origin_lat/origin_lon out of range")
    source = data.get("source_url")
    if source is not None and not str(source).startswith(("http://", "https://")):
        raise ConfigError(f"[brc] source_url must be an http(s) URL (got {source!r})")
    return BrcSettings(
        origin_lat=lat,
        origin_lon=lon,
        year=int(data["year"]),
        source_url=str(source) if source else None,
        az12_deg=float(data.get("az12_deg", 45.0)),
    )


def _parse_playa(data: object) -> PlayaSettings | None:
    if not isinstance(data, dict):
        return None
    base = str(data.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise ConfigError("[playa] requires 'base_url' (the event's public API)")
    if not base.startswith(("http://", "https://")):
        raise ConfigError(f"[playa] base_url must be an http(s) URL (got {base!r})")
    return PlayaSettings(base_url=base)


def _parse_site(data: object) -> SiteSettings:
    if not isinstance(data, dict):
        return SiteSettings()
    tz = str(data.get("timezone", "UTC")).strip() or "UTC"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001 - any zoneinfo failure is a config error
        raise ConfigError(f"[site] timezone {tz!r} is not a known IANA zone") from exc
    return SiteSettings(timezone=tz)


def _parse_weather(weather_data: object) -> WeatherSettings | None:
    """Parse the optional ``[weather]`` collector section (feature 110); ``None`` if absent.

    ``None`` ⇒ weather is off (feature 137). Bounds are validated so a bad value fails fast at
    load, not mid-cron.
    """

    if not isinstance(weather_data, dict):
        return None

    for key in ("latitude", "longitude"):
        if weather_data.get(key) is None:
            raise ConfigError(f"[weather] requires '{key}' (the forecast location)")
    latitude = float(weather_data["latitude"])
    if not -90.0 <= latitude <= 90.0:
        raise ConfigError(f"[weather] latitude must be within ±90 (got {latitude})")
    longitude = float(weather_data["longitude"])
    if not -180.0 <= longitude <= 180.0:
        raise ConfigError(f"[weather] longitude must be within ±180 (got {longitude})")
    zone_raw = weather_data.get("nws_zone")
    nws_zone = str(zone_raw).strip() if zone_raw is not None else None
    if nws_zone == "":
        raise ConfigError("[weather] nws_zone, when set, must be an NWS zone id (e.g. ABZ001)")
    place_raw = weather_data.get("place_name")
    timeout_s = float(weather_data.get("timeout_s", 20.0))
    if timeout_s <= 0:
        raise ConfigError(f"[weather] timeout_s must be > 0 (got {timeout_s})")
    retention_days = int(weather_data.get("retention_days", 120))
    if retention_days < 1:
        raise ConfigError(f"[weather] retention_days must be >= 1 (got {retention_days})")

    db_raw = weather_data.get("db_path")
    return WeatherSettings(
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_weather_db_path(),
        latitude=latitude,
        longitude=longitude,
        nws_zone=nws_zone,
        place_name=str(place_raw).strip() if place_raw else None,
        timeout_s=timeout_s,
        retention_days=retention_days,
    )


def _parse_traffic(traffic_data: object) -> TrafficSettings | None:
    """Parse the optional ``[traffic]`` collector section (feature 112); ``None`` if absent.

    ``None`` ⇒ traffic is off (feature 137). Bounds are validated so a bad value fails fast at
    load, not mid-cron.
    """

    if not isinstance(traffic_data, dict):
        return None

    if not traffic_data.get("base_url"):
        raise ConfigError("[traffic] requires 'base_url' (the event dashboard to poll)")
    base_url = str(traffic_data["base_url"]).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"[traffic] base_url must be an http(s) URL (got {base_url!r})")
    timeout_s = float(traffic_data.get("timeout_s", 20.0))
    if timeout_s <= 0:
        raise ConfigError(f"[traffic] timeout_s must be > 0 (got {timeout_s})")
    retention_days = int(traffic_data.get("retention_days", 120))
    if retention_days < 1:
        raise ConfigError(f"[traffic] retention_days must be >= 1 (got {retention_days})")

    db_raw = traffic_data.get("db_path")
    return TrafficSettings(
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_traffic_db_path(),
        base_url=base_url,
        timeout_s=timeout_s,
        retention_days=retention_days,
    )


def _parse_dash(dash_data: object) -> DashSettings | None:
    """Parse the optional ``[dash]`` section (feature 095); ``None`` if absent.

    Bounds are validated so a bad value fails at load, never mid-cycle in the minute cron (the
    087 conventions: strict lists, duration grammar validated for its ``ConfigError``, ratios in
    (0, 1]). ``[[dash.internet_links]]`` entries each require ``device``/``interface``/
    ``capacity_mbps`` — capacity is operator knowledge with no default.
    """

    if not isinstance(dash_data, dict):
        return None

    flap_window = str(dash_data.get("flap_window", "10m"))
    duration_to_seconds(flap_window, 600, context="[dash] flap_window")
    netwatch_loss_window = str(dash_data.get("netwatch_loss_window", "5m"))
    duration_to_seconds(netwatch_loss_window, 300, context="[dash] netwatch_loss_window")

    def _positive_float(key: str, default: float, *, maximum: float | None = None) -> float:
        try:
            value = float(dash_data.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"[dash] {key} must be a number (got {dash_data.get(key)!r})"
            ) from exc
        if value <= 0 or (maximum is not None and value > maximum):
            bound = f"(0, {maximum}]" if maximum is not None else "> 0"
            raise ConfigError(f"[dash] {key} must be {bound} (got {value})")
        return value

    ping_rtt_warn_ms = _positive_float("ping_rtt_warn_ms", 50.0)
    ping_loss_warn_pct = _positive_float("ping_loss_warn_pct", 25.0, maximum=100.0)
    netwatch_loss_warn_pct = _positive_float("netwatch_loss_warn_pct", 5.0, maximum=100.0)
    netwatch_rtt_warn_ms = _positive_float("netwatch_rtt_warn_ms", 25.0)
    internet_warn_pct = _positive_float("internet_warn_pct", 70.0, maximum=100.0)
    internet_down_pct = _positive_float("internet_down_pct", 90.0, maximum=100.0)
    if internet_warn_pct > internet_down_pct:
        raise ConfigError(
            f"[dash] internet_warn_pct ({internet_warn_pct}) must not exceed "
            f"internet_down_pct ({internet_down_pct})"
        )

    ping_down_after = int(dash_data.get("ping_down_after", 2))
    if ping_down_after < 1:
        raise ConfigError(f"[dash] ping_down_after must be >= 1 (got {ping_down_after})")

    refs_raw = dash_data.get("ping_refs")
    if isinstance(refs_raw, list):
        ping_refs = [str(r) for r in refs_raw]
    elif refs_raw is None:
        ping_refs = ["8.8.8.8", "1.1.1.1"]
    else:
        ping_refs = [str(refs_raw)]
    if not ping_refs:
        raise ConfigError("[dash] ping_refs must name at least one reference address")

    links_raw = dash_data.get("internet_links", [])
    if not isinstance(links_raw, list):
        raise ConfigError(
            "[dash] internet_links must be an array of tables ([[dash.internet_links]])"
        )
    internet_links: list[DashInternetLink] = []
    for entry in links_raw:
        if not isinstance(entry, dict):
            raise ConfigError("[dash] each [[dash.internet_links]] entry must be a table")
        missing = [key for key in ("device", "interface", "capacity_mbps") if key not in entry]
        if missing:
            raise ConfigError(
                f"[dash] [[dash.internet_links]] entry missing required key(s): "
                f"{', '.join(missing)}"
            )
        try:
            capacity = float(entry["capacity_mbps"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"[dash] internet_links capacity_mbps must be a number "
                f"(got {entry['capacity_mbps']!r})"
            ) from exc
        if capacity <= 0:
            raise ConfigError(f"[dash] internet_links capacity_mbps must be > 0 (got {capacity})")
        internet_links.append(
            DashInternetLink(
                device=str(entry["device"]),
                interface=str(entry["interface"]),
                capacity_mbps=capacity,
                label=str(entry.get("label", "")),
            )
        )

    pager_raw = dash_data.get("pager")
    pager = DashPagerSettings()
    if pager_raw is not None:
        if not isinstance(pager_raw, dict):
            raise ConfigError("[dash.pager] must be a table")
        mass_ratio = float(pager_raw.get("mass_ratio", 0.80))
        if not 0.0 < mass_ratio <= 1.0:
            raise ConfigError(f"[dash.pager] mass_ratio must be in (0, 1] (got {mass_ratio})")
        signals_raw = pager_raw.get("signals")
        if signals_raw is None:
            signals = list(_DASH_PAGEABLE_SIGNALS)
        elif isinstance(signals_raw, list):
            signals = [str(s).lower() for s in signals_raw]
            unknown = [s for s in signals if s not in _DASH_SIGNALS]
            if unknown:
                raise ConfigError(
                    f"[dash.pager] unknown signal(s) {', '.join(unknown)} "
                    f"(valid: {', '.join(_DASH_SIGNALS)})"
                )
            # 121: a real signal that is nonetheless display-only. Rejected rather than
            # ignored, so an operator who tries to page on it is told, not quietly overruled.
            not_pageable = [s for s in signals if s not in _DASH_PAGEABLE_SIGNALS]
            if not_pageable:
                raise ConfigError(
                    f"[dash.pager] signal(s) {', '.join(not_pageable)} are display-only and "
                    f"cannot page (pageable: {', '.join(_DASH_PAGEABLE_SIGNALS)})"
                )
        else:
            raise ConfigError("[dash.pager] signals must be a list of signal names")
        announce_hold = str(pager_raw.get("announce_hold", "0s"))
        duration_to_seconds(announce_hold, 0, context="[dash.pager] announce_hold")
        extra_raw = pager_raw.get("extra_sites", [])
        if not isinstance(extra_raw, list) or not all(isinstance(s, str) for s in extra_raw):
            raise ConfigError("[dash.pager] extra_sites must be a list of site names")
        pager = DashPagerSettings(
            enabled=bool(pager_raw.get("enabled", False)),
            mass_ratio=mass_ratio,
            signals=signals,
            link_grouping=bool(pager_raw.get("link_grouping", True)),
            announce_hold=announce_hold,
            extra_sites=[s.lower() for s in extra_raw],
        )

    # 121: [dash.mesh] / [dash.lighting] — display-only tiles, all-default when absent.
    mesh_raw = dash_data.get("mesh")
    if mesh_raw is None:
        dash_mesh = DashMeshSettings()
    elif isinstance(mesh_raw, dict):
        dash_mesh = DashMeshSettings(
            heard_yellow_after=str(mesh_raw.get("heard_yellow_after", "2h")),
            heard_red_after=str(mesh_raw.get("heard_red_after", "12h")),
            fix_yellow_after=str(mesh_raw.get("fix_yellow_after", "6h")),
            read_interval=str(mesh_raw.get("read_interval", "5m")),
        )
        for key, value in (
            ("heard_yellow_after", dash_mesh.heard_yellow_after),
            ("heard_red_after", dash_mesh.heard_red_after),
            ("fix_yellow_after", dash_mesh.fix_yellow_after),
            ("read_interval", dash_mesh.read_interval),
        ):
            duration_to_seconds(value, 0, context=f"[dash.mesh] {key}")
    else:
        raise ConfigError("[dash.mesh] must be a table")

    lighting_raw = dash_data.get("lighting")
    if lighting_raw is None:
        dash_lighting = DashLightingSettings()
    elif isinstance(lighting_raw, dict):
        timeout_raw = lighting_raw.get("timeout")
        if timeout_raw is None:
            dash_lighting = DashLightingSettings()
        else:
            if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
                raise ConfigError("[dash.lighting] timeout must be a positive number")
            if float(timeout_raw) <= 0:
                raise ConfigError("[dash.lighting] timeout must be a positive number")
            dash_lighting = DashLightingSettings(timeout=float(timeout_raw))
    else:
        raise ConfigError("[dash.lighting] must be a table")

    status_band_raw = dash_data.get("status_band")
    if status_band_raw is None:
        dash_status_band = DashStatusBandSettings()
    elif isinstance(status_band_raw, dict):
        sb_timeout_raw = status_band_raw.get("timeout")
        sb_timeout: float | None = None
        if sb_timeout_raw is not None:
            if isinstance(sb_timeout_raw, bool) or not isinstance(sb_timeout_raw, (int, float)):
                raise ConfigError("[dash.status_band] timeout must be a positive number")
            if float(sb_timeout_raw) <= 0:
                raise ConfigError("[dash.status_band] timeout must be a positive number")
            sb_timeout = float(sb_timeout_raw)
        dash_status_band = DashStatusBandSettings(
            enabled=bool(status_band_raw.get("enabled", False)),
            timeout=sb_timeout,
        )
    else:
        raise ConfigError("[dash.status_band] must be a table")

    state_raw = dash_data.get("state_dir")
    return DashSettings(
        enabled=bool(dash_data.get("enabled", False)),
        state_dir=Path(str(state_raw)).expanduser() if state_raw else default_dash_state_dir(),
        ping_router=str(dash_data.get("ping_router", "")),
        ping_refs=ping_refs,
        ping_rtt_warn_ms=ping_rtt_warn_ms,
        ping_loss_warn_pct=ping_loss_warn_pct,
        ping_down_after=ping_down_after,
        netwatch_loss_warn_pct=netwatch_loss_warn_pct,
        netwatch_rtt_warn_ms=netwatch_rtt_warn_ms,
        netwatch_loss_window=netwatch_loss_window,
        flap_window=flap_window,
        internet_warn_pct=internet_warn_pct,
        internet_down_pct=internet_down_pct,
        internet_links=internet_links,
        pager=pager,
        mesh=dash_mesh,
        lighting=dash_lighting,
        status_band=dash_status_band,
    )


def _parse_paging(paging_data: object) -> PagingSettings | None:
    """Parse the optional ``[paging]`` section (feature 102); ``None`` if absent.

    The person-only rule for ``[paging.alerts] people`` is enforced HERE, at the earliest
    possible moment (clarification #2): the automated path must be structurally unable to
    address a group, so a non-numeric token — a name, a group ID passed as a name, anything —
    fails the load, never a 1-minute cron cycle.
    """

    if not isinstance(paging_data, dict):
        return None

    if not paging_data.get("username") or not paging_data.get("password"):
        raise ConfigError("[paging] section requires both 'username' and 'password'")

    if not paging_data.get("url"):
        raise ConfigError("[paging] requires 'url' (the paging service endpoint)")
    url = str(paging_data["url"])
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"[paging] url must be an http(s) URL (got {url!r})")

    message_max_chars = int(paging_data.get("message_max_chars", 200))
    if message_max_chars < 40:
        # A page must at least fit its identity ("obi DOWN host: <name> …").
        raise ConfigError(f"[paging] message_max_chars must be >= 40 (got {message_max_chars})")

    send_spacing_s = float(paging_data.get("send_spacing_s", 2.0))
    if send_spacing_s < 0:
        raise ConfigError(f"[paging] send_spacing_s must be >= 0 (got {send_spacing_s})")

    alerts = PagingAlertSettings()
    alerts_raw = paging_data.get("alerts")
    if alerts_raw is not None:
        if not isinstance(alerts_raw, dict):
            raise ConfigError("[paging.alerts] must be a table")
        people_raw = alerts_raw.get("people", [])
        if not isinstance(people_raw, list):
            raise ConfigError("[paging.alerts] people must be a list of person IDs")
        people = [str(p) for p in people_raw]
        bad = [p for p in people if not p.isdigit()]
        if bad:
            raise ConfigError(
                f"[paging.alerts] people accepts numeric person IDs only (got "
                f"{', '.join(repr(p) for p in bad)}) — groups and names are not allowed on "
                f"the automated path; look IDs up with `obi page recipients`"
            )
        enabled = bool(alerts_raw.get("enabled", False))
        if enabled and not people:
            raise ConfigError(
                "[paging.alerts] enabled is true but 'people' is empty — name at least one "
                "person ID"
            )
        max_pages_per_hour = int(alerts_raw.get("max_pages_per_hour", 6))
        if max_pages_per_hour < 1:
            raise ConfigError(
                f"[paging.alerts] max_pages_per_hour must be >= 1 (got {max_pages_per_hour})"
            )
        alerts = PagingAlertSettings(
            enabled=enabled, people=people, max_pages_per_hour=max_pages_per_hour
        )

    # feature 130: the Slack-DM member-paging opt-in (constitution 3.0.0). Presence is the
    # feature switch; channels may be empty (operators only). The per-sender cap must be a
    # real bound — disable by removing the section, never by zeroing the cap.
    slack: PagingSlackSettings | None = None
    slack_raw = paging_data.get("slack")
    if slack_raw is not None:
        if not isinstance(slack_raw, dict):
            raise ConfigError("[paging.slack] must be a table")
        channels_raw = slack_raw.get("channels", [])
        if not isinstance(channels_raw, list):
            raise ConfigError("[paging.slack] channels must be a list of conversation IDs")
        recipients_raw = slack_raw.get("recipients")
        if not isinstance(recipients_raw, list) or not recipients_raw:
            # The recipient allowlist is load-bearing (operator decision 2026-09-03):
            # member pages may only reach operator-declared targets, so an absent or empty
            # list is a config error, never an open roster.
            raise ConfigError(
                "[paging.slack] requires a non-empty 'recipients' list — the roster tokens "
                "(names or ids) member pages may reach; look them up with `obi page "
                "recipients`"
            )
        slack_max = int(slack_raw.get("max_pages_per_hour", 4))
        if slack_max < 1:
            raise ConfigError(
                f"[paging.slack] max_pages_per_hour must be >= 1 (got {slack_max}) — "
                "disable member paging by removing the [paging.slack] section"
            )
        slack = PagingSlackSettings(
            channels=[str(c) for c in channels_raw],
            recipients=[str(r) for r in recipients_raw],
            max_pages_per_hour=slack_max,
        )

    roster_raw = paging_data.get("roster_path")
    return PagingSettings(
        username=str(paging_data["username"]),
        password=Secret(str(paging_data["password"])),
        url=url,
        roster_path=(
            Path(str(roster_raw)).expanduser() if roster_raw else default_paging_roster_path()
        ),
        message_max_chars=message_max_chars,
        send_spacing_s=send_spacing_s,
        alerts=alerts,
        slack=slack,
    )


def _parse_watch_uisp_links(data: object) -> WatchUispLinksSettings | None:
    """Parse the optional ``[watch.uisp_links]`` subsection (feature 034); ``None`` if absent."""

    if not isinstance(data, dict):
        return None

    signal_drop_db = float(data.get("signal_drop_db", 6.0))
    if signal_drop_db <= 0:
        raise ConfigError(f"[watch.uisp_links] signal_drop_db must be > 0 (got {signal_drop_db:g})")

    ignore_raw = data.get("ignore_links")
    if isinstance(ignore_raw, list):
        ignore_links = [str(pattern) for pattern in ignore_raw]
    elif ignore_raw is None:
        ignore_links = []
    else:
        ignore_links = [str(ignore_raw)]

    return WatchUispLinksSettings(
        enabled=bool(data.get("enabled", False)),
        signal_drop_db=signal_drop_db,
        ignore_links=ignore_links,
    )


def _parse_watch_config_changes(data: object) -> WatchConfigChangesSettings | None:
    """Parse the optional ``[watch.config_changes]`` subsection (feature 070)."""

    if not isinstance(data, dict):
        return None

    # Mirrors obi.core.watch.SEVERITIES; inlined to avoid a config→watch import cycle (as
    # slack_min_severity does above).
    severity = str(data.get("severity", "warning")).lower()
    if severity not in ("info", "warning", "critical"):
        raise ConfigError(
            "[watch.config_changes] severity must be one of info/warning/critical "
            f"(got {severity!r})"
        )

    max_diff_lines = int(data.get("max_diff_lines", 120))
    if max_diff_lines <= 0:
        raise ConfigError(
            f"[watch.config_changes] max_diff_lines must be > 0 (got {max_diff_lines})"
        )

    ignore_raw = data.get("ignore_devices")
    if isinstance(ignore_raw, list):
        ignore_devices = [str(d) for d in ignore_raw]
    elif ignore_raw is None:
        ignore_devices = []
    else:
        ignore_devices = [str(ignore_raw)]

    repo_raw = data.get("repo_path")
    return WatchConfigChangesSettings(
        enabled=bool(data.get("enabled", False)),
        severity=severity,
        max_diff_lines=max_diff_lines,
        ignore_devices=ignore_devices,
        repo_path=Path(str(repo_raw)).expanduser() if repo_raw else default_config_repo_dir(),
    )


def _parse_siklu(siklu_data: object) -> SikluSettings | None:
    """Parse the optional ``[siklu]`` section (feature 020); ``None`` when absent or hosts empty.

    An empty/missing ``hosts`` is a clean "no radios configured" (FR-010) — returns ``None`` so
    Siklu is simply not a configured source, never an error. A non-empty ``hosts`` REQUIRES
    ``username`` and ``password`` (like ``[uisp]``), raising :class:`ConfigError` otherwise.
    ``hosts`` is normalized to a de-duped, order-preserving list. ``pairs`` accepts a list of
    two-element ``[name_a, name_b]`` arrays.
    """

    if not isinstance(siklu_data, dict):
        return None

    hosts_raw = siklu_data.get("hosts")
    hosts_list = (
        [str(h).strip() for h in hosts_raw if str(h).strip()] if isinstance(hosts_raw, list) else []
    )
    # De-dupe while preserving order (a hand-maintained roster may repeat a host).
    seen: set[str] = set()
    hosts: list[str] = []
    for host in hosts_list:
        if host not in seen:
            seen.add(host)
            hosts.append(host)

    if not hosts:
        # Empty/missing roster ⇒ clean "no radios", not an error (FR-010).
        return None

    if not siklu_data.get("username") or not siklu_data.get("password"):
        raise ConfigError("[siklu] section with 'hosts' requires both 'username' and 'password'")

    pairs_raw = siklu_data.get("pairs")
    pairs: list[tuple[str, str]] | None = None
    if isinstance(pairs_raw, list):
        parsed_pairs: list[tuple[str, str]] = []
        for pair in pairs_raw:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                parsed_pairs.append((str(pair[0]), str(pair[1])))
            else:
                raise ConfigError(
                    "[siklu] each entry in 'pairs' must be a two-element [name_a, name_b] array"
                )
        pairs = parsed_pairs or None

    name_filter = siklu_data.get("name_filter")
    return SikluSettings(
        hosts=hosts,
        username=str(siklu_data["username"]),
        password=Secret(str(siklu_data["password"])),
        port=int(siklu_data.get("port", 22)),
        timeout=float(siklu_data.get("timeout", 6.0)),
        concurrency=int(siklu_data.get("concurrency", 8)),
        pairs=pairs,
        name_filter=str(name_filter) if name_filter else None,
    )


def _parse_wled(wled_data: object) -> WledSettings | None:
    """Parse the optional ``[wled]`` section (feature 103); ``None`` when absent or devices empty.

    ``devices`` is a name → base-URL table — the complete lighting write allowlist. Each URL must
    be ``http(s)://``; anything else raises :class:`ConfigError` (a mistyped allowlist must fail
    at load, never at write time). An empty/missing ``devices`` is a clean "no lighting device
    configured", not an error.
    """

    if not isinstance(wled_data, dict):
        return None

    devices_raw = wled_data.get("devices")
    if devices_raw is None:
        return None
    if not isinstance(devices_raw, dict):
        raise ConfigError("[wled] 'devices' must be a table of name = \"http(s)://…\" entries")
    devices: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for name, entry in devices_raw.items():
        url = entry
        if isinstance(entry, dict):
            url = entry.get("url", "")
            if entry.get("description"):
                descriptions[str(name)] = str(entry["description"]).strip()
        base = str(url).strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ConfigError(
                f"[wled] device {name!r} has URL {url!r} — must start with http:// or https://"
            )
        devices[str(name)] = base
    if not devices:
        return None
    # 137: descriptions may also live in their own [wled.descriptions] table (name → text) —
    # the form that stays readable by older obi versions sharing the same config file.
    extra = wled_data.get("descriptions")
    if isinstance(extra, dict):
        for name, text in extra.items():
            if str(name) in devices and str(text).strip():
                descriptions.setdefault(str(name), str(text).strip())

    segments: dict[str, SegmentLayout] = {}
    segments_raw = wled_data.get("segments")
    if segments_raw is not None:
        if not isinstance(segments_raw, dict):
            raise ConfigError(
                "[wled] 'segments' must be per-device tables ([wled.segments.<device>])"
            )
        for dev_name, layout_raw in segments_raw.items():
            key = f"[wled.segments.{dev_name}]"
            if str(dev_name) not in devices:
                raise ConfigError(
                    f"{key} does not match any [wled.devices] entry "
                    f"(configured: {', '.join(sorted(devices))})"
                )
            if not isinstance(layout_raw, dict):
                raise ConfigError(f"{key} must be a table with 'status' and 'main' id lists")
            roles: dict[str, tuple[int, ...]] = {}
            for role in ("status", "main"):
                ids_raw = layout_raw.get(role)
                if (
                    not isinstance(ids_raw, list)
                    or not ids_raw
                    or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids_raw)
                ):
                    raise ConfigError(
                        f"{key} {role} must be a non-empty list of segment ids (integers)"
                    )
                if any(i < 0 for i in ids_raw):
                    raise ConfigError(f"{key} {role} segment ids must be >= 0")
                roles[role] = tuple(ids_raw)
            overlap = set(roles["status"]) & set(roles["main"])
            if overlap:
                raise ConfigError(
                    f"{key} status and main must be disjoint "
                    f"(both claim: {', '.join(str(i) for i in sorted(overlap))})"
                )
            segments[str(dev_name)] = SegmentLayout(status=roles["status"], main=roles["main"])

    return WledSettings(
        devices=devices,
        timeout=float(wled_data.get("timeout", 5.0)),
        verify_tls=bool(wled_data.get("verify_tls", True)),
        segments=segments,
        descriptions=descriptions,
    )


def _parse_victoria(victoria_data: object) -> VictoriaSettings | None:
    """Parse the optional ``[victoria]`` section (feature 108); ``None`` when absent.

    ``base_url`` is required and must be ``http(s)://`` (a mistyped endpoint must fail at load,
    never mid-chat). The store is credential-free by design — no token key exists here.
    """

    if not isinstance(victoria_data, dict):
        return None

    base_raw = victoria_data.get("base_url")
    if base_raw in (None, ""):
        raise ConfigError("[victoria] section requires 'base_url'")
    base_url = str(base_raw).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"[victoria] base_url {base_raw!r} must start with http:// or https://")

    timeout = float(victoria_data.get("timeout", 10.0))
    if timeout <= 0:
        raise ConfigError(f"[victoria] timeout must be positive (got {timeout:g})")
    weather_stale_s = int(victoria_data.get("weather_stale_s", 900))
    if weather_stale_s <= 0:
        raise ConfigError(f"[victoria] weather_stale_s must be positive (got {weather_stale_s})")

    return VictoriaSettings(
        base_url=base_url,
        timeout=timeout,
        rf_window=str(victoria_data.get("rf_window", "24h")),
        weather_stale_s=weather_stale_s,
        display_name=str(victoria_data.get("display_name", "the external metrics store")),
        rf_matcher=str(victoria_data.get("rf_matcher", '{role="ptp-rf"}')),
    )


def default_wled_snapshots_path() -> Path:
    """XDG state location for the pre-change lighting snapshots (feature 103, FR-006).

    ``$XDG_STATE_HOME/obi/wled_snapshots.json`` (``~/.local/state`` default), beside the other
    obi state. One snapshot per device — what ``wled_revert`` restores; the full history rides
    in the audit store.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "wled_snapshots.json"


def _parse_meshtastic(mesh_data: object) -> MeshtasticSettings | None:
    """Parse the optional ``[meshtastic]`` section (feature 111); ``None`` when absent.

    An empty table is a valid all-defaults opt-in (the section's presence IS the enable —
    see :class:`MeshtasticSettings`). Malformed or non-positive limits raise
    :class:`ConfigError`: a broken airtime guard must fail at load, never at query time.
    """

    if not isinstance(mesh_data, dict):
        return None

    port_raw = mesh_data.get("port")
    port: str | None = None
    if port_raw is not None:
        port = str(port_raw).strip()
        if not port:
            raise ConfigError("[meshtastic] 'port' must be a device path (or omitted)")

    def positive_int(key: str, default: int) -> int:
        value = mesh_data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"[meshtastic] '{key}' must be a positive integer")
        if value <= 0:
            raise ConfigError(f"[meshtastic] '{key}' must be a positive integer")
        return value

    def positive_float(key: str, default: float) -> float:
        value = mesh_data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"[meshtastic] '{key}' must be a positive number")
        parsed = float(value)
        if parsed <= 0:
            raise ConfigError(f"[meshtastic] '{key}' must be a positive number")
        return parsed

    def nonnegative_int(key: str, default: int) -> int:
        # 125: auto_request_s legitimately takes 0 ("automatic requesting off"), which
        # positive_int deliberately refuses for the airtime guards.
        value = mesh_data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"[meshtastic] '{key}' must be a non-negative integer")
        return value

    request_timeout_s = positive_float("request_timeout_s", 30.0)
    snapshot_timeout_s = positive_float("snapshot_timeout_s", 10.0)

    def name_table(key: str) -> dict[str, str]:
        raw = mesh_data.get(key, {})
        if not isinstance(raw, dict):
            raise ConfigError(f"[meshtastic] '{key}' must be a table of string entries")
        table: dict[str, str] = {}
        for name, target in raw.items():
            target_text = str(target).strip()
            if not target_text:
                raise ConfigError(f"[meshtastic] {key} entry {name!r} has an empty value")
            table[str(name)] = target_text
        return table

    def string_list(key: str) -> list[str]:
        raw = mesh_data.get(key, [])
        if not isinstance(raw, list):
            raise ConfigError(f"[meshtastic] '{key}' must be a list of node ids/names")
        values: list[str] = []
        for entry in raw:
            text = str(entry).strip()
            if not text:
                raise ConfigError(f"[meshtastic] '{key}' has an empty entry")
            values.append(text)
        return values

    channel_raw = mesh_data.get("request_channel_index", 0)
    if (
        isinstance(channel_raw, bool)
        or not isinstance(channel_raw, int)
        or not (0 <= channel_raw <= 7)
    ):
        raise ConfigError("[meshtastic] 'request_channel_index' must be a channel slot 0-7")

    positions_raw = mesh_data.get("positions_db_path")
    if positions_raw is not None and not str(positions_raw).strip():
        raise ConfigError("[meshtastic] 'positions_db_path' must be a file path (or omitted)")

    socket_raw = mesh_data.get("daemon_socket")
    if socket_raw is not None and not str(socket_raw).strip():
        raise ConfigError("[meshtastic] 'daemon_socket' must be a file path (or omitted)")

    art_raw = mesh_data.get("art_path")
    if art_raw is not None and not str(art_raw).strip():
        raise ConfigError("[meshtastic] 'art_path' must be a file path (or omitted)")

    return MeshtasticSettings(
        port=port,
        # NOTE: excluded_ports is deliberately NOT parsed here — it is derived from [radio]
        # at the Config assembly site, where both sections are in scope. See the field's
        # comment on MeshtasticSettings for why it must not be operator-typed.
        snapshot_timeout_s=snapshot_timeout_s,
        fresh_within_s=positive_int("fresh_within_s", 300),
        request_timeout_s=request_timeout_s,
        node_rate_limit_s=positive_int("node_rate_limit_s", 60),
        hourly_tx_cap=positive_int("hourly_tx_cap", 30),
        request_channel_index=channel_raw,
        positions_db_path=(
            Path(str(positions_raw)).expanduser()
            if positions_raw
            else default_mesh_state_path() / "positions.db"
        ),
        daemon_socket=(
            Path(str(socket_raw)).expanduser()
            if socket_raw
            else default_mesh_state_path() / "daemon.sock"
        ),
        daemon_sweep_s=positive_float("daemon_sweep_s", 60.0),
        standby_lease_s=positive_int("standby_lease_s", 1800),
        auto_request_s=nonnegative_int("auto_request_s", 300),
        art_path=(
            Path(str(art_raw)).expanduser() if art_raw else default_playa_state_path() / "art.json"
        ),
        art_near_ft=positive_int("art_near_ft", 400),
        aliases=name_table("aliases"),
        display_names=name_table("names"),
        report_only=string_list("report_only"),
    )


def default_mesh_state_path() -> Path:
    """XDG state location for mesh-locate state (feature 111): the durable TX rate limiter.

    ``$XDG_STATE_HOME/obi/meshtastic`` (``~/.local/state`` default) — a directory, so the
    limiter (``ratelimit.json``) has room for future siblings without another helper.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "meshtastic"


def default_playa_state_path() -> Path:
    """XDG state location for the downloaded playa datasets (feature 127).

    ``$XDG_STATE_HOME/obi/playa`` (``~/.local/state`` default) — art.json and camp.json,
    the verbatim Burning Man public API arrays that `obi playa fetch` writes and every
    location answer reads. A directory, so future datasets (mutant vehicles?) have room.
    """

    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "playa"


def _parse_snmp(snmp_data: object) -> SnmpSettings | None:
    """Parse the optional ``[snmp]`` section (feature 012); ``None`` when absent.

    Validates the protocol mode: ``v2c`` requires a community string; ``v3`` requires a username and
    authentication key. Credentials are wrapped in :class:`Secret`.
    """

    if not isinstance(snmp_data, dict):
        return None

    version = str(snmp_data.get("version", "v2c")).lower()
    if version not in ("v2c", "v3"):
        raise ConfigError(f"[snmp] version must be 'v2c' or 'v3' (got {version!r})")

    community_raw = snmp_data.get("community")
    if version == "v2c" and not community_raw:
        raise ConfigError("[snmp] version 'v2c' requires a 'community' string")
    if version == "v3" and (not snmp_data.get("v3_username") or not snmp_data.get("v3_auth_key")):
        raise ConfigError("[snmp] version 'v3' requires 'v3_username' and 'v3_auth_key'")

    overrides: dict[str, SnmpOverride] = {}
    overrides_raw = snmp_data.get("overrides")
    if isinstance(overrides_raw, dict):
        for address, ov in overrides_raw.items():
            if not isinstance(ov, dict):
                continue
            ov_community = ov.get("community")
            ov_version = ov.get("version")
            overrides[str(address)] = SnmpOverride(
                community=Secret(str(ov_community)) if ov_community else None,
                version=str(ov_version).lower() if ov_version else None,
            )

    auth_key = snmp_data.get("v3_auth_key")
    priv_key = snmp_data.get("v3_priv_key")
    return SnmpSettings(
        version=version,
        community=Secret(str(community_raw)) if community_raw else None,
        port=int(snmp_data.get("port", 161)),
        timeout=float(snmp_data.get("timeout", 5.0)),
        retries=int(snmp_data.get("retries", 1)),
        profile=str(snmp_data.get("profile", "system")),
        v3_username=str(snmp_data["v3_username"]) if snmp_data.get("v3_username") else None,
        v3_auth_proto=str(snmp_data.get("v3_auth_proto", "SHA")),
        v3_auth_key=Secret(str(auth_key)) if auth_key else None,
        v3_priv_proto=str(snmp_data.get("v3_priv_proto", "AES")),
        v3_priv_key=Secret(str(priv_key)) if priv_key else None,
        overrides=overrides,
        history=_parse_snmp_history(snmp_data.get("history")),
    )


def _retain_to_days(value: object, default: int) -> int:
    """Parse a retention window given as an int (days) or a ``"90d"``/``"12w"``/``"720h"`` string.

    Returns whole days (rounded down, minimum 1). An unparseable or non-positive value is a
    :class:`ConfigError` — a silently-wrong retention could quietly discard history.
    """

    if value is None:
        return default
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ConfigError(f"[snmp.history] retain must be days or a duration (got {value!r})")
    if isinstance(value, int):
        days = value
    elif isinstance(value, str):
        text = value.strip().lower()
        unit_seconds = {"h": 3600, "d": 86400, "w": 604800}
        if text and text[-1] in unit_seconds:
            try:
                n = int(text[:-1])
            except ValueError as exc:
                raise ConfigError(f"[snmp.history] retain: bad duration {value!r}") from exc
            days = n * unit_seconds[text[-1]] // 86400
        else:
            try:
                days = int(text)
            except ValueError as exc:
                raise ConfigError(f"[snmp.history] retain: bad duration {value!r}") from exc
    else:
        raise ConfigError(f"[snmp.history] retain must be days or a duration (got {value!r})")
    if days < 1:
        raise ConfigError(f"[snmp.history] retain must be at least 1 day (got {value!r})")
    return days


def _parse_snmp_history(history_data: object) -> SnmpHistorySettings | None:
    """Parse the optional ``[snmp.history]`` subsection (feature 021); ``None`` when absent.

    Presence opts into scheduled collection. Validates the requested ``profiles`` against the
    curated registry so a typo fails fast rather than silently collecting nothing.
    """

    if not isinstance(history_data, dict):
        return None

    from obi.core.drivers.snmp_profiles import (
        PROFILES,  # local: keep drivers out of config's imports
    )

    profiles_raw = history_data.get("profiles")
    if profiles_raw is None:
        profiles = ["interfaces", "system"]
    elif isinstance(profiles_raw, list):
        profiles = [str(p) for p in profiles_raw]
    else:
        profiles = [str(profiles_raw)]
    if not profiles:
        raise ConfigError("[snmp.history] profiles must name at least one profile")
    for name in profiles:
        if name not in PROFILES:
            raise ConfigError(
                f"[snmp.history] unknown profile {name!r} — choose from {', '.join(PROFILES)}"
            )

    db_raw = history_data.get("db_path")
    return SnmpHistorySettings(
        db_path=Path(str(db_raw)).expanduser() if db_raw else default_metrics_db_path(),
        targets=str(history_data.get("targets", "")),
        profiles=profiles,
        retain_days=_retain_to_days(history_data.get("retain"), 90),
        query_buckets=int(history_data.get("query_buckets", 60)),
    )


def perms_warning(path: Path) -> str | None:
    """Public helper: warning string if ``path`` has insecure permissions, else ``None``."""

    if not path.exists():
        return None
    return _warn_if_insecure_perms(path)
