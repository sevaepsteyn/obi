"""T022: TLS verification default + the read-only GET wiring of the real httpx transport.

Exercises ``NetboxSource._default_transport`` with ``httpx.Client`` stubbed, so it asserts the
adapter passes ``verify``/``timeout``, the ``Authorization: Token`` header, and the correct URL —
and issues only a GET — without a real instance or network.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from obi.core.config import NetboxSettings, Secret
from obi.core.drivers.netbox import NetboxSource


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResponse({"results": [], "next": None})

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_verify_defaults_on_and_wires_get(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_fake_client(monkeypatch, captured)

    settings = NetboxSettings(base_url="https://netbox.example.com", token=Secret("tok-123"))
    NetboxSource(settings)._default_transport(7.0)

    assert captured["verify"] is True  # default-on (FR-018)
    assert captured["timeout"] == 7.0  # bounded (FR-012)
    assert captured["url"] == "https://netbox.example.com/api/dcim/devices/"
    assert captured["headers"]["Authorization"] == "Token tok-123"
    assert captured["headers"]["Accept"] == "application/json"


def test_verify_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_fake_client(monkeypatch, captured)

    settings = NetboxSettings(base_url="https://nb", token=Secret("t"), verify_tls=False)
    NetboxSource(settings)._default_transport(5.0)

    assert captured["verify"] is False
