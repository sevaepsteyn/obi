"""Shared offline fakes + sample data for UniFi controller-event tests (feature 016)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from obi.core.config import UnifiInternalSettings
from obi.core.drivers.unifi_common import UnifiError


def unifi_internal_settings(**over: Any) -> UnifiInternalSettings:
    """Build a :class:`UnifiInternalSettings` for tests (Secrets pre-wrapped by load paths)."""

    from obi.core.config import Secret

    base = {
        "base_url": "https://console.unifi-hosting.example.com",
        "username": Secret("admin"),
        "password": Secret("leave-no-trace"),
        "sites": [],
        "verify_tls": True,
        "timeout": 5.0,
    }
    base.update(over)
    return UnifiInternalSettings(**base)  # type: ignore[arg-type]


def sample_events() -> list[dict[str, Any]]:
    """A small ``system-log/all`` page: a HIGH event + a MEDIUM mesh-flap (newest first)."""

    return [
        {
            "id": "evt-high-1",
            "timestamp": 1_782_444_400_000,
            "category": "UNIFI_DEVICES",
            "subcategory": "SYSTEM",
            "key": "DEVICE_LOST_CONTACT",
            "severity": "HIGH",
            "title_raw": "Device Lost Contact",
            "message_raw": "{DEVICE} lost contact with the controller.",
            "parameters": {"DEVICE": {"name": "TP: Trailer 14", "ip": "172.31.206.75"}},
        },
        {
            "id": "evt-mesh-1",
            "timestamp": 1_782_444_302_721,
            "category": "UNIFI_DEVICES",
            "subcategory": "SYSTEM_WIFI",
            "key": "DEVICE_STOP_MESH",
            "severity": "MEDIUM",
            "title_raw": "AP Stopped Mesh",
            "message_raw": "{DEVICE} stopped cedar to parent {MESH_PARENT}.",
            "parameters": {
                "DEVICE": {"name": "TP: Trailer 11"},
                "MESH_PARENT": {"name": "TP: Trailer 14"},
            },
        },
    ]


class FakeUnifiInternalClient:
    """Duck-typed stand-in for :class:`UnifiInternalClient` (matches the engine's usage)."""

    def __init__(
        self,
        settings: UnifiInternalSettings,
        *,
        sites: dict[str, str] | None = None,
        events: dict[str, list[dict[str, Any]]] | None = None,
        sites_error: UnifiError | None = None,
        site_errors: dict[str, UnifiError] | None = None,
        ignore_window: bool = False,
    ) -> None:
        self._settings = settings
        self._sites = sites if sites is not None else {"Alder": "qgjyvj7p"}
        self._events = events if events is not None else {"qgjyvj7p": sample_events()}
        self._sites_error = sites_error
        self._site_errors = site_errors or {}
        self._ignore_window = ignore_window  # CLI tests use the real clock — ignore the date window
        self.windows: list[tuple[str, int, int]] = []  # (internal_site, frm_ms, to_ms)

    def __enter__(self) -> FakeUnifiInternalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def selected_sites(self) -> dict[str, str]:
        if self._sites_error is not None:
            raise self._sites_error
        return dict(self._sites)

    def iter_events(self, internal_site: str, frm_ms: int, to_ms: int) -> Iterator[dict[str, Any]]:
        self.windows.append((internal_site, frm_ms, to_ms))
        if internal_site in self._site_errors:
            raise self._site_errors[internal_site]
        for event in self._events.get(internal_site, []):
            ts = event.get("timestamp")
            if self._ignore_window or not isinstance(ts, int) or frm_ms <= ts <= to_ms:
                yield event
