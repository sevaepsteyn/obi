"""UniFi **Network Integration API** vendor adapter — strictly read-only (Constitution I, IV).

Reads one cloud-hosted UniFi console through up to two per-console surfaces and merges them per
device by MAC into obi's vendor-neutral :class:`DeviceStatus`:

- the **official** Network Integration API (``/proxy/network/integration/v1/...`` with an
  ``x-api-key`` header) — the authoritative inventory, queried **per site**; and
- the **undocumented controller** API (``/proxy/network/api/s/{site}/stat/device`` with a browser
  ``TOKEN`` cookie + a CSRF token derived from it) — richer health the official surface lacks
  (memory, temperatures, cpu/uptime).

The integration API is per-site, so this adapter resolves the operator's configured ``sites``
(one, several, or all — the default) and concatenates devices across them before merging. Only
``GET`` requests are ever issued; both transports are injectable for offline tests. The official
source is authoritative for the fields it provides; the controller source only fills gaps and
contributes controller-only devices (FR-005). Total: any failure becomes a returned
:class:`SourceReport` outcome, never an exception (FR-003).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from obi.core.audit import record_device_command
from obi.core.config import UnifiNetworkSettings
from obi.core.drivers.base import SourceReport
from obi.core.drivers.unifi_common import (
    UnifiError,
    _controller_fields,
    _csrf_from_token,
    _Fields,
    _http_error,
    _norm_mac,
    _official_fields,
    _to_int,
    _unwrap,
    site_selected,
)
from obi.core.filter import name_matches
from obi.core.models import Device, DeviceStatus, Reachability, SourceOutcome, _utcnow

# Each transport reads one surface and returns its raw device dicts (already gathered across the
# selected sites). Injecting them keeps tests offline; raising :class:`UnifiError` models a failure.
OfficialTransport = Callable[[float], list[dict[str, Any]]]
ControllerTransport = Callable[[float], list[dict[str, Any]]]

# When both surfaces are configured the single timeout budget is split; the official surface goes
# first (it resolves sites + paginates each device list), the controller surface gets the rest.
_OFFICIAL_BUDGET_FRACTION = 0.5


def _combine(
    official: _Fields | None,
    controller: _Fields | None,
    *,
    official_available: bool,
    controller_configured: bool,
    controller_available: bool,
) -> DeviceStatus:
    """Merge a device's fields from one or both surfaces into a :class:`DeviceStatus`.

    The official surface is authoritative for the fields it provides; the controller surface fills
    only what the official one left absent. Gaps are noted only for reachable devices; temperatures
    and provenance go into ``notes``.
    """

    primary = official if official is not None else controller
    secondary = controller if official is not None else None
    assert primary is not None  # at least one source contributes this device

    def pick(attr: str) -> Any:
        value = getattr(primary, attr)
        if value is None and secondary is not None:
            value = getattr(secondary, attr)
        return value

    reachable = primary.reachable
    uptime = pick("uptime")
    cpu = pick("cpu")
    free_mem = pick("free_mem")
    total_mem = pick("total_mem")
    temps = pick("temps")

    notes: list[str] = []
    if temps:
        notes.append(temps)
    if official is None and official_available:
        notes.append("seen only via the controller API (not in the integration API)")
    if reachable:  # only note gaps for devices the console can actually reach
        if not controller_configured:
            hint = " (configure the controller API token to include it)"
        elif not controller_available:
            hint = " (controller API unavailable this run)"
        else:
            hint = ""
        if uptime is None:
            notes.append("uptime unavailable")
        if cpu is None:
            notes.append(f"cpu unavailable{hint}")
        if free_mem is None:
            notes.append(f"memory unavailable{hint}")

    device = Device(
        identity=pick("identity") or "",
        address=pick("address") or "",
        model=pick("model"),
        version=pick("version"),
    )
    return DeviceStatus(
        device=device,
        reachability=Reachability.REACHABLE if reachable else Reachability.UNREACHABLE,
        uptime_seconds=uptime,
        cpu_load_percent=cpu,
        free_memory_bytes=free_mem,
        total_memory_bytes=total_mem,
        notes=notes,
    )


def _merge_devices(
    official_raw: list[dict[str, Any]],
    controller_raw: list[dict[str, Any]],
    *,
    official_available: bool,
    controller_configured: bool,
    controller_available: bool,
) -> list[DeviceStatus]:
    """Correlate the two surfaces' device lists by normalized MAC; each device appears once."""

    controller_by_mac: dict[str, _Fields] = {}
    for raw in controller_raw:
        mac = _norm_mac(raw.get("mac"))
        if mac is not None:
            controller_by_mac.setdefault(mac, _controller_fields(raw))

    statuses: list[DeviceStatus] = []
    matched: set[str] = set()
    for raw in official_raw:
        mac = _norm_mac(raw.get("macAddress") or raw.get("mac"))
        ctrl = controller_by_mac.get(mac) if mac is not None else None
        if mac is not None and ctrl is not None:
            matched.add(mac)
        statuses.append(
            _combine(
                _official_fields(raw),
                ctrl,
                official_available=official_available,
                controller_configured=controller_configured,
                controller_available=controller_available,
            )
        )

    for raw in controller_raw:
        mac = _norm_mac(raw.get("mac"))
        if mac is not None and mac in matched:
            continue
        if mac is not None:
            matched.add(mac)
        statuses.append(
            _combine(
                None,
                _controller_fields(raw),
                official_available=official_available,
                controller_configured=controller_configured,
                controller_available=controller_available,
            )
        )
    return statuses


def _compose_outcome(
    official: tuple[SourceOutcome | None, str | None],
    controller: tuple[SourceOutcome | None, str | None],
) -> tuple[SourceOutcome, str | None]:
    """Combine the two surfaces' outcomes (FR-007/FR-008)."""

    configured = [(o, d) for (o, d) in (official, controller) if o is not None]
    failures = [(o, d) for (o, d) in configured if o is not SourceOutcome.REACHABLE]

    if any(o is SourceOutcome.REACHABLE for (o, _) in configured):
        return SourceOutcome.REACHABLE, "; ".join(d for (_, d) in failures if d) or None

    detail = "; ".join(d for (_, d) in failures if d) or None
    reasons = {o for (o, _) in failures}
    if len(reasons) == 1:
        return failures[0][0], detail
    return SourceOutcome.UNREACHABLE, detail


class UnifiNetworkController:
    """Read-only per-console UniFi Network Integration adapter (an ``InventorySource``)."""

    name = "unifi-network"

    def __init__(
        self,
        settings: UnifiNetworkSettings,
        *,
        official: OfficialTransport | None = None,
        controller: ControllerTransport | None = None,
    ) -> None:
        self._settings = settings
        self._official: OfficialTransport | None = None
        if official is not None:
            self._official = official
        elif settings.api_key is not None:
            self._official = self._default_official_transport
        self._controller: ControllerTransport | None = None
        if controller is not None:
            self._controller = controller
        elif settings.token is not None:
            self._controller = self._default_controller_transport

    # --- default httpx-backed transports (the only place real requests are issued) -------------

    # feature 042: each whole transport read (site resolution + paginated devices) is one
    # audited device_command against the console host (ambient scope; no-op outside one).
    def _record_read(
        self, operation: str, start: float, *, items: int | None = None, error: str | None = None
    ) -> None:
        from urllib.parse import urlparse

        target = urlparse(self._settings.base_url).netloc or self._settings.base_url
        record_device_command(
            target,
            "http",
            operation,
            response_items=items,
            duration_ms=(time.monotonic() - start) * 1000.0,
            outcome="error" if error else "ok",
            error=error,
        )

    def _default_official_transport(self, timeout: float) -> list[dict[str, Any]]:
        base = f"{self._settings.base_url}/proxy/network/integration/v1"
        key = self._settings.api_key.reveal() if self._settings.api_key is not None else ""
        headers = {"X-API-KEY": key, "Accept": "application/json"}
        devices: list[dict[str, Any]] = []
        operation = "GET /proxy/network/integration/v1 sites+devices (paginated)"
        start = time.monotonic()
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                for site_id in self._official_site_ids(client, base, headers):
                    devices.extend(
                        self._get_paginated(client, f"{base}/sites/{site_id}/devices", headers)
                    )
            self._record_read(operation, start, items=len(devices))
            return devices
        except httpx.HTTPError as exc:
            mapped = _http_error(exc, timeout, "integration API")
            self._record_read(operation, start, error=mapped.detail)
            raise mapped from exc

    def _official_site_ids(
        self, client: httpx.Client, base: str, headers: dict[str, str]
    ) -> list[str]:
        """Resolve the integration-API site ids the operator selected (all sites by default)."""

        response = client.get(f"{base}/sites", headers=headers)
        response.raise_for_status()
        ids: list[str] = []
        for site in _unwrap(response.json(), "data", "sites"):
            candidates = [site.get("id"), site.get("name"), site.get("internalReference")]
            if site_selected(candidates, self._settings.sites) and site.get("id"):
                ids.append(str(site["id"]))
        return ids

    @staticmethod
    def _get_paginated(
        client: httpx.Client, url: str, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Follow the integration API's ``{offset,limit,count,totalCount,data}`` pages (bounded)."""

        devices: list[dict[str, Any]] = []
        offset = 0
        for _ in range(1000):  # defensive page bound
            response = client.get(url, headers=headers, params={"offset": offset, "limit": 200})
            response.raise_for_status()
            body = response.json()
            page = _unwrap(body, "data")
            devices.extend(page)
            if not isinstance(body, dict):
                break
            total = _to_int(body.get("totalCount"))
            count = _to_int(body.get("count")) or len(page)
            if total is None or not page or offset + count >= total:
                break
            offset += count
        return devices

    def _default_controller_transport(self, timeout: float) -> list[dict[str, Any]]:
        token = self._settings.token.reveal() if self._settings.token is not None else ""
        base = f"{self._settings.base_url}/proxy/network/api"
        headers = {"Accept": "application/json", "Cookie": f"TOKEN={token}"}
        csrf = _csrf_from_token(token)
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        devices: list[dict[str, Any]] = []
        operation = "GET /proxy/network/api sites+stat/device"
        start = time.monotonic()
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                for name in self._controller_site_names(client, base, headers):
                    response = client.get(f"{base}/s/{name}/stat/device", headers=headers)
                    response.raise_for_status()
                    devices.extend(_unwrap(response.json(), "data"))
            self._record_read(operation, start, items=len(devices))
            return devices
        except httpx.HTTPError as exc:
            mapped = _http_error(exc, timeout, "controller API")
            self._record_read(operation, start, error=mapped.detail)
            raise mapped from exc

    def _controller_site_names(
        self, client: httpx.Client, base: str, headers: dict[str, str]
    ) -> list[str]:
        """Resolve the undocumented-API site path segments selected (all sites by default)."""

        response = client.get(f"{base}/self/sites", headers=headers)
        response.raise_for_status()
        names: list[str] = []
        for site in _unwrap(response.json(), "data"):
            candidates = [site.get("name"), site.get("desc"), site.get("_id")]
            if site_selected(candidates, self._settings.sites) and site.get("name"):
                names.append(str(site["name"]))
        return names or ["default"]  # fall back to the implicit default site

    # --- the one public read --------------------------------------------------------------------

    def fetch(self, *, name_filter: str | None, timeout: float) -> SourceReport:
        """READ-ONLY live read of the console (both surfaces, selected sites); total + bounded."""

        if self._official is not None and self._controller is not None:
            official_budget = timeout * _OFFICIAL_BUDGET_FRACTION
            controller_budget = timeout - official_budget
        else:
            official_budget = controller_budget = timeout

        official_raw, official_outcome, official_detail = self._read(
            self._official, official_budget
        )
        controller_raw, controller_outcome, controller_detail = self._read(
            self._controller, controller_budget
        )

        official_ok = official_outcome is SourceOutcome.REACHABLE
        controller_ok = controller_outcome is SourceOutcome.REACHABLE
        statuses = _merge_devices(
            official_raw if official_ok else [],
            controller_raw if controller_ok else [],
            official_available=official_ok,
            controller_configured=self._controller is not None,
            controller_available=controller_ok,
        )

        now = _utcnow()
        matched: list[DeviceStatus] = []
        for status in statuses:
            if name_matches(status.device.identity, name_filter):
                status.gathered_at = now
                matched.append(status)

        outcome, detail = _compose_outcome(
            (official_outcome, official_detail), (controller_outcome, controller_detail)
        )
        return SourceReport(devices=matched, outcome=outcome, detail=detail)

    @staticmethod
    def _read(
        transport: OfficialTransport | ControllerTransport | None, timeout: float
    ) -> tuple[list[dict[str, Any]], SourceOutcome | None, str | None]:
        """Run one surface's transport; never raises (the source MUST be total, FR-003)."""

        if transport is None:
            return [], None, None  # surface not configured
        try:
            return list(transport(timeout)), SourceOutcome.REACHABLE, None
        except UnifiError as exc:
            return [], exc.outcome, exc.detail
        except Exception as exc:  # noqa: BLE001 - totality: any error becomes an outcome
            return [], SourceOutcome.UNREACHABLE, f"unexpected error: {exc}"
