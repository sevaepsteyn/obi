"""Shared helpers for the UniFi vendor adapters — strictly read-only (Constitution I, IV).

Both UniFi surfaces — the per-console Network Integration API (``unifi_network.py``) and the
account-level Site Manager API (``unifi_sm.py``) — map device records into obi's vendor-neutral
:class:`DeviceStatus` and classify HTTP failures the same way. Those primitives live here so the
two driver modules don't duplicate them. This module and the two drivers are the only place
permitted to import ``httpx`` for UniFi (Principle IV).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

import httpx

from obi.core.models import Device, DeviceStatus, LogEvent, Reachability, SourceOutcome

# device "state"/"status" values that mean the console currently reaches the device.
_ONLINE_STATES = frozenset({"online", "connected", "active", "ready", "provisioned", "1"})


class UnifiError(Exception):
    """A normalized surface-level failure (carries the obi :class:`SourceOutcome`)."""

    def __init__(self, outcome: SourceOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


def _csrf_from_token(token: str) -> str | None:
    """Extract the ``csrfToken`` claim from a UniFi ``TOKEN`` JWT (the controller CSRF header).

    Shared by both UniFi surfaces that talk to the undocumented controller API — the feature-011
    health reads (``unifi_network.py``) and the feature-016 event ingest (``unifi_internal.py``).
    """

    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64url padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, TypeError):
        return None
    csrf = claims.get("csrfToken") if isinstance(claims, dict) else None
    return str(csrf) if csrf else None


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


def _norm_mac(value: object) -> str | None:
    """Normalize a MAC for cross-surface correlation: lowercase, separators stripped."""

    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value.lower() if ch in "0123456789abcdef")
    return cleaned or None


def _is_reachable(state: object) -> bool:
    if isinstance(state, bool):
        return state
    if isinstance(state, (int, float)):
        return int(state) == 1
    return str(state or "").strip().lower() in _ONLINE_STATES


def _summarize_temperatures(raw: dict[str, Any]) -> str | None:
    """Compact temperature note from the controller payload's temperature fields.

    Reads ``temperatures`` (a list of sensors) or ``general_temperature``; ``None`` if neither.
    """

    temps = raw.get("temperatures")
    if isinstance(temps, list) and temps:
        parts: list[str] = []
        for entry in temps:
            if not isinstance(entry, dict):
                continue
            value = _to_float(entry.get("value"))
            if value is None:
                continue
            label = _first_str(entry.get("name")) or "sensor"
            parts.append(f"{label} {value:g}°C")
        if parts:
            return "temps: " + ", ".join(parts)
    general = _to_float(raw.get("general_temperature"))
    if general is not None:
        return f"temperature {general:g}°C"
    return None


@dataclass
class _Fields:
    """The vendor-neutral fields one surface contributes for one device (pre-merge)."""

    identity: str | None = None
    address: str | None = None
    model: str | None = None
    version: str | None = None
    reachable: bool = False
    uptime: int | None = None
    cpu: float | None = None
    free_mem: int | None = None
    total_mem: int | None = None
    temps: str | None = None


def _official_fields(raw: dict[str, Any]) -> _Fields:
    """Map one official device object to :class:`_Fields` (authoritative inventory).

    Handles BOTH official shapes by reading fallbacks: the per-console Network Integration API
    (``ipAddress``/``macAddress``/``firmwareVersion``/``state``) and the account-level Site Manager
    API (``ip``/``mac``/``version``/``status``).
    """

    ip = raw.get("ipAddress") or raw.get("ip")
    if isinstance(ip, str) and "/" in ip:
        ip = ip.split("/", 1)[0]
    uptime_raw = raw.get("uptimeSec") if raw.get("uptimeSec") is not None else raw.get("uptime")
    state = raw.get("state") if raw.get("state") is not None else raw.get("status")
    return _Fields(
        identity=_first_str(raw.get("name"), raw.get("hostname")),
        address=_first_str(ip, raw.get("macAddress"), raw.get("mac"), raw.get("id")),
        model=_first_str(raw.get("model"), raw.get("modelName")),
        version=_first_str(raw.get("firmwareVersion"), raw.get("version")),
        reachable=_is_reachable(state),
        uptime=_to_int(uptime_raw),
    )


def _controller_fields(raw: dict[str, Any]) -> _Fields:
    """Map one undocumented controller ``stat/device`` object to :class:`_Fields`."""

    ip = raw.get("ip")
    if isinstance(ip, str) and "/" in ip:
        ip = ip.split("/", 1)[0]
    sys_stats = raw.get("system-stats")
    cpu = _to_float(sys_stats.get("cpu")) if isinstance(sys_stats, dict) else None
    total_mem = _to_int(raw.get("mem_total"))
    used_mem = _to_int(raw.get("mem_used"))
    free_mem = _to_int(raw.get("mem_free"))
    if free_mem is None and total_mem is not None and used_mem is not None:
        free_mem = max(total_mem - used_mem, 0)
    return _Fields(
        identity=_first_str(raw.get("name"), raw.get("hostname")),
        address=_first_str(ip, raw.get("mac"), raw.get("device_id"), raw.get("_id")),
        model=_first_str(raw.get("model"), raw.get("model_name")),
        version=_first_str(raw.get("version")),
        reachable=_is_reachable(raw.get("state")),
        uptime=_to_int(raw.get("uptime")),
        cpu=cpu,
        free_mem=free_mem,
        total_mem=total_mem,
        temps=_summarize_temperatures(raw),
    )


def status_from_fields(fields: _Fields, *, notes: list[str] | None = None) -> DeviceStatus:
    """Build a :class:`DeviceStatus` from a single surface's :class:`_Fields` (no merge)."""

    return DeviceStatus(
        device=Device(
            identity=fields.identity or "",
            address=fields.address or "",
            model=fields.model,
            version=fields.version,
        ),
        reachability=Reachability.REACHABLE if fields.reachable else Reachability.UNREACHABLE,
        uptime_seconds=fields.uptime,
        cpu_load_percent=fields.cpu,
        free_memory_bytes=fields.free_mem,
        total_memory_bytes=fields.total_mem,
        notes=list(notes or []),
    )


def _http_error(exc: httpx.HTTPError, timeout: float, surface: str) -> UnifiError:
    """Classify an ``httpx`` error into a normalized :class:`UnifiError` (never leaks a secret)."""

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return UnifiError(
                SourceOutcome.AUTH_FAILED, f"{surface} rejected the credential (HTTP {code})"
            )
        if code == 404:
            return UnifiError(
                SourceOutcome.UNREACHABLE,
                f"{surface} endpoint not found (HTTP 404) — check base_url / site",
            )
        return UnifiError(SourceOutcome.UNREACHABLE, f"{surface} returned HTTP {code}")
    if isinstance(exc, httpx.TimeoutException):
        return UnifiError(SourceOutcome.TIMED_OUT, f"{surface} did not respond within {timeout:g}s")
    if isinstance(exc, httpx.ConnectError):
        return UnifiError(
            SourceOutcome.UNREACHABLE,
            f"could not connect to the {surface} — check base_url / network",
        )
    return UnifiError(SourceOutcome.UNREACHABLE, f"{surface} request failed: {exc}")


def _unwrap(data: object, *keys: str) -> list[dict[str, Any]]:
    """Pull a list out of either a bare array or a ``{key: [...]}`` envelope."""

    if isinstance(data, dict):
        for key in keys:
            inner = data.get(key)
            if isinstance(inner, list):
                return [d for d in inner if isinstance(d, dict)]
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def site_selected(candidates: list[object], selected: list[str]) -> bool:
    """Whether a site (known by ``candidates`` — its id/name/ref) is in the operator's selection.

    An empty selection, or a selection containing ``"all"``, matches every site (the default).
    """

    if not selected or any(str(s).strip().lower() == "all" for s in selected):
        return True
    have = {str(c).strip().lower() for c in candidates if c}
    return any(str(s).strip().lower() in have for s in selected)


# --- UniFi controller event → LogEvent mapping (feature 016) -----------------------------------
#
# UniFi severities are a different vocabulary from RouterOS's; shift them down so the bulk of
# UNIFI_DEVICES events (MEDIUM mesh-flaps) stay below the watcher's default `warning` floor while
# genuine HIGH/VERY_HIGH problems cross it (FR-016, clarified 2026-06-29).
_UNIFI_SEVERITY_MAP = {
    "LOW": "info",
    "MEDIUM": "info",
    "HIGH": "warning",
    "VERY_HIGH": "critical",
}
_PLACEHOLDER_RE = re.compile(r"\{([A-Z0-9_]+)\}")


def unifi_severity(raw: object) -> str | None:
    """Map a UniFi severity (LOW/MEDIUM/HIGH/VERY_HIGH) onto the store's severity (FR-016)."""

    return _UNIFI_SEVERITY_MAP.get(str(raw).strip().upper()) if raw else None


def render_unifi_message(event: dict[str, Any]) -> str:
    """Render a human message: ``{KEY}`` in ``message_raw`` → ``parameters[KEY].name``.

    Falls back to ``title_raw`` then ``key`` when the template/parameters are missing; an
    unresolved placeholder is left verbatim rather than dropped.
    """

    params = event.get("parameters")
    template = event.get("message_raw") or event.get("title_raw") or event.get("key") or ""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = params.get(key) if isinstance(params, dict) else None
        if isinstance(value, dict):
            return str(value.get("name") or value.get("id") or match.group(0))
        return str(value) if value is not None else match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, str(template)).strip()


def unifi_event_to_log_event(
    event: dict[str, Any], *, site: str, tz: tzinfo
) -> tuple[LogEvent, str] | None:
    """Map one ``system-log/all`` event to ``(LogEvent, dedup_key)``, or ``None`` if unusable.

    ``host`` is the site (the operator's primary filter handle); the affected device stays inside
    the rendered ``message`` (FTS-searchable). ``collectors=["unifi"]`` tags the row as UniFi; the
    dedup key is the stable per-event ``id``. A row missing ``id``/``timestamp`` returns ``None``
    (the caller counts and skips it) — never raises.
    """

    event_id = event.get("id")
    ts_ms = event.get("timestamp")
    if not event_id or not isinstance(ts_ms, (int, float)) or isinstance(ts_ms, bool):
        return None
    category = str(event.get("category") or "UNKNOWN")
    key = str(event.get("key") or event.get("event") or "")
    subcategory = event.get("subcategory")
    parts = [category, str(subcategory) if subcategory else "", key]
    topics = ",".join(p for p in parts if p).lower()
    log_event = LogEvent(
        ts=datetime.fromtimestamp(ts_ms / 1000, tz=tz),
        host=site,
        topics=topics,
        message=render_unifi_message(event),
        subsystem=category.lower(),
        severity=unifi_severity(event.get("severity")),
        raw_line=json.dumps(event, separators=(",", ":"), default=str),
        collectors=["unifi"],
    )
    return log_event, f"unifi:{site}:{event_id}"
