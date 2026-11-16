"""Unit (feature 096): the `obi health status` CLI over a seeded metrics store.

Covers the fleet table (worst-first: stale devices float up, STALE marked), honest absence
(classes a device lacks render as em-dash, never zero), display scaling (deci-°C → °C,
mdBm → dBm, memory ratio), the per-device detail view, `--json` parity via the shared
serializer, and the store-absent short-circuit naming the collection commands.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from obi.core import metricstore
from obi.core.metricstore import StoredSample

runner = CliRunner(env={"COLUMNS": "200"})  # wide console: no cell wrap in assertions


def _write_config(tmp_path: Path, db: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[discovery]\n"
        'seeds  = ["172.25.110.1"]\n'
        'ranges = ["172.25.0.0/16"]\n'
        "\n[routeros]\n"
        'username = "u"\n'
        'password = "p"\n'
        "use_tls  = false\n"
        "\n[snmp]\n"
        'community = "public"\n'
        "\n[snmp.history]\n"
        f'db_path = "{db}"\n'
        "\n[audit]\n"
        f'path = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)
    return config


def _row(
    device: str, metric: str, value: int, *, age_s: float, ifindex: str = "", ifname=None
) -> StoredSample:
    ts_us = int((datetime.now(UTC).timestamp() - age_s) * 1_000_000)
    return StoredSample(
        ts_us=ts_us,
        ts_iso=datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address="172.18.81.1",
        profile="health",
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="gauge",
        value=value,
    )


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "metrics.db"
    conn = metricstore.connect(db)
    rows = [
        # chestnut-gw: fresh, full house
        _row("chestnut-gw", "health:cpu-temperature", 350, age_s=60, ifindex="17",
             ifname="cpu-temperature"),
        _row("chestnut-gw", "health:psu1-state", 0, age_s=60, ifindex="7401",
             ifname="psu1-state"),
        _row("chestnut-gw", "cpuLoad", 4, age_s=60),
        _row("chestnut-gw", "memUsedKb", 500_000, age_s=60),
        _row("chestnut-gw", "memTotalKb", 1_000_000, age_s=60),
        _row("chestnut-gw", "optics:rxPowerMdbm", -1907, age_s=60, ifindex="2",
             ifname="sfp-sfpplus1"),
        # manzanita-gw: last sample two hours sequoia → stale, floats to the top
        _row("manzanita-gw", "health:cpu-temperature", 490, age_s=7200, ifindex="17",
             ifname="cpu-temperature"),
    ]
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()
    return db


def test_store_absent_names_the_collection_commands(tmp_path: Path) -> None:
    config = _write_config(tmp_path, tmp_path / "missing.db")
    result = runner.invoke(app, ["health", "status", "--scope", str(config)])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())  # tolerate Rich's line wrapping
    assert "obi metrics ingest" in flat
    assert "obi health probe" in flat


def test_fleet_table_is_worst_first_with_stale_marker(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    config = _write_config(tmp_path, db)
    result = runner.invoke(app, ["health", "status", "--scope", str(config)])
    assert result.exit_code == 0
    assert result.output.index("manzanita-gw") < result.output.index("chestnut-gw")  # stale first
    assert "STALE" in result.output
    assert "35" in result.output  # 350 deci-°C → 35 °C
    assert "50%" in result.output  # memory ratio from used/total kB
    assert "ok" in result.output  # psu1-state 0 ⇒ states column ok


def test_absent_classes_render_as_dash_never_zero(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    config = _write_config(tmp_path, db)
    result = runner.invoke(app, ["health", "status", "--scope", str(config)])
    # manzanita-gw has only a temperature: its CPU/Mem/Rx/Disk cells are dashes
    biz_line = next(line for line in result.output.splitlines() if "manzanita-gw" in line)
    assert biz_line.count("—") >= 4


def test_json_uses_shared_serializer_and_scales_values(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    config = _write_config(tmp_path, db)
    result = runner.invoke(app, ["health", "status", "--scope", str(config), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    devices = {d["device"]: d for d in payload["devices"]}
    assert list(devices) == ["manzanita-gw", "chestnut-gw"]  # worst-first in the payload too
    assert devices["manzanita-gw"]["stale"] is True
    readings = {r["metric"]: r for r in devices["chestnut-gw"]["readings"]}
    assert readings["health:cpu-temperature"]["value"] == 35.0
    assert readings["health:cpu-temperature"]["unit"] == "°C"
    assert readings["optics:rxPowerMdbm"]["value"] == -1.907
    assert readings["optics:rxPowerMdbm"]["unit"] == "dBm"


def test_device_detail_lists_every_reading(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    config = _write_config(tmp_path, db)
    result = runner.invoke(
        app, ["health", "status", "--scope", str(config), "--device", "chestnut-gw"]
    )
    assert result.exit_code == 0
    assert "health:cpu-temperature" in result.output
    assert "sfp-sfpplus1" in result.output
    assert "manzanita-gw" not in result.output


def test_breaching_device_floats_first_and_is_annotated(tmp_path: Path) -> None:
    """US2: a device over threshold sorts above stale and healthy ones, its breaching
    sensor is named in the Breaches column, and --json carries the breach objects."""

    db = tmp_path / "metrics.db"
    conn = metricstore.connect(db)
    rows = [
        _row("cool-gw", "health:cpu-temperature", 400, age_s=60, ifindex="17",
             ifname="cpu-temperature"),
        _row("hot-gw", "health:board-temperature2", 810, age_s=60, ifindex="7102",
             ifname="board-temperature2"),
        _row("sequoia-gw", "health:cpu-temperature", 450, age_s=7200, ifindex="17",
             ifname="cpu-temperature"),
    ]
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()
    config = _write_config(tmp_path, db)

    result = runner.invoke(app, ["health", "status", "--scope", str(config)])
    out = result.output
    assert out.index("hot-gw") < out.index("sequoia-gw") < out.index("cool-gw")
    assert "board-temperature2" in out
    assert "breaching" in out  # footer counts

    js = runner.invoke(app, ["health", "status", "--scope", str(config), "--json"])
    payload = json.loads(js.output)
    hot = next(d for d in payload["devices"] if d["device"] == "hot-gw")
    assert hot["breaches"] and hot["breaches"][0]["class"] == "temperature"
    assert hot["breaches"][0]["value"] == 81.0
    cool = next(d for d in payload["devices"] if d["device"] == "cool-gw")
    assert cool["breaches"] == []
