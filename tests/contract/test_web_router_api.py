"""Contract tests: GET /api/router.json — the web surface's one LIVE device-read route (047).

Real behavioral tests against the real TLS server (the 038 data-api pattern). They pin
specs/047-ros-detail/contracts/router-detail-api.md for the paths that DO NOT contact a
device — absent config, a missing param, a POST, and an unknown-device query (resolution
fails before any connection). The reachable path (a live read of a real router) is the
engine unit test's job (fake driver) and the quickstart manual pass (real hardware) — no
device is ever contacted from a test (constitution II: production-only network).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx

from obi.core.config import Config
from obi.core.inventory import Inventory, InventoryEntry, save
from obi.core.models import DiscoveryScope, DiscoveryState
from obi.web.server import create_server


def _serve(tmp_path: Path, **kwargs: object) -> Iterator[str]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export_path, state_dir=tmp_path / "state",
        **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def _config_with_roster(tmp_path: Path) -> Config:
    from tests.fakes import settings as mikrotik_settings

    inv_path = tmp_path / "inventory.json"
    save(
        Inventory(
            scope=DiscoveryScope(seeds=["172.18.81.1"]),
            entries=[
                InventoryEntry(address="172.18.81.1", identity="core-gw-1",
                               discovery_state=DiscoveryState.IDENTIFIED),
            ],
        ),
        inv_path,
    )
    return Config(
        scope=DiscoveryScope(seeds=["172.18.81.1"]),
        routeros=mikrotik_settings(),
        inventory_path=inv_path,
        audit_path=tmp_path / "audit.jsonl",
    )


def test_router_route_absent_when_no_routeros_configured(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        payload = httpx.get(f"{base}/api/router.json?device=core-gw-1",
                            verify=False, timeout=5.0).json()
        assert payload["store"] == "absent"
        assert "[routeros]" in payload["hint"]
        assert payload["device"] is None
        assert payload["read"]["outcome"] == "unknown_device"
        assert payload["interfaces"] == [] and payload["addresses"] == []


def test_router_route_requires_a_device_param(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        response = httpx.get(f"{base}/api/router.json", verify=False, timeout=5.0)
        assert response.status_code == 400
        assert "device is required" in response.json()["error"]


def test_router_route_unknown_device_is_a_state(tmp_path: Path) -> None:
    # A real [routeros] config + roster, but the requested device is not in it: resolution
    # fails and returns a 200 state WITHOUT contacting any device.
    config = _config_with_roster(tmp_path)
    for base in _serve(tmp_path, configs_app_config=config):
        payload = httpx.get(f"{base}/api/router.json?device=ghost-gw",
                            verify=False, timeout=5.0).json()
        assert payload["store"] == "ok"
        assert payload["read"]["outcome"] == "unknown_device"
        assert payload["device"] is None
    # the unknown-device read is still audited (Principle V)
    trail = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "router_detail" in trail


def test_router_route_rejects_post(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        response = httpx.post(f"{base}/api/router.json?device=core-gw-1",
                              verify=False, timeout=5.0)
        assert response.status_code == 405
