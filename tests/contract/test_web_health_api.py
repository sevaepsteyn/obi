"""Contract (feature 096): /api/health.json — the fleet-health camp-1 route.

One test per contract sentence (contracts/health-api.md): fleet mode pre-sorted
worst-first with per-device summary/breaches; ?device= mode with recent series; absent
store names the CLI fix; GET/HEAD only; staleness and absence rendered honestly (nothing
fabricated). Store seeded via production insert code; real TLS server on port 0.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from obi.core import metricstore
from obi.core.config import Config, SnmpHistorySettings, SnmpSettings
from obi.core.config import Secret as ConfigSecret
from obi.core.metricstore import StoredSample
from obi.web.server import create_server

NOW_US = int(datetime.now(UTC).timestamp() * 1_000_000)


def _row(device: str, metric: str, value: int, *, age_s: float = 60, ifindex: str = "",
         ifname: str | None = None, profile: str = "health") -> StoredSample:
    ts_us = NOW_US - int(age_s * 1_000_000)
    return StoredSample(
        ts_us=ts_us,
        ts_iso=datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address="172.18.81.1",
        profile=profile,
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="gauge",
        value=value,
    )


def _seed(db: Path) -> None:
    conn = metricstore.connect(db)
    rows = [
        # healthy gateway, several classes present
        _row("chestnut-gw", "health:cpu-temperature", 350, ifindex="17",
             ifname="cpu-temperature"),
        _row("chestnut-gw", "health:psu1-state", 0, ifindex="7401", ifname="psu1-state"),
        _row("chestnut-gw", "cpuLoad", 4, profile="system"),
        _row("chestnut-gw", "optics:rxPowerMdbm", -1907, ifindex="2", ifname="sfp-sfpplus1",
             profile="optics"),
        # breaching gateway: board over 75 °C AND a failed PSU state (hard failure ⇒ down)
        _row("hot-gw", "health:board-temperature2", 810, ifindex="7102",
             ifname="board-temperature2"),
        _row("hot-gw", "health:psu2-state", 1, ifindex="7402", ifname="psu2-state"),
        # stale device: last sample two hours sequoia
        _row("sequoia-gw", "health:cpu-temperature", 400, age_s=7200, ifindex="17",
             ifname="cpu-temperature"),
        # linux collector
        _row("core-vms-1", "linux:diskUsedPercent", 423, ifindex="/", ifname="/",
             profile="linux-health"),
    ]
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()


def _config(tmp_path: Path, db: Path) -> Config:
    return Config(
        snmp=SnmpSettings(
            community=ConfigSecret("public"),
            history=SnmpHistorySettings(db_path=db),
        ),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )


def _serve(tmp_path: Path, config: Config | None) -> Iterator[str]:
    export_path = tmp_path / "links.json"
    export_path.write_text("{}")
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        configs_app_config=config,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[str]:
    db = tmp_path / "metrics.db"
    _seed(db)
    yield from _serve(tmp_path, _config(tmp_path, db))


@pytest.fixture()
def storeless(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, _config(tmp_path, tmp_path / "never.db"))


def _get(base: str, query: str = "") -> httpx.Response:
    return httpx.get(f"{base}/api/health.json{query}", verify=False, timeout=10.0)


def test_fleet_mode_is_worst_first_with_summaries(served: str) -> None:
    payload = _get(served).json()
    assert payload["store"] == "ok"
    order = [d["device"] for d in payload["devices"]]
    # down (hard failure) > warn > stale > ok
    assert order.index("hot-gw") < order.index("sequoia-gw")
    assert order.index("sequoia-gw") < order.index("chestnut-gw")
    by_name = {d["device"]: d for d in payload["devices"]}
    assert by_name["hot-gw"]["worst"] == "down"  # psu state = hard failure
    assert by_name["sequoia-gw"]["worst"] == "stale"
    assert by_name["chestnut-gw"]["worst"] == "ok"
    assert by_name["core-vms-1"]["kind"] == "linux"
    assert by_name["chestnut-gw"]["summary"]["temperature_c"] == 35.0
    assert by_name["core-vms-1"]["summary"]["disk_max_percent"] == 42.3


def test_breaches_carry_value_and_limit(served: str) -> None:
    payload = _get(served).json()
    hot = next(d for d in payload["devices"] if d["device"] == "hot-gw")
    classes = {b["class"] for b in hot["breaches"]}
    assert classes == {"temperature", "status"}
    temp = next(b for b in hot["breaches"] if b["class"] == "temperature")
    assert temp["value"] == 81.0 and "75" in temp["limit"]


def test_absence_is_never_fabricated(served: str) -> None:
    payload = _get(served).json()
    sequoia = next(d for d in payload["devices"] if d["device"] == "sequoia-gw")
    assert sequoia["stale"] is True
    assert "cpu_percent" not in sequoia["summary"] or sequoia["summary"]["cpu_percent"] is None
    vms = next(d for d in payload["devices"] if d["device"] == "core-vms-1")
    assert vms["summary"].get("temperature_c") is None  # no temp series seeded ⇒ absent


def test_device_mode_returns_series(served: str) -> None:
    payload = _get(served, "?device=chestnut-gw").json()
    assert payload["device"] == "chestnut-gw"
    metrics = {s["metric"] for s in payload["series"]}
    assert "health:cpu-temperature" in metrics
    temp_series = next(s for s in payload["series"] if s["metric"] == "health:cpu-temperature")
    assert any(b["avg"] == 35.0 for b in temp_series["buckets"] if b["n"])
    assert payload["readings"]


def test_unknown_device_is_honest(served: str) -> None:
    payload = _get(served, "?device=nope").json()
    assert payload["device"] == "nope"
    assert payload["readings"] == [] and payload["series"] == []
    assert "no health samples" in payload["note"]


def test_absent_store_names_the_cli_fix(storeless: str) -> None:
    payload = _get(storeless).json()
    assert payload["store"] == "absent"
    assert "obi metrics ingest" in payload["hint"]  # createBoard renders body.hint


def test_post_is_405_and_head_ok(served: str) -> None:
    response = httpx.post(f"{served}/api/health.json", verify=False, timeout=10.0)
    assert response.status_code == 405
    head = httpx.head(f"{served}/api/health.json", verify=False, timeout=10.0)
    assert head.status_code == 200
