"""UniFi **Site Manager API** vendor adapter — strictly read-only (Constitution I, IV).

Reads Ubiquiti's account-level Site Manager API (``https://api.ui.com/v1/devices``, ``X-API-KEY``)
and returns the whole-account device inventory + reachability as obi :class:`DeviceStatus`. Unlike
the per-console Network Integration adapter, this is a single account-wide read (it aggregates all
sites/consoles) and exposes no per-device cpu/memory/uptime — so those stay ``None`` without
per-device "unavailable" noise. Total: any failure becomes a returned :class:`SourceReport`
outcome, never an exception (FR-003).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from obi.core.audit import record_device_command
from obi.core.config import UnifiSiteManagerSettings
from obi.core.drivers.base import SourceReport
from obi.core.drivers.unifi_common import (
    UnifiError,
    _http_error,
    _official_fields,
    status_from_fields,
)
from obi.core.filter import name_matches
from obi.core.models import DeviceStatus, SourceOutcome, _utcnow

# A transport reads the account device list and returns the flattened raw device dicts.
Transport = Callable[[float], list[dict[str, Any]]]


def _flatten_sitemanager(body: object) -> list[dict[str, Any]]:
    """Flatten the Site Manager ``/v1/devices`` payload into a flat device list.

    The payload is ``{"data": [{"hostId": ..., "devices": [...]}, ...]}`` — devices are grouped by
    host. Tolerates a host entry that is itself a device (has ``mac``/``id``).
    """

    out: list[dict[str, Any]] = []
    data = body.get("data") if isinstance(body, dict) else body
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            inner = item.get("devices")
            if isinstance(inner, list):
                out.extend(d for d in inner if isinstance(d, dict))
            elif item.get("mac") or item.get("id"):
                out.append(item)
    return out


class UnifiSiteManagerController:
    """Read-only account-level UniFi Site Manager adapter (implements :class:`InventorySource`)."""

    name = "unifi-sm"

    def __init__(
        self, settings: UnifiSiteManagerSettings, *, transport: Transport | None = None
    ) -> None:
        self._settings = settings
        self._transport: Transport = transport or self._default_transport

    def _default_transport(self, timeout: float) -> list[dict[str, Any]]:
        """Read ``api.ui.com/v1/devices``, following ``nextToken`` pagination (bounded)."""

        from urllib.parse import urlparse

        key = self._settings.api_key.reveal()
        headers = {"X-API-KEY": key, "Accept": "application/json"}
        url = f"{self._settings.base_url}/v1/devices"
        devices: list[dict[str, Any]] = []
        params: dict[str, Any] = {"pageSize": 200}
        # feature 042: the whole paginated read is one audited device_command (ambient scope).
        target = urlparse(self._settings.base_url).netloc or self._settings.base_url
        operation = "GET /v1/devices (paginated)"
        start = time.monotonic()
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                for _ in range(1000):  # defensive page bound
                    response = client.get(url, headers=headers, params=params)
                    response.raise_for_status()
                    body = response.json()
                    devices.extend(_flatten_sitemanager(body))
                    token = body.get("nextToken") if isinstance(body, dict) else None
                    if not token:
                        break
                    params = {"pageSize": 200, "nextToken": token}
            record_device_command(
                target, "http", operation,
                response_items=len(devices),
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
            return devices
        except httpx.HTTPError as exc:
            mapped = _http_error(exc, timeout, "Site Manager API")
            record_device_command(
                target, "http", operation,
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error", error=mapped.detail,
            )
            raise mapped from exc

    def fetch(self, *, name_filter: str | None, timeout: float) -> SourceReport:
        """READ-ONLY live read of the account inventory; total (never raises) and bounded."""

        try:
            raw_devices = self._transport(timeout)
        except UnifiError as exc:
            return SourceReport(devices=[], outcome=exc.outcome, detail=exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total (FR-003)
            return SourceReport(
                devices=[], outcome=SourceOutcome.UNREACHABLE, detail=f"unexpected error: {exc}"
            )

        now = _utcnow()
        statuses: list[DeviceStatus] = []
        for raw in raw_devices:
            # Inventory + reachability only — the Site Manager API exposes no per-device health,
            # so absent cpu/memory/uptime are left as None without per-device "unavailable" noise.
            status = status_from_fields(_official_fields(raw))
            if name_matches(status.device.identity, name_filter):
                status.gathered_at = now
                statuses.append(status)
        return SourceReport(devices=statuses, outcome=SourceOutcome.REACHABLE)
