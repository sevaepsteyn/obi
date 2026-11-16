"""Unit (feature 030): parsing the opt-in ``[watch.reachability]`` subsection.

Asserts the pass is inactive without the subsection, the defaults, custom values, and that an
out-of-range ``stale_after_cycles`` or ``mass_outage_ratio`` fails fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, WatchReachabilitySettings, load_config


def _write(tmp_path: Path, watch_block: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(f"{logs}[watch]\nenabled = true\n{watch_block}")
    path.chmod(0o600)
    return path


def _reach(tmp_path: Path, block: str) -> WatchReachabilitySettings:
    cfg = load_config(_write(tmp_path, block))
    assert cfg.watch.reachability is not None
    return cfg.watch.reachability


def test_absent_reachability_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.watch.reachability is None  # other passes unchanged


def test_defaults(tmp_path: Path) -> None:
    r = _reach(tmp_path, "[watch.reachability]\n")
    assert r.enabled is False  # presence alone does not activate; enabled must be set
    assert r.stale_after_cycles == 2
    assert r.mass_outage_ratio == 0.80
    assert r.ignore_devices == []
    assert r.db_path.name == "metrics.db"


def test_enabled_and_custom_values(tmp_path: Path) -> None:
    r = _reach(
        tmp_path,
        "[watch.reachability]\nenabled = true\nstale_after_cycles = 3\n"
        'mass_outage_ratio = 0.5\nignore_devices = ["brc-gw"]\n',
    )
    assert r.enabled is True
    assert r.stale_after_cycles == 3
    assert r.mass_outage_ratio == 0.5
    assert r.ignore_devices == ["brc-gw"]


def test_stale_after_cycles_below_one_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _reach(tmp_path, "[watch.reachability]\nstale_after_cycles = 0\n")


def test_mass_outage_ratio_zero_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _reach(tmp_path, "[watch.reachability]\nmass_outage_ratio = 0\n")


def test_mass_outage_ratio_above_one_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _reach(tmp_path, "[watch.reachability]\nmass_outage_ratio = 1.5\n")
