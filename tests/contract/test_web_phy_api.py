"""Contract tests: GET /api/phy.json — the web surface's SECOND live device-read route (075).

Real behavioral tests against the real TLS server (the 038/047 data-api pattern). They pin
specs/075-router-phy/contracts/phy-api.md for the paths that DO NOT contact a device — absent
config, a missing param, a POST, HEAD parity, and an unknown-device query (resolution fails
before any connection). The reachable path (a live read of a real router) is the engine unit
test's job (fake driver) and the quickstart manual pass (real hardware) — no device is ever
contacted from a test (constitution II: production-only network). US5 appends the metrics tests.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from obi.core.config import Config, SnmpHistorySettings
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
                InventoryEntry(address="172.18.81.1", identity="chestnut-gw",
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


def test_phy_route_absent_when_no_routeros_configured(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        payload = httpx.get(f"{base}/api/phy.json?device=chestnut-gw",
                            verify=False, timeout=5.0).json()
        assert payload["store"] == "absent"
        assert "[routeros]" in payload["hint"]
        assert payload["device"] is None
        assert payload["read"]["outcome"] == "unknown_device"
        assert payload["ports"] == []
        # the metrics envelope is always present, independent of the live view
        assert payload["metrics"]["store"] == "absent"
        assert payload["metrics"]["error_increases"] == []


def test_phy_route_requires_a_device_param(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        response = httpx.get(f"{base}/api/phy.json", verify=False, timeout=5.0)
        assert response.status_code == 400
        assert "device is required" in response.json()["error"]


def test_phy_route_no_inventory_is_absent_state(tmp_path: Path) -> None:
    from tests.fakes import settings as mikrotik_settings

    config = Config(
        scope=DiscoveryScope(seeds=["172.18.81.1"]),
        routeros=mikrotik_settings(),
        inventory_path=tmp_path / "missing-inventory.json",
        audit_path=tmp_path / "audit.jsonl",
    )
    for base in _serve(tmp_path, configs_app_config=config):
        payload = httpx.get(f"{base}/api/phy.json?device=chestnut-gw",
                            verify=False, timeout=5.0).json()
        assert payload["store"] == "absent"
        assert "discover" in payload["hint"]
        assert payload["device"] is None


def test_phy_route_unknown_device_is_a_state(tmp_path: Path) -> None:
    # A real [routeros] config + roster, but the requested device is not in it: resolution fails
    # and returns a 200 state WITHOUT contacting any device.
    config = _config_with_roster(tmp_path)
    for base in _serve(tmp_path, configs_app_config=config):
        payload = httpx.get(f"{base}/api/phy.json?device=ghost-gw",
                            verify=False, timeout=5.0).json()
        assert payload["store"] == "ok"
        assert payload["read"]["outcome"] == "unknown_device"
        assert payload["device"] is None
        assert payload["metrics"]["store"] == "absent"
    # the unknown-device read is still audited (Principle V)
    trail = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "router_phy" in trail


def test_phy_route_rejects_post(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        response = httpx.post(f"{base}/api/phy.json?device=chestnut-gw",
                              verify=False, timeout=5.0)
        assert response.status_code == 405


def test_phy_route_head_parity(tmp_path: Path) -> None:
    for base in _serve(tmp_path):
        response = httpx.head(f"{base}/api/phy.json?device=chestnut-gw", verify=False, timeout=5.0)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")


# --- US5: the independent metrics envelope (error-counter increases) --------------------------


def _seed_metrics(db_path: Path, rows: list[tuple[str, str, str, int, datetime]]) -> None:
    """Seed (device, ifname, metric, value, ts) rows into a temp metrics store (021 schema)."""
    from obi.core import metricstore
    from obi.core.metricstore import StoredSample

    conn = metricstore.connect(db_path)
    metricstore.insert_samples(conn, [
        StoredSample(
            ts_us=int(ts.timestamp() * 1_000_000), ts_iso=ts.isoformat(),
            device=device, address="192.0.2.10", profile="interfaces",
            metric=metric, ifindex=ifname, ifname=ifname, kind="counter", value=value,
        )
        for device, ifname, metric, value, ts in rows
    ])
    conn.commit()
    conn.close()


def _config_at_unroutable(tmp_path: Path, identity: str = "core-gw-1") -> Config:
    # A roster device at an unroutable TEST-NET address (RFC5737): the live read fails fast without
    # contacting any real fleet device, so the metrics envelope can be asserted independently.
    from tests.fakes import settings as mikrotik_settings

    inv_path = tmp_path / "inventory.json"
    save(
        Inventory(
            scope=DiscoveryScope(seeds=["192.0.2.10"]),
            entries=[InventoryEntry(address="192.0.2.10", identity=identity,
                                    discovery_state=DiscoveryState.IDENTIFIED)],
        ),
        inv_path,
    )
    return Config(
        scope=DiscoveryScope(seeds=["192.0.2.10"]),
        routeros=mikrotik_settings(),
        inventory_path=inv_path,
        audit_path=tmp_path / "audit.jsonl",
        timeout=1.0,  # bound the (failing) live read so the test stays quick
    )


def test_phy_metrics_facts_from_seeded_store(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    db = tmp_path / "metrics.db"
    _seed_metrics(db, [
        ("core-gw-1", "ether3", "ifInErrors", 100, now - timedelta(minutes=30)),
        ("core-gw-1", "ether3", "ifInErrors", 217, now - timedelta(minutes=1)),  # +117
        ("core-gw-1", "wireguard1", "ifOutErrors", 5, now - timedelta(minutes=30)),
        ("core-gw-1", "wireguard1", "ifOutErrors", 900, now - timedelta(minutes=1)),  # ignored
        ("other-gw", "ether1", "ifInErrors", 1, now - timedelta(minutes=30)),
        ("other-gw", "ether1", "ifInErrors", 999, now - timedelta(minutes=1)),  # other device
    ])
    config = _config_at_unroutable(tmp_path)
    metrics_settings = SnmpHistorySettings(db_path=db)
    for base in _serve(tmp_path, configs_app_config=config, metrics_settings=metrics_settings):
        payload = httpx.get(f"{base}/api/phy.json?device=core-gw-1",
                            verify=False, timeout=10.0).json()
        metrics = payload["metrics"]
        assert metrics["store"] == "ok"
        assert metrics["window_seconds"] == 3600
        names = {(f["ifname"], f["metric"]) for f in metrics["error_increases"]}
        assert ("ether3", "ifInErrors") in names  # the real climbing counter
        assert ("wireguard1", "ifOutErrors") not in names  # ignore-list filtered
        assert all(f["ifname"] != "ether1" for f in metrics["error_increases"])  # other device


def test_phy_metrics_absent_when_no_store(tmp_path: Path) -> None:
    config = _config_at_unroutable(tmp_path)
    for base in _serve(tmp_path, configs_app_config=config):  # no metrics_settings
        payload = httpx.get(f"{base}/api/phy.json?device=core-gw-1",
                            verify=False, timeout=10.0).json()
        assert payload["metrics"]["store"] == "absent"
        assert payload["metrics"]["error_increases"] == []
