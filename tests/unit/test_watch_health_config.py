"""Unit (feature 096): parsing the opt-in ``[watch.health]`` subsection."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config

_BASE = """
[logs]
mirror_root = "{root}/mirror"
db_path = "{root}/logs.db"

[watch]
enabled = true
watch_dir = "{root}/watch"
"""


def _load(tmp_path: Path, health_block: str):
    path = tmp_path / "config.toml"
    path.write_text(_BASE.format(root=tmp_path) + health_block)
    path.chmod(0o600)
    return load_config(path)


def test_absent_section_leaves_pass_inactive(tmp_path: Path) -> None:
    assert _load(tmp_path, "").watch.health is None


def test_presence_without_enabled_is_inactive(tmp_path: Path) -> None:
    settings = _load(tmp_path, "[watch.health]\n").watch.health
    assert settings is not None
    assert settings.enabled is False


def test_defaults(tmp_path: Path) -> None:
    s = _load(tmp_path, "[watch.health]\nenabled = true\n").watch.health
    assert s is not None and s.enabled is True
    assert s.temperature_max_c == 75.0
    assert s.sfp_temperature_max_c == 70.0
    assert s.cpu_max_percent == 90.0
    assert s.memory_max_percent == 90.0
    assert s.sustained_samples == 3
    assert s.disk_max_percent == 85.0
    assert s.rx_power_min_dbm == -24.0
    assert s.status_healthy_value == 0
    assert s.severity == "warning"
    assert s.mass_outage_ratio == 0.80
    assert s.ignore_devices == [] and s.ignore_sensors == []


def test_custom_values(tmp_path: Path) -> None:
    block = (
        "[watch.health]\nenabled = true\ntemperature_max_c = 80\nsustained_samples = 5\n"
        'rx_power_min_dbm = -20.5\nseverity = "critical"\nignore_devices = ["yellowwood"]\n'
        'ignore_sensors = ["psu2-state"]\n'
    )
    s = _load(tmp_path, block).watch.health
    assert s is not None
    assert s.temperature_max_c == 80.0
    assert s.sustained_samples == 5
    assert s.rx_power_min_dbm == -20.5
    assert s.severity == "critical"
    assert s.ignore_devices == ["yellowwood"] and s.ignore_sensors == ["psu2-state"]


@pytest.mark.parametrize(
    "bad",
    [
        'severity = "loud"',
        "mass_outage_ratio = 0.0",
        "mass_outage_ratio = 1.5",
        "sustained_samples = 0",
        'temperature_max_c = "hot"',
        'ignore_devices = "yellowwood"',
    ],
)
def test_bad_values_raise_config_error_naming_the_section(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match=r"\[watch\.health\]"):
        _load(tmp_path, f"[watch.health]\nenabled = true\n{bad}\n")
