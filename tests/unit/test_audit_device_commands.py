"""Unit (feature 042, T011): every instrumented driver transport emits `device_command` events.

Each driver is exercised offline (fake connect/session/exec/transport); with an active audit
scope the transport seam records one event carrying the right transport/operation/target, and
with no scope the drivers behave identically and record nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from obi.core import auditstore
from obi.core.audit import AuditRecorder, audit_scope
from obi.core.config import AuditSettings, Secret, UnifiInternalSettings
from obi.core.drivers.mikrotik import MikrotikDriver
from obi.core.drivers.netbox import NetboxSource
from obi.core.drivers.routeros_config import RouterosConfigDriver
from obi.core.drivers.siklu import SikluDriver
from obi.core.drivers.uisp import UispController
from obi.core.drivers.unifi_internal import UnifiInternalClient
from obi.core.drivers.unifi_network import UnifiNetworkController
from obi.core.drivers.unifi_sm import UnifiSiteManagerController
from tests.fakes import (
    config_backup_settings,
    netbox_settings,
    settings,
    siklu_settings,
    uisp_settings,
    unifi_network_settings,
    unifi_sm_settings,
)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    recorder = AuditRecorder(AuditSettings(db_path=tmp_path / "audit.db"))
    with audit_scope(recorder, "cli:test", "cli-test-1"):
        yield tmp_path / "audit.db"
    recorder.close()


def _events(db: Path) -> list[dict[str, Any]]:
    if not db.exists():
        return []
    conn = auditstore.connect_readonly(db)
    try:
        rows, _ = auditstore.query_events(conn, limit=100, include_payload=True)
        return list(reversed(rows))
    finally:
        conn.close()


def _device_commands(db: Path) -> list[dict[str, Any]]:
    return [e for e in _events(db) if e["kind"] == "device_command"]


# --- mikrotik (routeros-api) --------------------------------------------------------------


def test_mikrotik_probe_records_error(store: Path) -> None:
    def connect(address: str, timeout: float) -> Any:
        raise ConnectionRefusedError("refused")

    driver = MikrotikDriver(settings(), connect=connect)
    driver.probe("172.25.110.1", timeout=1.0)
    (event,) = _device_commands(store)
    assert event["payload"]["transport"] == "routeros-api"
    assert event["target"] == "172.25.110.1"
    assert "probe" in event["payload"]["operation"]
    assert event["outcome"] == "error"
    assert event["duration_ms"] is not None


def test_mikrotik_ping_records_operation(store: Path) -> None:
    def connect(address: str, timeout: float) -> Any:
        raise TimeoutError("no route")

    driver = MikrotikDriver(settings(), connect=connect)
    driver.ping("172.25.110.1", "172.25.110.9", count=3, timeout=1.0)
    (event,) = _device_commands(store)
    assert event["payload"]["operation"] == "/ping 172.25.110.9 count=3"
    assert event["outcome"] == "error"


def test_mikrotik_no_scope_records_nothing(tmp_path: Path) -> None:
    def connect(address: str, timeout: float) -> Any:
        raise ConnectionRefusedError("refused")

    driver = MikrotikDriver(settings(), connect=connect)
    result = driver.probe("172.25.110.1", timeout=1.0)
    assert result.outcome.value != "identified"  # behavior unchanged
    assert not (tmp_path / "audit.db").exists()


# --- routeros_config (ssh /export) ---------------------------------------------------------


def test_config_export_records_ssh_command(store: Path, tmp_path: Path) -> None:
    def exec_fn(host: str, command: str, timeout: float) -> str:
        return "/interface ethernet\nset 0 name=ether1\n"

    driver = RouterosConfigDriver(config_backup_settings(tmp_path), exec_fn=exec_fn)
    result = driver.fetch_export("172.25.110.2", timeout=1.0)
    assert result.content is not None
    (event,) = _device_commands(store)
    assert event["payload"]["transport"] == "ssh"
    assert "/export" in event["payload"]["operation"]
    assert event["payload"]["response_bytes"] > 0
    assert event["outcome"] == "ok"


# --- siklu (ssh show commands) --------------------------------------------------------------


def test_siklu_records_each_show_command(store: Path) -> None:
    class FakeSession:
        def run(self, command: str) -> str:
            return ""

        def close(self) -> None:
            return None

    driver = SikluDriver(
        siklu_settings(hosts=["172.25.110.3"]), session=lambda host, timeout: FakeSession()
    )
    driver.fetch_radio("172.25.110.3", timeout=1.0)
    commands = [e["payload"]["operation"] for e in _device_commands(store)]
    assert "show system" in commands
    assert "show rf" in commands
    assert len(commands) == 6
    assert all(e["payload"]["transport"] == "ssh" for e in _device_commands(store))


# --- snmp (get/walk) ------------------------------------------------------------------------


def test_snmp_walk_records_one_event(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from obi.core.drivers.snmp import _PysnmpTransport

    async def fake_walk(self: Any, base_oid: str) -> list[tuple[object, object]]:
        return [("1.3.6.1.2.1.2.2.1.2.1", "ether1")]

    monkeypatch.setattr(_PysnmpTransport, "_async_walk", fake_walk)
    transport = _PysnmpTransport("172.25.110.4", object())  # type: ignore[arg-type]
    transport.walk("1.3.6.1.2.1.2.2.1.2")
    (event,) = _device_commands(store)
    assert event["payload"]["transport"] == "snmp"
    assert event["payload"]["operation"] == "walk 1.3.6.1.2.1.2.2.1.2"
    assert event["payload"]["response_items"] == 1


# --- uisp / netbox (http, impl-wrapped) ------------------------------------------------------


def test_uisp_devices_read_records(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = UispController(uisp_settings())
    monkeypatch.setattr(controller, "_default_transport_impl", lambda timeout: [{}, {}])
    controller._default_transport(1.0)
    (event,) = _device_commands(store)
    assert event["payload"]["transport"] == "http"
    assert event["payload"]["operation"] == "GET /nms/api/v2.1/devices"
    assert event["payload"]["response_items"] == 2


def test_netbox_read_records(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = NetboxSource(netbox_settings())
    monkeypatch.setattr(source, "_default_transport_impl", lambda timeout: [{}])
    source._default_transport(1.0)
    (event,) = _device_commands(store)
    assert event["payload"]["operation"] == "GET /api/dcim/devices/ (paginated)"
    assert event["payload"]["response_items"] == 1


# --- unifi (http) ----------------------------------------------------------------------------


def test_unifi_network_official_read_records(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = UnifiNetworkController(unifi_network_settings())
    monkeypatch.setattr(
        controller, "_official_site_ids", lambda client, base, headers: []
    )
    controller._default_official_transport(1.0)
    (event,) = _device_commands(store)
    assert event["payload"]["transport"] == "http"
    assert "integration" in event["payload"]["operation"]
    assert event["outcome"] == "ok"


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200
        self.content = b"{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeHttpxClient:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeHttpxClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse({"data": [], "nextToken": None})


def test_unifi_sm_read_records(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import obi.core.drivers.unifi_sm as sm

    monkeypatch.setattr(sm.httpx, "Client", _FakeHttpxClient)
    controller = UnifiSiteManagerController(unifi_sm_settings())
    controller._default_transport(1.0)
    (event,) = _device_commands(store)
    assert event["payload"]["operation"] == "GET /v1/devices (paginated)"
    assert event["outcome"] == "ok"


def test_unifi_internal_request_records(store: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    settings_ = UnifiInternalSettings(
        base_url="https://172.25.110.6",
        username=Secret("admin"),
        password=Secret("pw"),
    )
    client = UnifiInternalClient(
        settings_,
        client_factory=lambda: httpx.Client(
            base_url="https://172.25.110.6", transport=httpx.MockTransport(handler)
        ),
    )
    client._csrf = "token"  # skip login; exercise the request seam directly
    client._request("GET", "/proxy/network/api/self/sites")
    (event,) = _device_commands(store)
    assert event["target"] == "172.25.110.6"
    assert event["payload"]["operation"] == "GET /proxy/network/api/self/sites"
    assert event["outcome"] == "ok"
