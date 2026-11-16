"""UISP controller vendor adapter — strictly read-only (Constitution Principles I, IV).

Talks to a UISP controller's REST API with an ``x-auth-token`` header. Feature 003 maps the device
list (``GET /nms/api/v2.1/devices``) into obi's vendor-neutral :class:`DeviceStatus`; feature 019
adds the controller event log (``GET /nms/api/v2.1/logs``) and device outage records
(``GET /nms/api/v2.1/outages``), mapping each into a vendor-neutral :class:`LogEvent` for the local
log store. Every request is a ``GET``; no write/exec request is ever sent. This module is the ONLY
place permitted to import ``httpx`` for UISP (Principle IV), mirroring how ``librouteros`` is
quarantined in ``mikrotik.py``.

The transports are injectable (``transport=`` for ``/devices``; ``log_transport=`` for the paginated
``/logs`` and ``/outages`` reads) so the field-mapping — where the vendor quirks live — is exercised
by offline fixture tests without a real controller. See ``003/contracts/uisp-rest.md``,
``003/contracts/inventory-source.md``, and ``019-uisp-logs/contracts/uisp-logs-rest.md``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import httpx

from obi.core.audit import record_device_command
from obi.core.config import UispSettings
from obi.core.drivers.base import SourceReport
from obi.core.filter import name_matches
from obi.core.models import (
    Device,
    DeviceStatus,
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    LogEvent,
    Reachability,
    SourceOutcome,
    UispLinkEnd,
    UispRadioHealth,
    UispWirelessLink,
    _utcnow,
)

# A transport reads the controller's device list and returns the raw device dicts. Injecting it
# keeps tests offline; raising :class:`UispError` models a controller-level failure.
Transport = Callable[[float], list[dict[str, Any]]]

# A log transport issues one paginated GET (logs/outages) and returns the parsed JSON envelope
# (``{items, pagination, …}``). Injected the same way as ``Transport`` to keep tests offline.
LogTransport = Callable[[str, dict[str, Any], float], dict[str, Any]]

# A geometry transport issues one read-only GET for an array endpoint (feature 022:
# /devices, /data-links, /sites) and returns the raw element dicts. Injected like the others.
GeoTransport = Callable[[str, float], list[dict[str, Any]]]

# overview.status values that mean the controller currently reaches the device.
_ONLINE_STATES = frozenset({"active", "connected", "online", "ready"})

# Feature 019: paging + record-mapping constants for the /logs and /outages reads.
_PAGE_SIZE = 1000  # one fleet-wide page; the endpoints are low-volume, so few pages per cycle
_MAX_PAGES = 1000  # defensive bound on a single window's page walk
# UISP log levels → the store's severity vocabulary (FR-011); `error` is the store's `critical`.
_UISP_LOG_SEVERITY = {"info": "info", "warning": "warning", "error": "critical"}
_LEVEL_ORDER = {"info": 0, "warning": 1, "error": 2}


class UispError(Exception):
    """A normalized controller-level failure (carries the obi :class:`SourceOutcome`)."""

    def __init__(self, outcome: SourceOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


def _to_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _first_str(*values: object) -> str | None:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return None


def _as_dict(value: object) -> dict[str, Any]:
    """A nested object as a dict (``{}`` when absent/non-dict) — keeps the mappers total."""

    return value if isinstance(value, dict) else {}


def _http_error(exc: httpx.HTTPError, timeout: float) -> UispError:
    """Classify an ``httpx`` error into a normalized :class:`UispError` (used by the log reads).

    Mirrors the failure mapping the feature-003 ``/devices`` transport applies inline, so the two
    UISP read paths classify identically (FR-008, contract ``uisp-logs-rest.md``).
    """

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401:
            return UispError(
                SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)"
            )
        if code == 404:
            return UispError(
                SourceOutcome.UNREACHABLE,
                "endpoint not found (HTTP 404) — check base_url / API version",
            )
        return UispError(SourceOutcome.UNREACHABLE, f"controller returned HTTP {code}")
    if isinstance(exc, httpx.TimeoutException):
        return UispError(
            SourceOutcome.TIMED_OUT, f"controller did not respond within {timeout:g}s"
        )
    if isinstance(exc, httpx.ConnectError):
        return UispError(
            SourceOutcome.UNREACHABLE, "could not connect to controller — check base_url / network"
        )
    return UispError(SourceOutcome.UNREACHABLE, f"controller request failed: {exc}")


def _parse_uisp_ts(value: object) -> datetime | None:
    """Parse a UISP timestamp to an aware UTC datetime, tolerating ISO-8601 or epoch-ms.

    ``/logs`` returns ISO-8601 with a trailing ``Z``; ``/outages`` start/end are typed as plain
    strings that may be ISO or epoch-ms across controller versions. Returns ``None`` for anything
    unparseable (the caller counts and skips it).
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _fmt_duration(delta: timedelta) -> str:
    """Compact human duration (e.g. ``2h13m``) for an outage clearance message."""

    total = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def uisp_level_allowed(level: str | None, min_level: str | None) -> bool:
    """Whether a UISP event ``level`` is at or above the operator's ``min_level`` (FR-015).

    Unknown/absent levels are admitted (never silently dropped); ``min_level`` unset admits all.
    """

    if not min_level:
        return True
    floor = _LEVEL_ORDER.get(min_level.strip().lower())
    rank = _LEVEL_ORDER.get((level or "").strip().lower())
    if floor is None or rank is None:
        return True
    return rank >= floor


def uisp_log_to_log_event(raw: dict[str, Any], *, tz: tzinfo) -> tuple[LogEvent, str] | None:
    """Map one ``GET /logs`` event to ``(LogEvent, dedup_key)``, or ``None`` if unusable.

    ``host`` is the site name (the operator's primary filter handle; ``"uisp"`` when a record has no
    site); the affected device is folded into the rendered ``message`` (FTS-searchable) and the full
    record is preserved in ``raw_line``. ``collectors=["uisp"]`` tags the row; the dedup key is the
    stable controller event ``id``. A record missing ``id``/``timestamp`` returns ``None``.
    """

    log_id = raw.get("id")
    ts = _parse_uisp_ts(raw.get("timestamp"))
    if not log_id or ts is None:
        return None
    level = str(raw.get("level") or "").strip().lower()
    site = _as_dict(raw.get("site"))
    host = _first_str(site.get("name")) or "uisp"
    device = _as_dict(raw.get("device"))
    dev_name = _first_str(device.get("name"), device.get("displayName"), device.get("hostname"))
    dev_role = str(device.get("role") or "").strip().lower()
    message = str(raw.get("message") or "").strip()
    if dev_name and dev_name.lower() not in message.lower():
        message = f"{message} (device: {dev_name})" if message else f"device: {dev_name}"
    topics = ",".join(p for p in ("uisp", "log", level, dev_role) if p)
    event = LogEvent(
        ts=ts.astimezone(tz),
        host=host,
        topics=topics,
        message=message,
        subsystem="uisp",
        severity=_UISP_LOG_SEVERITY.get(level),
        raw_line=json.dumps(raw, separators=(",", ":"), default=str),
        collectors=["uisp"],
    )
    return event, f"uisp:log:{log_id}"


def uisp_outage_to_log_events(raw: dict[str, Any], *, tz: tzinfo) -> list[tuple[LogEvent, str]]:
    """Map one ``GET /outages`` record to its onset (always) and clearance (once ended) rows.

    An outage onset is always emitted at ``severity="warning"`` (crossing the watcher's default
    floor — a device down is operator-worthy, FR-011); a clearance row at ``severity="info"`` is
    added only once ``endTimestamp`` is set, so the down duration is recoverable (FR-003). The dedup
    keys (``…:start`` / ``…:end``) make re-scanning an ongoing outage idempotent. Returns ``[]`` for
    an outage missing ``id``/``startTimestamp``.
    """

    outage_id = raw.get("id")
    start = _parse_uisp_ts(raw.get("startTimestamp"))
    if not outage_id or start is None:
        return []
    otype = str(raw.get("type") or "outage").strip().lower() or "outage"
    site = _as_dict(raw.get("site"))
    host = _first_str(site.get("name")) or "uisp"
    device = _as_dict(raw.get("device"))
    who = _first_str(device.get("name"), device.get("displayName")) or "device"
    raw_json = json.dumps(raw, separators=(",", ":"), default=str)

    events: list[tuple[LogEvent, str]] = [
        (
            LogEvent(
                ts=start.astimezone(tz),
                host=host,
                topics=f"uisp,outage,{otype}",
                message=f"{who} {otype} started",
                subsystem="uisp",
                severity="warning",
                raw_line=raw_json,
                collectors=["uisp"],
            ),
            f"uisp:outage:{outage_id}:start",
        )
    ]
    end = _parse_uisp_ts(raw.get("endTimestamp"))
    if end is not None:
        events.append(
            (
                LogEvent(
                    ts=end.astimezone(tz),
                    host=host,
                    topics=f"uisp,outage,{otype},cleared",
                    message=f"{who} {otype} cleared after {_fmt_duration(end - start)}",
                    subsystem="uisp",
                    severity="info",
                    raw_line=raw_json,
                    collectors=["uisp"],
                ),
                f"uisp:outage:{outage_id}:end",
            )
        )
    return events


def map_geo_device(raw: dict[str, Any]) -> GeoDeviceRecord | None:
    """Map one ``/devices`` element to its geometry-relevant fields (feature 022).

    Returns ``None`` for a record without a usable name. Absent fields become ``None`` —
    selection/exclusion decisions belong to the core (``geo.py``), not the adapter.
    """

    ident = _as_dict(raw.get("identification"))
    name = _first_str(ident.get("name"), ident.get("hostname"))
    if not name:
        return None
    location = _as_dict(raw.get("location"))
    overview = _as_dict(raw.get("overview"))
    site = _as_dict(ident.get("site"))
    return GeoDeviceRecord(
        name=name,
        site=_first_str(site.get("name")),
        latitude=_to_float(location.get("latitude")),
        longitude=_to_float(location.get("longitude")),
        altitude=_to_float(location.get("altitude")),
        status=_first_str(overview.get("status")),
        link_score=_to_float(overview.get("linkScore")),
        frequency_mhz=_to_float(overview.get("frequency")),
        # 044 US2: the capacity/usage sextet, verbatim (disconnected radios report nulls;
        # pre-044 captures lack the keys — both map to None, never a fabricated number).
        capacity_down_bps=_to_int(overview.get("downlinkCapacity")),
        capacity_up_bps=_to_int(overview.get("uplinkCapacity")),
        capacity_down_theoretical_bps=_to_int(overview.get("theoreticalDownlinkCapacity")),
        capacity_up_theoretical_bps=_to_int(overview.get("theoreticalUplinkCapacity")),
        utilization_down=_to_float(overview.get("downlinkUtilization")),
        utilization_up=_to_float(overview.get("uplinkUtilization")),
        # 044 US3: the operator-set flag, strict boolean-True only — absent/malformed meta
        # reads False, so a guessed flag can never mask a real outage (research R1).
        maintenance=_as_dict(raw.get("meta")).get("maintenance") is True,
    )


def map_geo_link(raw: dict[str, Any]) -> GeoLinkRecord | None:
    """Map one ``/data-links`` element (feature 022). ``None`` for a record without an id.

    Wiredness is the ``type`` field (``"wireless"``/``"ethernet"`` — fixture-verified; there
    is no ``wired`` key); endpoint names come from the embedded ``from``/``to`` devices.
    """

    link_id = _first_str(raw.get("id"))
    if not link_id:
        return None

    def _end_name(end: object) -> str | None:
        device = _as_dict(_as_dict(end).get("device"))
        ident = _as_dict(device.get("identification"))
        return _first_str(ident.get("name"), ident.get("hostname"))

    return GeoLinkRecord(
        id=link_id,
        from_name=_end_name(raw.get("from")),
        to_name=_end_name(raw.get("to")),
        link_type=_first_str(raw.get("type")),
        state=_first_str(raw.get("state")),
        signal=_to_float(raw.get("signal")),
        distance_m=_to_float(raw.get("devicesDistance")),
        site_distance_m=_to_float(raw.get("sitesDistance")),
        frequency_mhz=_to_float(raw.get("frequency")),
    )


def map_geo_site(raw: dict[str, Any]) -> GeoSiteRecord | None:
    """Map one ``/sites`` element (feature 022). ``None`` for a record without a name."""

    ident = _as_dict(raw.get("identification"))
    name = _first_str(ident.get("name"))
    if not name:
        return None
    location = _as_dict(_as_dict(raw.get("description")).get("location"))
    return GeoSiteRecord(
        name=name,
        latitude=_to_float(location.get("latitude")),
        longitude=_to_float(location.get("longitude")),
    )


def map_radio_health(raw: dict[str, Any]) -> tuple[str, UispRadioHealth] | None:
    """Map one ``/devices`` element to ``(device_id, UispRadioHealth)`` (feature 034).

    ``None`` for a record without an ``identification.id`` (the link-end join key). Total:
    absent/malformed fields (including a missing/junk ``linkScore`` sub-object, as on
    disconnected radios) become ``None`` fields, never an error.
    """

    ident = _as_dict(raw.get("identification"))
    device_id = _first_str(ident.get("id"))
    if not device_id:
        return None
    overview = _as_dict(raw.get("overview"))
    link_score = _as_dict(overview.get("linkScore"))
    return device_id, UispRadioHealth(
        status=_first_str(overview.get("status")),
        signal_dbm=_to_float(overview.get("signal")),
        frequency_mhz=_to_float(overview.get("frequency")),
        score=_to_float(link_score.get("score")),
        link_score=_to_float(link_score.get("linkScore")),
        downlink_score=_to_float(link_score.get("downlinkScore")),
        uplink_score=_to_float(link_score.get("uplinkScore")),
        airtime=_to_float(link_score.get("airTime")),
        capacity_down_bps=_to_int(link_score.get("theoreticalDownlinkCapacity")),
        capacity_up_bps=_to_int(link_score.get("theoreticalUplinkCapacity")),
    )


def map_wireless_link(
    raw: dict[str, Any],
    radios: dict[str, UispRadioHealth],
    device_sites: dict[str, str] | None = None,
) -> UispWirelessLink | None:
    """Map one ``/data-links`` element to a :class:`UispWirelessLink` (feature 034).

    ``None`` for a record without an ``id`` or whose ``type`` is not ``wireless``
    (case-insensitive) — the view promises RF health, so non-wireless records are excluded
    (FR-003). Each end joins its radio quality from ``radios`` by the controller device id; a
    join miss keeps the link with ``radio=None`` (FR-002). The end's site is the link record's
    own ``site.name`` (null across the live fleet) with the joined device's site as fallback.
    """

    link_id = _first_str(raw.get("id"))
    if not link_id or str(raw.get("type") or "").strip().lower() != "wireless":
        return None
    sites = device_sites or {}

    def _end(end: object) -> UispLinkEnd:
        end_dict = _as_dict(end)
        ident = _as_dict(_as_dict(end_dict.get("device")).get("identification"))
        device_id = _first_str(ident.get("id"))
        site = _first_str(_as_dict(end_dict.get("site")).get("name"))
        if site is None and device_id is not None:
            site = sites.get(device_id)
        return UispLinkEnd(
            name=_first_str(ident.get("name"), ident.get("hostname")),
            site=site,
            device_id=device_id,
            radio=radios.get(device_id) if device_id else None,
        )

    return UispWirelessLink(
        id=link_id,
        end_a=_end(raw.get("from")),
        end_b=_end(raw.get("to")),
        state=_first_str(raw.get("state")),
        signal_dbm=_to_float(raw.get("signal")),
        ssid=_first_str(raw.get("ssid")),
        frequency_mhz=_to_float(raw.get("frequency")),
        distance_m=_to_float(raw.get("devicesDistance")) or _to_float(raw.get("sitesDistance")),
    )


def map_device_status(raw: dict[str, Any]) -> DeviceStatus:
    """Map one UISP ``/devices`` element to a :class:`DeviceStatus`.

    See ``contracts/uisp-rest.md``. Missing metrics become ``None`` plus a note (FR-005); an
    offline device still maps (FR-004).
    """

    ident = raw.get("identification") or {}
    overview = raw.get("overview") or {}

    identity = _first_str(ident.get("name"), ident.get("hostname")) or ""
    ip = raw.get("ipAddress")
    if isinstance(ip, str) and "/" in ip:
        ip = ip.split("/", 1)[0]  # UISP returns ipAddress with a CIDR suffix, e.g. "192.0.2.5/24"
    address = _first_str(ip, ident.get("mac"), ident.get("id"), raw.get("_id")) or ""
    model = _first_str(ident.get("model"), ident.get("modelName"))
    version = _first_str(ident.get("firmwareVersion"))

    status_str = str(overview.get("status") or "").strip().lower()
    reachable = status_str in _ONLINE_STATES
    reachability = Reachability.REACHABLE if reachable else Reachability.UNREACHABLE

    uptime = _to_int(overview.get("uptime"))
    cpu = _to_float(overview.get("cpu"))
    ram = overview.get("ram")
    free_mem: int | None = None
    total_mem: int | None = None
    if isinstance(ram, dict):
        free_mem = _to_int(ram.get("free"))
        total_mem = _to_int(ram.get("total"))

    notes: list[str] = []
    if reachable:  # only note gaps for devices the controller can actually reach
        if uptime is None:
            notes.append("uptime unavailable")
        if cpu is None:
            notes.append("cpu unavailable")
        if free_mem is None:
            ram_pct = _to_float(ram) if not isinstance(ram, dict) else None
            notes.append(
                f"memory reported as {ram_pct:g}% (bytes not provided)"
                if ram_pct is not None
                else "memory unavailable"
            )

    device = Device(identity=identity, address=address, model=model, version=version)
    return DeviceStatus(
        device=device,
        reachability=reachability,
        uptime_seconds=uptime,
        cpu_load_percent=cpu,
        free_memory_bytes=free_mem,
        total_memory_bytes=total_mem,
        notes=notes,
        # 095 follow-on: same meta.maintenance read the geo mapper does (044) — the status
        # path carries it so the uisp.json snapshot (and the dash fleet tile) can see it.
        maintenance=_as_dict(raw.get("meta")).get("maintenance") is True,
    )


class UispController:
    """Read-only UISP controller adapter implementing the :class:`InventorySource` protocol."""

    name = "uisp"

    def __init__(
        self,
        settings: UispSettings,
        *,
        transport: Transport | None = None,
        log_transport: LogTransport | None = None,
        geo_transport: GeoTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport: Transport = transport or self._default_transport
        self._log_transport: LogTransport = log_transport or self._default_log_transport
        self._geo_transport: GeoTransport = geo_transport or self._default_geo_transport

    # feature 042: the controller identity used as the audit `device_command` target.
    def _audit_target(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self._settings.base_url).netloc or self._settings.base_url

    def _record_get(
        self, path: str, start: float, *, items: int | None = None, error: str | None = None
    ) -> None:
        record_device_command(
            self._audit_target(),
            "http",
            f"GET {path}",
            response_items=items,
            duration_ms=(time.monotonic() - start) * 1000.0,
            outcome="error" if error else "ok",
            error=error,
        )

    def _default_transport(self, timeout: float) -> list[dict[str, Any]]:
        """Issue the one read-only GET against the controller; classify failures (no raise out)."""

        start = time.monotonic()
        try:
            devices = self._default_transport_impl(timeout)
        except UispError as exc:
            self._record_get("/nms/api/v2.1/devices", start, error=exc.detail)
            raise
        self._record_get("/nms/api/v2.1/devices", start, items=len(devices))
        return devices

    def _default_transport_impl(self, timeout: float) -> list[dict[str, Any]]:
        url = f"{self._settings.base_url}/nms/api/v2.1/devices"
        headers = {
            "x-auth-token": self._settings.token.reveal(),
            "Accept": "application/json",
        }
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 401:
                raise UispError(
                    SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)"
                ) from exc
            if code == 404:
                raise UispError(
                    SourceOutcome.UNREACHABLE,
                    "device list not found (HTTP 404) — check base_url / API version",
                ) from exc
            raise UispError(
                SourceOutcome.UNREACHABLE, f"controller returned HTTP {code}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise UispError(
                SourceOutcome.TIMED_OUT, f"controller did not respond within {timeout:g}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise UispError(
                SourceOutcome.UNREACHABLE,
                "could not connect to controller — check base_url / network",
            ) from exc
        except httpx.HTTPError as exc:
            raise UispError(SourceOutcome.UNREACHABLE, f"controller request failed: {exc}") from exc

        # /devices returns a JSON array; tolerate an enveloped {items|devices|data: [...]}.
        if isinstance(data, dict):
            for key in ("devices", "items", "data"):
                if isinstance(data.get(key), list):
                    return list(data[key])
            return []
        return list(data) if isinstance(data, list) else []

    def _default_log_transport(
        self, path: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Issue one read-only paginated GET (``/logs`` or ``/outages``); classify failures.

        Returns the parsed JSON envelope. The only requests this method makes are GETs; no
        write/exec request is ever sent (Principle I, FR-010).
        """

        url = f"{self._settings.base_url}{path}"
        headers = {
            "x-auth-token": self._settings.token.reveal(),
            "Accept": "application/json",
        }
        start = time.monotonic()
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                response = client.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            mapped = _http_error(exc, timeout)
            self._record_get(path, start, error=mapped.detail)
            raise mapped from exc
        if isinstance(data, dict):
            items = data.get("items")
            self._record_get(
                path, start, items=len(items) if isinstance(items, list) else None
            )
            return data
        rows = list(data) if isinstance(data, list) else []
        self._record_get(path, start, items=len(rows))
        return {"items": rows}

    def _default_geo_transport(self, path: str, timeout: float) -> list[dict[str, Any]]:
        """Issue one read-only GET for an array endpoint (feature 022); classify failures.

        The only requests this method makes are GETs; no write/exec request is ever sent
        (Principle I). Tolerates an enveloped ``{items|data: [...]}`` response shape.
        """

        url = f"{self._settings.base_url}{path}"
        headers = {
            "x-auth-token": self._settings.token.reveal(),
            "Accept": "application/json",
        }
        start = time.monotonic()
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            mapped = _http_error(exc, timeout)
            self._record_get(path, start, error=mapped.detail)
            raise mapped from exc
        self._record_get(path, start)
        if isinstance(data, dict):
            for key in ("items", "data"):
                if isinstance(data.get(key), list):
                    return list(data[key])
            return []
        return list(data) if isinstance(data, list) else []

    def geometry(self, *, timeout: float) -> GeometrySnapshot:
        """READ-ONLY (feature 022): the three geometry reads, minimally shaped.

        Exactly three GETs — ``/devices``, ``/data-links``, ``/sites`` (the allowlist in
        ``022/contracts/uisp-geometry.md``). Raises :class:`UispError` on a controller-level
        failure, exactly like the log reads (the engine records the failed audit and the
        export fails cleanly, leaving any prior export intact — FR-008).
        """

        devices_raw = self._geo_transport("/nms/api/v2.1/devices", timeout)
        links_raw = self._geo_transport("/nms/api/v2.1/data-links", timeout)
        sites_raw = self._geo_transport("/nms/api/v2.1/sites", timeout)
        return GeometrySnapshot(
            devices=[d for d in map(map_geo_device, devices_raw) if d is not None],
            links=[link for link in map(map_geo_link, links_raw) if link is not None],
            sites=[s for s in map(map_geo_site, sites_raw) if s is not None],
        )

    def links(self, *, timeout: float) -> list[UispWirelessLink]:
        """READ-ONLY (feature 034): the wireless link-health view, fully joined.

        Exactly two GETs — ``/data-links`` then ``/devices`` (the allowlist in
        ``034/contracts/uisp-links-rest.md``), via the same geo transport as ``geometry()``.
        Non-wireless links are excluded; each end joins its radio quality by controller device
        id (a miss keeps the link). Raises :class:`UispError` on a controller-level failure —
        no partial view is ever returned.
        """

        links_raw = self._geo_transport("/nms/api/v2.1/data-links", timeout)
        devices_raw = self._geo_transport("/nms/api/v2.1/devices", timeout)
        radios: dict[str, UispRadioHealth] = {}
        device_sites: dict[str, str] = {}
        for raw in devices_raw:
            mapped = map_radio_health(raw)
            if mapped is None:
                continue
            device_id, radio = mapped
            radios[device_id] = radio
            site = _first_str(_as_dict(_as_dict(raw.get("identification")).get("site")).get("name"))
            if site:
                device_sites[device_id] = site
        return [
            link
            for link in (map_wireless_link(raw, radios, device_sites) for raw in links_raw)
            if link is not None
        ]

    @staticmethod
    def _page_items(envelope: dict[str, Any]) -> list[dict[str, Any]]:
        """The ``items`` array from a ``/logs`` or ``/outages`` envelope (tolerant of shapes)."""

        items = envelope.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
        return []

    @staticmethod
    def _page_total(envelope: dict[str, Any]) -> int | None:
        """``pagination.pages`` from an envelope, or ``None`` if not reported."""

        pagination = envelope.get("pagination")
        if isinstance(pagination, dict):
            pages = pagination.get("pages")
            if isinstance(pages, (int, float)) and not isinstance(pages, bool):
                return int(pages)
        return None

    def _iter_records(
        self, path: str, start_ms: int, *, timeout: float, count: int = _PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        """Page a window of ``/logs`` or ``/outages`` records (1-based pages; bounded).

        Stops on a short/empty page or once ``page`` reaches the reported ``pagination.pages``.
        Read-only; raises :class:`UispError` (the engine isolates it per source).
        """

        for page in range(1, _MAX_PAGES + 1):
            params = {"count": count, "page": page, "start": start_ms}
            envelope = self._log_transport(path, params, timeout)
            rows = self._page_items(envelope)
            yield from rows
            pages = self._page_total(envelope)
            if len(rows) < count or (pages is not None and page >= pages):
                return

    def iter_logs(
        self, *, start_ms: int, timeout: float, count: int = _PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        """Yield raw ``GET /logs`` events from ``start_ms`` to now (paged, bounded)."""

        return self._iter_records("/nms/api/v2.1/logs", start_ms, timeout=timeout, count=count)

    def iter_outages(
        self, *, start_ms: int, timeout: float, count: int = _PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        """Yield raw ``GET /outages`` records from ``start_ms`` to now (paged, bounded)."""

        return self._iter_records("/nms/api/v2.1/outages", start_ms, timeout=timeout, count=count)

    def fetch(self, *, name_filter: str | None, timeout: float) -> SourceReport:
        """READ-ONLY live read of the controller; total (never raises) and bounded by timeout."""

        try:
            raw_devices = self._transport(timeout)
        except UispError as exc:
            return SourceReport(devices=[], outcome=exc.outcome, detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total (FR-011)
            return SourceReport(
                devices=[], outcome=SourceOutcome.UNREACHABLE, detail=f"unexpected error: {exc}"
            )

        statuses: list[DeviceStatus] = []
        now = _utcnow()
        for raw in raw_devices:
            status = map_device_status(raw)
            if name_matches(status.device.identity, name_filter):
                status.gathered_at = now
                statuses.append(status)
        return SourceReport(devices=statuses, outcome=SourceOutcome.REACHABLE)
