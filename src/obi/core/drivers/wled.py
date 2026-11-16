"""WLED LED-lighting controller adapter (feature 103).

Lighting is a SEPARATE, explicitly write-capable adapter class OUTSIDE the read-only
network-vendor capability family in ``drivers/base.py`` (constitution 1.15.0, Principle IV):
this driver both reads and — uniquely among obi's drivers — writes device state, scoped by the
``[wled]`` allowlist the caller resolves names against. It is the only place permitted to speak
the WLED JSON API, and it stays total in the family's sense: every method returns a typed
outcome and never raises.

Exactly five endpoints (103 research D2; 107 adds the palette catalog): ``GET /json/info``
(identity + the ``live`` realtime flag), ``GET /json/state`` (the full lighting state — the
snapshot unit), ``GET /json/eff`` (the effect-name catalog, index = effect id),
``GET /json/pal`` (the palette-name catalog, index = palette id), ``POST /json/state``
(apply a partial state; ``"v": true`` makes the response echo the resulting full state).

Transient-read resilience (107 research D3, from the radio-call-24 field failure): GETs are
retried ONCE on the transient failure classes (HTTP 5xx, timeout, connect error); POSTs are
never retried — a write that failed ambiguously may already have applied, and the device is
shared last-writer-wins hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from obi.core.config import WledSettings


@dataclass(frozen=True)
class WledInfo:
    """Parsed subset of ``GET /json/info``."""

    name: str
    version: str
    uptime_s: int | None
    led_count: int | None
    power_estimate_mw: int | None
    wifi_rssi: int | None
    live: bool
    live_source: str


@dataclass(frozen=True)
class WledState:
    """The device's lighting state: ``raw`` is the verbatim ``/json/state`` body (what a
    snapshot stores and a revert POSTs back), the rest parsed conveniences."""

    raw: dict[str, Any]
    on: bool
    brightness: int
    primary_color: tuple[int, int, int] | None
    effect_id: int | None
    palette_id: int | None = None
    speed: int | None = None  # seg[0].sx, device scale 0-255
    intensity: int | None = None  # seg[0].ix, device scale 0-255


@dataclass(frozen=True)
class WledEffects:
    """The effect catalog from ``GET /json/eff``; list index = WLED effect id."""

    names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WledPalettes:
    """The palette catalog from ``GET /json/pal``; list index = WLED palette id."""

    names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WledResult:
    """Typed outcome of one driver call: ``ok`` + ``value`` on success, ``error`` otherwise."""

    ok: bool
    value: WledInfo | WledState | WledEffects | WledPalettes | None = None
    error: str | None = None


def _fail(error: str) -> WledResult:
    return WledResult(ok=False, error=error)


def _parse_state(data: Any) -> WledState | None:
    if not isinstance(data, dict):
        return None
    segments = data.get("seg")
    color: tuple[int, int, int] | None = None
    effect_id: int | None = None
    palette_id: int | None = None
    speed: int | None = None
    intensity: int | None = None
    if isinstance(segments, list) and segments and isinstance(segments[0], dict):
        first = segments[0]
        cols = first.get("col")
        if isinstance(cols, list) and cols and isinstance(cols[0], list) and len(cols[0]) >= 3:
            color = (int(cols[0][0]), int(cols[0][1]), int(cols[0][2]))
        if isinstance(first.get("fx"), int):
            effect_id = int(first["fx"])
        if isinstance(first.get("pal"), int):
            palette_id = int(first["pal"])
        if isinstance(first.get("sx"), int):
            speed = int(first["sx"])
        if isinstance(first.get("ix"), int):
            intensity = int(first["ix"])
    return WledState(
        raw=data,
        on=bool(data.get("on")),
        brightness=int(data.get("bri", 0)),
        primary_color=color,
        effect_id=effect_id,
        palette_id=palette_id,
        speed=speed,
        intensity=intensity,
    )


class WledDriver:
    """One driver instance per configured ``[wled]`` section; targets are per-call base URLs
    (already resolved through the allowlist by the caller — this class never sees a name)."""

    def __init__(
        self, settings: WledSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        # Test seam: httpx.MockTransport in tests; None (real network) in production.
        self._transport = transport

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        """Return parsed JSON or raise ``_WledHttpError`` with an honest, classified message.

        GETs retry ONCE on a transient failure (HTTP 5xx / timeout / connect — research D3:
        a single dropped request killed a field request on radio-call-24); POSTs never retry.
        """

        retries = 1 if method == "GET" else 0
        while True:
            try:
                return self._request_once(method, url, body)
            except _WledHttpError as exc:
                if retries > 0 and exc.transient:
                    retries -= 1
                    continue
                raise

    def _request_once(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        timeout = self._settings.timeout
        try:
            with httpx.Client(
                verify=self._settings.verify_tls, timeout=timeout, transport=self._transport
            ) as client:
                response = client.request(method, url, json=body)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise _WledHttpError(
                f"device returned HTTP {status} for {url}", transient=status >= 500
            ) from exc
        except httpx.TimeoutException as exc:
            raise _WledHttpError(
                f"device did not respond within {timeout:g}s", transient=True
            ) from exc
        except httpx.ConnectError as exc:
            raise _WledHttpError(
                "could not connect to the device — check its URL / power", transient=True
            ) from exc
        except httpx.HTTPError as exc:
            raise _WledHttpError(f"request failed: {exc}") from exc
        except ValueError as exc:  # response.json() on a non-JSON body
            raise _WledHttpError("device returned a non-JSON response") from exc

    def info(self, base_url: str) -> WledResult:
        try:
            data = self._request("GET", f"{base_url}/json/info")
        except _WledHttpError as exc:
            return _fail(str(exc))
        if not isinstance(data, dict):
            return _fail("device returned an unexpected /json/info shape")
        leds_raw = data.get("leds")
        leds: dict[str, Any] = leds_raw if isinstance(leds_raw, dict) else {}
        wifi_raw = data.get("wifi")
        wifi: dict[str, Any] = wifi_raw if isinstance(wifi_raw, dict) else {}
        return WledResult(
            ok=True,
            value=WledInfo(
                name=str(data.get("name", "?")),
                version=str(data.get("ver", "?")),
                uptime_s=int(data["uptime"]) if isinstance(data.get("uptime"), int) else None,
                led_count=int(leds["count"]) if isinstance(leds.get("count"), int) else None,
                power_estimate_mw=int(leds["pwr"]) if isinstance(leds.get("pwr"), int) else None,
                wifi_rssi=int(wifi["rssi"]) if isinstance(wifi.get("rssi"), int) else None,
                live=bool(data.get("live")),
                live_source=str(data.get("lm") or data.get("lip") or ""),
            ),
        )

    def state(self, base_url: str) -> WledResult:
        try:
            data = self._request("GET", f"{base_url}/json/state")
        except _WledHttpError as exc:
            return _fail(str(exc))
        state = _parse_state(data)
        if state is None:
            return _fail("device returned an unexpected /json/state shape")
        return WledResult(ok=True, value=state)

    def effects(self, base_url: str) -> WledResult:
        try:
            data = self._request("GET", f"{base_url}/json/eff")
        except _WledHttpError as exc:
            return _fail(str(exc))
        if not isinstance(data, list):
            return _fail("device returned an unexpected /json/eff shape")
        return WledResult(ok=True, value=WledEffects(names=[str(name) for name in data]))

    def palettes(self, base_url: str) -> WledResult:
        try:
            data = self._request("GET", f"{base_url}/json/pal")
        except _WledHttpError as exc:
            return _fail(str(exc))
        if not isinstance(data, list):
            return _fail("device returned an unexpected /json/pal shape")
        return WledResult(ok=True, value=WledPalettes(names=[str(name) for name in data]))

    def apply(self, base_url: str, partial_state: dict[str, Any]) -> WledResult:
        """POST a partial state (WLED merges it); returns the RESULTING full state."""

        body = {**partial_state, "v": True}
        try:
            data = self._request("POST", f"{base_url}/json/state", body=body)
        except _WledHttpError as exc:
            return _fail(str(exc))
        state = _parse_state(data)
        if state is None:
            return _fail("device accepted the change but returned an unexpected state shape")
        return WledResult(ok=True, value=state)


class _WledHttpError(Exception):
    """Internal transport-failure carrier; never escapes the driver. ``transient`` marks the
    failure classes worth one retry on a read (5xx / timeout / connect)."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
