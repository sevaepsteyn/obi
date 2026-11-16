"""T008: TLS verification default + read-only GET wiring of the Network adapter's two transports.

Stubs ``httpx.Client`` so it asserts the adapter passes ``verify``/``timeout``, sends the right
auth headers (``x-api-key`` for the official surface, a ``TOKEN`` cookie for the controller
surface), resolves + sweeps the selected sites, and issues ONLY GETs — without a real console.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from obi.core.config import Secret, UnifiNetworkSettings
from obi.core.drivers.unifi_network import UnifiNetworkController


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    captured.setdefault("gets", [])

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def get(
            self,
            url: str,
            headers: dict[str, str] | None = None,
            params: dict[str, Any] | None = None,
        ) -> _FakeResponse:
            captured["gets"].append({"url": url, "headers": headers, "params": params})
            captured["headers"] = headers
            if url.endswith("/integration/v1/sites"):
                return _FakeResponse(
                    {
                        "data": [
                            {"id": "s1", "name": "Default", "internalReference": "default"},
                            {"id": "s2", "name": "SF", "internalReference": "brc206c9"},
                        ]
                    }
                )
            if url.endswith("/self/sites"):
                return _FakeResponse({"data": [{"name": "default", "desc": "Default"}]})
            if url.endswith("/stat/device"):
                return _FakeResponse({"meta": {"rc": "ok"}, "data": []})
            return _FakeResponse({"data": [], "totalCount": 0, "count": 0})  # per-site devices

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_official_transport_sweeps_all_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_fake_client(monkeypatch, captured)

    settings = UnifiNetworkSettings(base_url="https://c.example.com", api_key=Secret("key-123"))
    UnifiNetworkController(settings)._default_official_transport(7.0)

    assert captured["verify"] is True  # cloud certs valid → default-on
    assert captured["timeout"] == 7.0  # bounded
    assert captured["headers"]["X-API-KEY"] == "key-123"
    device_gets = [g["url"] for g in captured["gets"] if g["url"].endswith("/devices")]
    base = "https://c.example.com/proxy/network/integration/v1"
    # both sites swept (no `sites` filter configured → all)
    assert device_gets == [f"{base}/sites/s1/devices", f"{base}/sites/s2/devices"]


def test_official_transport_honors_site_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_fake_client(monkeypatch, captured)

    settings = UnifiNetworkSettings(
        base_url="https://c.example.com", api_key=Secret("k"), sites=["SF"]
    )
    UnifiNetworkController(settings)._default_official_transport(5.0)

    device_gets = [g["url"] for g in captured["gets"] if g["url"].endswith("/devices")]
    assert device_gets == ["https://c.example.com/proxy/network/integration/v1/sites/s2/devices"]


def test_controller_transport_verify_and_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_fake_client(monkeypatch, captured)

    settings = UnifiNetworkSettings(
        base_url="https://c.example.com", token=Secret("tok-123"), verify_tls=False
    )
    UnifiNetworkController(settings)._default_controller_transport(5.0)

    assert captured["verify"] is False
    assert captured["timeout"] == 5.0
    last = captured["gets"][-1]
    assert last["url"] == "https://c.example.com/proxy/network/api/s/default/stat/device"
    assert last["headers"]["Cookie"] == "TOKEN=tok-123"
