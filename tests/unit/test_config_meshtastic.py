"""Unit (feature 111): parsing the ``[meshtastic]`` section — the mesh-locate opt-in.

Asserts the section is None when absent (the feature stays cleanly inactive), that an
EMPTY table is a valid all-defaults opt-in (a capability that can key shared LoRa
airtime is operator-enabled, but needs no tuning), the documented defaults, custom
values round-trip, and that malformed values fail at load — never at query time. Also
pins the rate-limiter state-path XDG helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import (
    ConfigError,
    default_mesh_state_path,
    default_playa_state_path,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_absent_meshtastic_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.meshtastic is None


def test_empty_section_is_the_all_defaults_opt_in(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.port is None
    # 121: 900 → 300. A 14-minute-old fix is confidently wrong for a moving tracker, and the
    # airtime budget to refresh it was going unused (~1 of 30 permitted sends per hour).
    assert cfg.meshtastic.fresh_within_s == 300
    assert cfg.meshtastic.request_timeout_s == 30.0
    assert cfg.meshtastic.snapshot_timeout_s == 10.0
    assert cfg.meshtastic.node_rate_limit_s == 60
    assert cfg.meshtastic.hourly_tx_cap == 30
    assert cfg.meshtastic.request_channel_index == 0
    assert cfg.meshtastic.excluded_ports == ()


def test_custom_values_round_trip(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[meshtastic]
port = "/dev/cu.usbmodem11301"
fresh_within_s = 300
request_timeout_s = 10.5
node_rate_limit_s = 120
hourly_tx_cap = 10
request_channel_index = 1
""",
        )
    )
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.port == "/dev/cu.usbmodem11301"
    assert cfg.meshtastic.fresh_within_s == 300
    assert cfg.meshtastic.request_timeout_s == 10.5
    assert cfg.meshtastic.node_rate_limit_s == 120
    assert cfg.meshtastic.hourly_tx_cap == 10
    assert cfg.meshtastic.request_channel_index == 1


def test_positions_db_path_defaults_to_the_mesh_state_dir(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.positions_db_path.name == "positions.db"
    assert cfg.meshtastic.positions_db_path.parent.name == "meshtastic"


def test_positions_db_path_round_trips(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, f'[meshtastic]\npositions_db_path = "{tmp_path}/pos.db"\n')
    )
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.positions_db_path == tmp_path / "pos.db"


def test_positions_db_path_rejects_blank(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="positions_db_path"):
        load_config(_write(tmp_path, '[meshtastic]\npositions_db_path = ""\n'))


def test_rejects_out_of_range_channel_index(tmp_path: Path) -> None:
    # A wrong slot would key the wrong channel's airtime — fail at load, never at send.
    with pytest.raises(ConfigError, match="request_channel_index"):
        load_config(_write(tmp_path, "[meshtastic]\nrequest_channel_index = 8\n"))
    with pytest.raises(ConfigError, match="request_channel_index"):
        load_config(_write(tmp_path, "[meshtastic]\nrequest_channel_index = -1\n"))
    with pytest.raises(ConfigError, match="request_channel_index"):
        load_config(_write(tmp_path, '[meshtastic]\nrequest_channel_index = "one"\n'))


def test_rejects_wrong_type(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="fresh_within_s"):
        load_config(_write(tmp_path, '[meshtastic]\nfresh_within_s = "soon"\n'))


def test_rejects_negative_values(tmp_path: Path) -> None:
    # A negative limit would disable the airtime guard silently — fail at load instead.
    with pytest.raises(ConfigError, match="hourly_tx_cap"):
        load_config(_write(tmp_path, "[meshtastic]\nhourly_tx_cap = -1\n"))
    with pytest.raises(ConfigError, match="node_rate_limit_s"):
        load_config(_write(tmp_path, "[meshtastic]\nnode_rate_limit_s = -5\n"))
    with pytest.raises(ConfigError, match="request_timeout_s"):
        load_config(_write(tmp_path, "[meshtastic]\nrequest_timeout_s = 0\n"))


def test_rejects_blank_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="port"):
        load_config(_write(tmp_path, '[meshtastic]\nport = ""\n'))


def test_aliases_parse_and_default_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.aliases == {}
    cfg = load_config(
        _write(
            tmp_path,
            '[meshtastic.aliases]\n"art car" = "!91378e3a"\ntooma = "!91378e3a"\n',
        )
    )
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.aliases == {"art car": "!91378e3a", "tooma": "!91378e3a"}


def test_aliases_reject_blank_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="aliases"):
        load_config(_write(tmp_path, '[meshtastic.aliases]\n"art car" = ""\n'))
    with pytest.raises(ConfigError, match="aliases"):
        load_config(_write(tmp_path, '[meshtastic]\naliases = ["art car"]\n'))


def test_display_names_parse(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[meshtastic.names]\n"!91378e3a" = "Tooma"\n'))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.display_names == {"!91378e3a": "Tooma"}
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.display_names == {}


def test_display_names_reject_blank(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="names"):
        load_config(_write(tmp_path, '[meshtastic.names]\n"!91378e3a" = ""\n'))


def test_report_only_parses_and_defaults_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.report_only == []
    cfg = load_config(_write(tmp_path, '[meshtastic]\nreport_only = ["!91378e3a"]\n'))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.report_only == ["!91378e3a"]


def test_report_only_rejects_malformed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="report_only"):
        load_config(_write(tmp_path, '[meshtastic]\nreport_only = "!91378e3a"\n'))
    with pytest.raises(ConfigError, match="report_only"):
        load_config(_write(tmp_path, '[meshtastic]\nreport_only = [""]\n'))


def test_state_path_honors_xdg_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_mesh_state_path() == tmp_path / "state" / "obi" / "meshtastic"


# --- 121: the snapshot deadline and the derived radio-station port exclusion ---------------


def test_snapshot_timeout_is_configurable(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\nsnapshot_timeout_s = 4.5\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.snapshot_timeout_s == 4.5


@pytest.mark.parametrize("value", ["0", "-1", '"soon"', "true"])
def test_snapshot_timeout_must_be_a_positive_number(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="snapshot_timeout_s"):
        load_config(_write(tmp_path, f"[meshtastic]\nsnapshot_timeout_s = {value}\n"))


def test_excluded_ports_cannot_be_set_from_config(tmp_path: Path) -> None:
    """It is DERIVED, never operator-typed.

    An operator-writable list could be emptied — and empty here means the mesh driver may
    open the transmitter. A stray TOML key must be inert, not authoritative.
    """

    cfg = load_config(
        _write(tmp_path, '[meshtastic]\nexcluded_ports = ["/dev/cu.anything"]\n')
    )
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.excluded_ports == ()


def test_radio_section_populates_the_port_exclusion(tmp_path: Path) -> None:
    """The cross-section safety rule: [radio] present ⇒ its PTT device is off-limits."""

    cfg = load_config(
        _write(
            tmp_path,
            "[meshtastic]\n\n[radio]\nftdi_serial = 'FBC3793547DF'\n",
        )
    )
    assert cfg.meshtastic is not None
    assert "/dev/cu.usbserial-FBC3793547DF" in cfg.meshtastic.excluded_ports


def test_radio_serial_port_override_is_excluded_too(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[meshtastic]\n\n[radio]\nftdi_serial = 'FBC3793547DF'\n"
            "serial_port = '/dev/cu.custom-ptt'\n",
        )
    )
    assert cfg.meshtastic is not None
    assert "/dev/cu.custom-ptt" in cfg.meshtastic.excluded_ports


def test_no_radio_section_means_no_exclusion(tmp_path: Path) -> None:
    """FR-006: a machine with no radio station behaves exactly as before."""

    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.excluded_ports == ()


# --- feature 123: daemon settings --------------------------------------------------------


def test_daemon_settings_default(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.daemon_socket.name == "daemon.sock"
    assert cfg.meshtastic.daemon_sweep_s == 60.0
    assert cfg.meshtastic.standby_lease_s == 1800


def test_daemon_settings_round_trip(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[meshtastic]\ndaemon_socket = '/tmp/meshd-test.sock'\n"
            "daemon_sweep_s = 30\nstandby_lease_s = 600\n",
        )
    )
    assert cfg.meshtastic is not None
    assert str(cfg.meshtastic.daemon_socket) == "/tmp/meshd-test.sock"
    assert cfg.meshtastic.daemon_sweep_s == 30.0
    assert cfg.meshtastic.standby_lease_s == 600


def test_daemon_socket_rejects_blank(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="daemon_socket"):
        load_config(_write(tmp_path, "[meshtastic]\ndaemon_socket = ' '\n"))


def test_daemon_numbers_reject_nonpositive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="daemon_sweep_s"):
        load_config(_write(tmp_path, "[meshtastic]\ndaemon_sweep_s = 0\n"))
    with pytest.raises(ConfigError, match="standby_lease_s"):
        load_config(_write(tmp_path, "[meshtastic]\nstandby_lease_s = -5\n"))


# --- feature 125: the automatic freshness interval ---------------------------------------


def test_auto_request_defaults_on_at_five_minutes(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.auto_request_s == 300


def test_auto_request_zero_is_the_off_switch(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\nauto_request_s = 0\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.auto_request_s == 0


def test_auto_request_round_trips_and_rejects_negative(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[meshtastic]\nauto_request_s = 600\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.auto_request_s == 600
    with pytest.raises(ConfigError, match="auto_request_s"):
        load_config(_write(tmp_path, "[meshtastic]\nauto_request_s = -1\n"))


def test_art_defaults(tmp_path: Path) -> None:
    # 126/127: art landmarks default to the SHARED playa data dir (one art.json for
    # landmarks AND playa_locate) + a 400 ft "near" radius; the file's ABSENCE (the
    # default state) simply means answers without references.
    cfg = load_config(_write(tmp_path, "[meshtastic]\n"))
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.art_path == default_playa_state_path() / "art.json"
    assert cfg.meshtastic.art_near_ft == 400


def test_art_settings_round_trip(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[meshtastic]\nart_path = "~/art/2026.json"\nart_near_ft = 250\n',
        )
    )
    assert cfg.meshtastic is not None
    assert cfg.meshtastic.art_path == Path("~/art/2026.json").expanduser()
    assert cfg.meshtastic.art_near_ft == 250


def test_art_path_rejects_blank(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="art_path"):
        load_config(_write(tmp_path, '[meshtastic]\nart_path = "  "\n'))


def test_art_near_ft_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="art_near_ft"):
        load_config(_write(tmp_path, "[meshtastic]\nart_near_ft = 0\n"))
