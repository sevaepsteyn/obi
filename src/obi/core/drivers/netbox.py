"""NetBox source-of-truth adapter — strictly read-only (Constitution Principles I, IV).

Talks to a NetBox instance's REST API (``/api/dcim/devices/``) with an
``Authorization: Token`` header and maps each **documented** device into obi's
:class:`NetboxDevice`. NetBox is a *source of truth* (DCIM): it documents the network's intended
state, not live health — so this adapter never produces a :class:`DeviceStatus` and never
fabricates CPU/memory/uptime/reachability (FR-004). The only requests issued are paginated
``GET`` reads of the device list; no write/exec request is ever sent. This module is the only
new place permitted to import ``httpx`` (Principle IV), mirroring how it is quarantined in
``uisp.py``.

The transport is injectable (``transport=`` argument) so the field-mapping — where the NetBox
quirks live — is exercised by offline fixture tests without a real instance. See
``contracts/netbox-rest.md`` and ``contracts/documentation-source.md``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from obi.core.audit import record_device_command
from obi.core.config import NetboxSettings
from obi.core.drivers.base import DocumentationReport
from obi.core.filter import name_matches
from obi.core.models import NetboxDevice, SourceOutcome

# A transport reads the (paginated) NetBox device list and returns the accumulated raw device
# dicts. Injecting it keeps tests offline; raising :class:`NetboxError` models a source-level
# failure.
Transport = Callable[[float], list[dict[str, Any]]]

# Safety bound on pagination so a misbehaving instance (e.g. a self-referential ``next``) can
# never loop forever; far above any real inventory size.
_MAX_PAGES = 1000


class NetboxError(Exception):
    """A normalized source-level failure (carries the obi :class:`SourceOutcome`)."""

    def __init__(self, outcome: SourceOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


def _nested_name(value: object) -> str | None:
    """Return ``value["name"]`` when ``value`` is a NetBox nested object, else ``None``."""

    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name not in (None, "") else None
    return None


def _clean(value: object) -> str | None:
    """Coerce a NetBox scalar to a non-empty string, or ``None`` (``""`` is treated as unset)."""

    return str(value) if value not in (None, "") else None


def map_netbox_device(raw: dict[str, Any]) -> NetboxDevice:
    """Map one NetBox ``/api/dcim/devices/`` element to a :class:`NetboxDevice`.

    See ``contracts/netbox-rest.md``. NetBox nests related objects (``site``/``role`` →
    ``{name}``, ``device_type`` → ``{model, manufacturer:{name}}``, ``status`` →
    ``{value, label}``, ``primary_ip`` → ``{address}``). Absent attributes become ``None`` plus a
    note for the operationally notable ones (FR-005); ``status.value`` is passed through as-is and
    is documentation, NOT reachability (FR-019).
    """

    name = _clean(raw.get("name"))
    site = _nested_name(raw.get("site"))
    role = _nested_name(raw.get("role"))

    device_type = raw.get("device_type")
    model: str | None = None
    manufacturer: str | None = None
    if isinstance(device_type, dict):
        model = _clean(device_type.get("model"))
        manufacturer = _nested_name(device_type.get("manufacturer"))

    status_raw = raw.get("status")
    status = _clean(status_raw.get("value")) if isinstance(status_raw, dict) else _clean(status_raw)

    primary_ip_raw = raw.get("primary_ip")
    primary_ip = (
        _clean(primary_ip_raw.get("address")) if isinstance(primary_ip_raw, dict) else None
    )

    serial = _clean(raw.get("serial"))

    # Notes for the operationally notable absent attributes (kept light to avoid noise).
    notes: list[str] = []
    if name is None:
        notes.append("device has no name in NetBox")
    if site is None:
        notes.append("no site")
    if role is None:
        notes.append("no role")
    if primary_ip is None:
        notes.append("no primary IP")

    return NetboxDevice(
        name=name,
        site=site,
        role=role,
        model=model,
        manufacturer=manufacturer,
        status=status,
        primary_ip=primary_ip,
        serial=serial,
        notes=notes,
    )


class NetboxSource:
    """Read-only NetBox adapter implementing the :class:`DocumentationSource` protocol."""

    name = "netbox"

    def __init__(self, settings: NetboxSettings, *, transport: Transport | None = None) -> None:
        self._settings = settings
        self._transport: Transport = transport or self._default_transport

    def _default_transport(self, timeout: float) -> list[dict[str, Any]]:
        """Issue the read-only, paginated GETs against NetBox; classify failures (no raise out).

        Follows the standard NetBox envelope's ``next`` link until exhausted so the COMPLETE
        inventory is returned (FR-013); each page carries the same per-request timeout (bounded).
        """

        # feature 042: the whole paginated read is one audited device_command (ambient scope).
        from urllib.parse import urlparse

        target = urlparse(self._settings.base_url).netloc or self._settings.base_url
        start = time.monotonic()
        try:
            results = self._default_transport_impl(timeout)
        except NetboxError as exc:
            record_device_command(
                target,
                "http",
                "GET /api/dcim/devices/ (paginated)",
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error",
                error=exc.detail,
            )
            raise
        record_device_command(
            target,
            "http",
            "GET /api/dcim/devices/ (paginated)",
            response_items=len(results),
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return results

    def _default_transport_impl(self, timeout: float) -> list[dict[str, Any]]:
        url: str | None = f"{self._settings.base_url}/api/dcim/devices/"
        headers = {
            "Authorization": f"Token {self._settings.token.reveal()}",
            "Accept": "application/json",
        }
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            with httpx.Client(verify=self._settings.verify_tls, timeout=timeout) as client:
                pages = 0
                while url and pages < _MAX_PAGES:
                    if url in seen:  # defensive: a self-referential `next` must not loop forever
                        break
                    seen.add(url)
                    pages += 1
                    response = client.get(url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    if isinstance(data, dict):
                        page = data.get("results")
                        results.extend(page if isinstance(page, list) else [])
                        nxt = data.get("next")
                        url = nxt if isinstance(nxt, str) and nxt else None
                    elif isinstance(data, list):  # tolerate a non-paginated array
                        results.extend(data)
                        url = None
                    else:
                        url = None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise NetboxError(
                    SourceOutcome.AUTH_FAILED, f"NetBox rejected the API token (HTTP {code})"
                ) from exc
            if code == 404:
                raise NetboxError(
                    SourceOutcome.UNREACHABLE,
                    "device list not found (HTTP 404) — check base_url / API path",
                ) from exc
            raise NetboxError(
                SourceOutcome.UNREACHABLE, f"NetBox returned HTTP {code}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise NetboxError(
                SourceOutcome.TIMED_OUT, f"NetBox did not respond within {timeout:g}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise NetboxError(
                SourceOutcome.UNREACHABLE,
                "could not connect to NetBox — check base_url / network",
            ) from exc
        except httpx.HTTPError as exc:
            raise NetboxError(SourceOutcome.UNREACHABLE, f"NetBox request failed: {exc}") from exc

        return results

    def fetch(self, *, name_filter: str | None, timeout: float) -> DocumentationReport:
        """READ-ONLY live read of NetBox's documented inventory; total (never raises) and bounded.

        Returns the matched documented devices with the optional case-insensitive ``name_filter``
        applied; a source failure yields an empty report carrying the connection-level outcome.
        """

        try:
            raw_devices = self._transport(timeout)
        except NetboxError as exc:
            return DocumentationReport([], exc.outcome, exc.detail)
        except Exception as exc:  # noqa: BLE001 - the source MUST be total (FR-011)
            return DocumentationReport(
                [], SourceOutcome.UNREACHABLE, f"unexpected error: {exc}"
            )

        devices = [map_netbox_device(raw) for raw in raw_devices]
        matched = [d for d in devices if name_matches(d.name, name_filter)]
        return DocumentationReport(matched, SourceOutcome.REACHABLE)
