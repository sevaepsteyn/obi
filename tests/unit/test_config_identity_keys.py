"""Moved identity values are config, never built-in defaults (feature 137, T027).

Every row of specs/137-identity-externalize/contracts/config-keys.md: the key parses; a missing
REQUIRED key in a present section is a ConfigError naming the key; an absent event section is
``None`` (the feature is off); optional keys get neutral defaults. All values are fictional.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config


def _cfg(tmp_path: Path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n' + body)
    path.chmod(0o600)
    return load_config(path)


def test_absent_event_sections_are_none(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "")
    assert cfg.weather is None and cfg.traffic is None
    assert cfg.brc is None and cfg.playa is None
    assert cfg.site.timezone == "UTC"
    assert cfg.web.brc is False  # the city grid is on only when [brc] exists
    assert cfg.web.default_ground_elevation == 0.0
    assert cfg.web.ospf_sources == []


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("[weather]\nlongitude = -100.0\n", "latitude"),
        ("[weather]\nlatitude = 45.0\n", "longitude"),
        ("[traffic]\n", "base_url"),
        ('[paging]\nusername = "u"\npassword = "p"\n', "url"),
        ("[brc]\norigin_lat = 45.0\nyear = 2030\n", "origin_lon"),
        ("[brc]\norigin_lat = 45.0\norigin_lon = -100.0\n", "year"),
        ("[playa]\n", "base_url"),
        ('[health.linux]\nhosts = ["collector-1"]\n', "username"),
    ],
)
def test_missing_required_key_names_it(tmp_path: Path, body: str, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        _cfg(tmp_path, body)


def test_weather_has_no_event_defaults(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "[weather]\nlatitude = 45.0\nlongitude = -100.0\n")
    assert cfg.weather is not None
    assert cfg.weather.nws_zone is None and cfg.weather.place_name is None


def test_new_sections_parse(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        "[brc]\norigin_lat = 45.0\norigin_lon = -100.0\nyear = 2030\n"
        'source_url = "https://gis.example.net/{year}"\naz12_deg = 30.0\n\n'
        '[playa]\nbase_url = "https://api.example.net"\n\n'
        '[site]\ntimezone = "America/Denver"\n',
    )
    assert cfg.brc is not None and cfg.brc.year == 2030 and cfg.brc.az12_deg == 30.0
    assert cfg.brc.cache_name == "brc-2030"
    assert cfg.playa is not None and cfg.playa.base_url == "https://api.example.net"
    assert cfg.site.timezone == "America/Denver"
    assert cfg.web.brc is True


def test_neutral_defaults(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        '[snmp]\ncommunity = "x"\n[snmp.history]\n\n[dash]\n\n[radio]\nserial_port = "/dev/null"\n',
    )
    assert cfg.snmp is not None and cfg.snmp.history is not None
    assert cfg.snmp.history.targets == ""
    assert cfg.dash is not None and cfg.dash.ping_router == ""
    assert cfg.radio is not None
    assert cfg.radio.ftdi_serial is None
    assert cfg.radio.pronunciations == {} and cfg.radio.speech_aliases == {}
    assert cfg.radio.channel_name == "the operations radio channel"


def test_radio_needs_a_cable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ftdi_serial"):
        _cfg(tmp_path, "[radio]\n")


def test_optional_display_keys(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        '[victoria]\nbase_url = "https://metrics.example.net"\ndisplay_name = "the ops store"\n'
        "rf_matcher = '{role=\"ptp-rf\"}'\n\n"
        '[wled.devices]\ngate = { url = "http://192.0.2.5", description = "the gate arch" }\n'
        'lobby = "http://192.0.2.6"\n',
    )
    assert cfg.victoria is not None and cfg.victoria.display_name == "the ops store"
    assert cfg.victoria.rf_matcher == '{role="ptp-rf"}'
    assert cfg.wled is not None
    assert cfg.wled.devices == {"gate": "http://192.0.2.5", "lobby": "http://192.0.2.6"}
    assert cfg.wled.descriptions == {"gate": "the gate arch"}
