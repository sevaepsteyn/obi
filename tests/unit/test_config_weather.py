"""Unit (feature 110): parsing the optional ``[weather]`` section.

Asserts the section is ``None`` when absent (weather is OFF — feature 137: no built-in location),
that a present section needs latitude/longitude, the neutral defaults (no NWS zone), custom
values with ``~`` expansion, and that out-of-range bounds fail fast at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import (
    ConfigError,
    WeatherSettings,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_absent_weather_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.weather is None  # obi weather run still works with an all-default WeatherSettings


LOC = "latitude = 45.0\nlongitude = -100.0\n"


def test_weather_defaults_are_neutral(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[weather]\n" + LOC))
    weather = cfg.weather
    assert weather is not None
    assert weather.latitude == pytest.approx(45.0)
    assert weather.longitude == pytest.approx(-100.0)
    assert weather.nws_zone is None  # 137: no built-in alert zone
    assert weather.timeout_s == 20.0
    assert weather.retention_days == 120
    assert weather.db_path.name == "weather.db"


def test_weather_requires_a_location(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="latitude"):
        load_config(_write(tmp_path, "[weather]\n"))


def test_weather_custom_values(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[weather]\nlatitude = 44.5\nlongitude = -101.2\nnws_zone = "ABZ001"\n'
            f'timeout_s = 5.0\nretention_days = 30\ndb_path = "{tmp_path}/wx.db"\n',
        )
    )
    weather = cfg.weather
    assert weather is not None
    assert weather.latitude == pytest.approx(44.5)
    assert weather.longitude == pytest.approx(-101.2)
    assert weather.nws_zone == "ABZ001"
    assert weather.timeout_s == 5.0
    assert weather.retention_days == 30
    assert weather.db_path == tmp_path / "wx.db"


def test_weather_db_path_expands_home(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[weather]\n" + LOC + 'db_path = "~/wx/weather.db"\n'))
    assert cfg.weather is not None
    assert "~" not in str(cfg.weather.db_path)


def test_weather_bad_timeout_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[weather]\n" + LOC + "timeout_s = 0\n"))


def test_weather_bad_retention_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[weather]\n" + LOC + "retention_days = 0\n"))


def test_weather_bad_latitude_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[weather]\nlatitude = 91.0\nlongitude = 0.0\n"))


def test_weather_bad_longitude_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[weather]\nlatitude = 0.0\nlongitude = -181.0\n"))


def test_weather_empty_zone_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[weather]\n" + LOC + 'nws_zone = ""\n'))


def test_defaults_constructible_without_toml() -> None:
    # The dataclass stands alone given a location (137: there is no all-default form).
    settings = WeatherSettings(latitude=45.0, longitude=-100.0)
    assert settings.nws_zone is None
    assert settings.retention_days == 120
