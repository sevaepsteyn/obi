"""Integration (feature 111): `obi mesh locate` drives the full engine offline.

The CLI builds the real MeshLocator; only the ACCESS seam is swapped — since 123 the
default is the daemon client (mesh_access), so these tests patch that factory to hand
back a MeshDriver over a fake connection factory and the 2026-08-28 fixtures. The test
still exercises CLI → locator → driver → rendering without ever touching a serial port
(the daemon's own RPC path has its own suites).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core import mesh as mesh_mod
from obi.core.config import MeshtasticSettings
from obi.core.drivers.meshtastic import MeshDriver

runner = CliRunner()

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "meshtastic"


class _Connection:
    def __init__(self) -> None:
        raw = json.loads((_FIXTURES / "nodes.json").read_text())
        self.my_node_id = str(raw["myNodeId"])
        self.nodes: dict[str, dict[str, Any]] = dict(raw["nodes"])
        reply = json.loads((_FIXTURES / "position_response.json").read_text())
        self.reply = {k: v for k, v in reply.items() if not k.startswith("_")}
        self.requests: list[str] = []

    def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]:
        return self.my_node_id, self.nodes

    def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
        self.requests.append(node_id)
        return self.reply

    def close(self) -> None:
        pass


def _config(tmp_path: Path, *, mesh_section: str = "[meshtastic]\n") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n{mesh_section}')
    path.chmod(0o600)
    return path


def test_locate_answers_through_the_full_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _Connection()

    def fake_driver(settings: MeshtasticSettings) -> MeshDriver:
        return MeshDriver(
            settings,
            connection_factory=lambda port, timeout: connection,
            find_ports=lambda: ["/dev/cu.usbmodem11301"],
        )

    monkeypatch.setattr(mesh_mod, "mesh_access", fake_driver)
    result = runner.invoke(app, ["mesh", "locate", "tooma", "--config", str(_config(tmp_path))])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    # 125: the lookup is purely passive — TUMA is positionless in the fixture, so the
    # answer is honest absence, and no request went out (the daemon owns freshness).
    assert "Tooma 9831 (TUMA) has no known position" in flat
    assert "no fresher position has been heard" in flat
    assert connection.requests == []


def test_unconfigured_exits_two_with_guidance(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["mesh", "locate", "tooma", "--config", str(_config(tmp_path, mesh_section=""))]
    )
    assert result.exit_code == 2
    assert "meshtastic" in result.output.lower()


# --- 117: the last-known fallback through the full stack ---------------------------------


def _ghost_section(tmp_path: Path) -> str:
    return (
        "[meshtastic]\n"
        'report_only = ["!feedf00d"]\n'
        f'positions_db_path = "{tmp_path}/positions.db"\n'
        "[meshtastic.aliases]\n"
        'ghost = "!feedf00d"\n'
        "[meshtastic.names]\n"
        '"!feedf00d" = "Ghost"\n'
    )


def _seed_ghost(tmp_path: Path) -> None:
    from obi.core import meshstore

    conn = meshstore.connect(tmp_path / "positions.db")
    meshstore.record_fix(
        conn,
        meshstore.RecordedFix(
            node_id="!feedf00d",
            latitude=40.7797341,
            longitude=-114.8227661,
            altitude_m=1191,
            fix_time_s=1,  # ancient — ages as days, wording not pinned here
            observed_at_s=2,
            hops_away=0,
            snr_db=11.75,
            address="6:25 & Esplanade",
        ),
    )
    conn.close()


def test_locate_with_history_prints_last_known_when_node_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ghost(tmp_path)
    connection = _Connection()

    def fake_driver(settings: MeshtasticSettings) -> MeshDriver:
        return MeshDriver(
            settings,
            connection_factory=lambda port, timeout: connection,
            find_ports=lambda: ["/dev/cu.usbmodem11301"],
        )

    monkeypatch.setattr(mesh_mod, "mesh_access", fake_driver)
    result = runner.invoke(
        app,
        [
            "mesh",
            "locate",
            "ghost",
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    flat = " ".join(result.output.split())  # Rich wraps at terminal width
    assert "Ghost was last known at 6:25 & Esplanade" in flat
    assert "isn't in the radio's node table" in flat
    assert connection.requests == []  # the fallback never transmits


def test_locate_without_history_keeps_todays_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _Connection()

    def fake_driver(settings: MeshtasticSettings) -> MeshDriver:
        return MeshDriver(
            settings,
            connection_factory=lambda port, timeout: connection,
            find_ports=lambda: ["/dev/cu.usbmodem11301"],
        )

    monkeypatch.setattr(mesh_mod, "mesh_access", fake_driver)
    result = runner.invoke(
        app,
        [
            "mesh",
            "locate",
            "ghost",
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    assert "No mesh node matches" in result.output


# --- 117: `obi mesh record` — the sweep through the full stack ---------------------------


def _tracker_section(tmp_path: Path) -> str:
    # "!38568530" (AshH) is the fixture node WITH a stored position.
    return (
        "[meshtastic]\n"
        'report_only = ["!38568530"]\n'
        f'positions_db_path = "{tmp_path}/positions.db"\n'
    )


def test_record_sweeps_once_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _Connection()

    def fake_driver(settings: MeshtasticSettings) -> MeshDriver:
        return MeshDriver(
            settings,
            connection_factory=lambda port, timeout: connection,
            find_ports=lambda: ["/dev/cu.usbmodem11301"],
        )

    monkeypatch.setattr(mesh_mod, "mesh_access", fake_driver)
    config = str(_config(tmp_path, mesh_section=_tracker_section(tmp_path)))
    first = runner.invoke(app, ["mesh", "record", "--config", config])
    assert first.exit_code == 0
    assert "recorded 1 new fix(es)" in first.output
    assert connection.requests == []  # the sweep never transmits
    second = runner.invoke(app, ["mesh", "record", "--config", config])
    assert second.exit_code == 0
    assert "no new fixes" in second.output


def test_record_skips_quietly_without_a_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_driver(settings: MeshtasticSettings) -> MeshDriver:
        return MeshDriver(settings, find_ports=lambda: [])

    monkeypatch.setattr(mesh_mod, "mesh_access", fake_driver)
    config = str(_config(tmp_path, mesh_section=_tracker_section(tmp_path)))
    result = runner.invoke(app, ["mesh", "record", "--config", config])
    assert result.exit_code == 0
    assert "skipped:" in result.output


def test_record_unconfigured_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["mesh", "record", "--config", str(_config(tmp_path, mesh_section=""))]
    )
    assert result.exit_code == 2


# --- 121: the radio station's device is refused end to end -------------------------------


def _station_driver(
    monkeypatch: pytest.MonkeyPatch, station_port: str = "/dev/cu.usbserial-FBC3793547DF"
) -> None:
    """Discovery offers ONLY the walkie-talkie — the 2026-08-31 shape, RAK unplugged.

    The connection factory raises if it is ever called: opening the device is itself the
    harm (it asserts DTR+RTS and keys the transmitter), so the CLI must refuse before it.
    """

    def exploding_factory(port: str, timeout: float) -> object:
        raise AssertionError(f"the CLI opened the radio station's device: {port}")

    def fake_driver(settings: MeshtasticSettings) -> MeshDriver:
        return MeshDriver(
            settings,
            connection_factory=exploding_factory,
            find_ports=lambda: [station_port],
        )

    monkeypatch.setattr(mesh_mod, "mesh_access", fake_driver)


def _unwrapped(output: str) -> str:
    """CLI output with wrapping collapsed — Rich folds at 80 cols and splits phrases."""

    return " ".join(output.split())


def test_locate_refuses_the_radio_stations_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _station_driver(monkeypatch)
    config = str(
        _config(
            tmp_path,
            mesh_section=_tracker_section(tmp_path)
            + "\n[radio]\nftdi_serial = 'FBC3793547DF'\n",
        )
    )
    result = runner.invoke(app, ["mesh", "locate", "tooma", "--config", config])
    assert "radio station" in _unwrapped(result.output)
    assert "key the transmitter" in _unwrapped(result.output)


def test_record_refuses_the_radio_stations_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs every 10 minutes from cron — it is the likeliest caller to hit this."""

    _station_driver(monkeypatch)
    config = str(
        _config(
            tmp_path,
            mesh_section=_tracker_section(tmp_path)
            + "\n[radio]\nftdi_serial = 'FBC3793547DF'\n",
        )
    )
    result = runner.invoke(app, ["mesh", "record", "--config", config])
    assert result.exit_code == 0  # a quiet skip, as for any absent radio
    assert "skipped:" in _unwrapped(result.output)
    assert "radio station" in _unwrapped(result.output)


# --- 117: `obi mesh track` — the recorded track ------------------------------------------


def _seed_track(tmp_path: Path, base_s: int) -> None:
    from obi.core import meshstore

    conn = meshstore.connect(tmp_path / "positions.db")
    for offset, lat, address in (
        (0, 40.7790000, "6:00 & Esplanade"),
        (600, 40.7797341, "6:25 & Esplanade"),
        (1200, 40.7810000, "6:30 and 2,100 feet from the Man"),
    ):
        meshstore.record_fix(
            conn,
            meshstore.RecordedFix(
                node_id="!feedf00d",
                latitude=lat,
                longitude=-119.2150581,
                altitude_m=1191,
                fix_time_s=base_s + offset,
                observed_at_s=base_s + offset + 5,
                hops_away=0,
                snr_db=11.75,
                address=address,
            ),
        )
    # One clock-less fix (fix_time 0): rendered from its observation time, flagged.
    meshstore.record_fix(
        conn,
        meshstore.RecordedFix(
            node_id="!feedf00d",
            latitude=40.7820000,
            longitude=-114.8227661,
            altitude_m=None,
            fix_time_s=0,
            observed_at_s=base_s + 1800,
            hops_away=2,
            snr_db=None,
            address="6:35 and 1,900 feet from the Man",
        ),
    )
    conn.close()


def test_track_prints_ascending_rows(tmp_path: Path) -> None:
    import time as _t

    base = int(_t.time()) - 3600
    _seed_track(tmp_path, base)
    result = runner.invoke(
        app,
        [
            "mesh",
            "track",
            "ghost",
            "--hours",
            "2",
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    first = flat.index("6:00 & Esplanade")
    second = flat.index("6:25 & Esplanade")
    third = flat.index("2,100 feet")
    assert first < second < third  # ascending by effective instant
    assert "~" in result.output  # the clock-less fix is flagged as observed-time


def test_track_respects_since_until(tmp_path: Path) -> None:
    import time as _t
    from datetime import datetime

    base = int(_t.time()) - 3600
    _seed_track(tmp_path, base)
    since = datetime.fromtimestamp(base + 300).isoformat()
    until = datetime.fromtimestamp(base + 900).isoformat()
    result = runner.invoke(
        app,
        [
            "mesh",
            "track",
            "ghost",
            "--since",
            since,
            "--until",
            until,
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "6:25 & Esplanade" in flat
    assert "6:00 & Esplanade" not in flat
    assert "2,100 feet" not in flat


def test_track_csv_exports_raw_rows(tmp_path: Path) -> None:
    import time as _t

    base = int(_t.time()) - 3600
    _seed_track(tmp_path, base)
    result = runner.invoke(
        app,
        [
            "mesh",
            "track",
            "ghost",
            "--hours",
            "2",
            "--csv",
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines[0] == (
        "node_id,fix_time_s,observed_at_s,latitude,longitude,altitude_m,"
        "hops_away,snr_db,address"
    )
    assert len(lines) == 5  # header + 4 fixes
    assert "40.7797341" in result.output
    assert "-119.2150581" in result.output


def test_track_empty_range_is_honest(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mesh",
            "track",
            "ghost",
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    assert "no recorded fixes" in result.output.lower()


def test_track_refuses_a_non_allowlisted_node(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mesh",
            "track",
            "stranger",
            "--config",
            str(_config(tmp_path, mesh_section=_ghost_section(tmp_path))),
        ],
    )
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "position history is enabled only for" in flat
