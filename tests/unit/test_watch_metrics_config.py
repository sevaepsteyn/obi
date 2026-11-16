"""Unit (feature 027): parsing the opt-in ``[watch.metrics]`` subsection.

Asserts the pass is inactive without the subsection, the defaults, the window duration parsing, and
that an unknown counter or a negative threshold fails fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, WatchMetricsSettings, load_config


def _write(tmp_path: Path, watch_block: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(f"{logs}[watch]\nenabled = true\n{watch_block}")
    path.chmod(0o600)
    return path


def _metrics(tmp_path: Path, block: str) -> WatchMetricsSettings:
    cfg = load_config(_write(tmp_path, block))
    assert cfg.watch.metrics is not None
    return cfg.watch.metrics


def test_absent_metrics_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.watch.metrics is None  # log-watching unchanged


def test_defaults(tmp_path: Path) -> None:
    m = _metrics(tmp_path, "[watch.metrics]\n")
    assert m.enabled is False  # presence alone does not activate; enabled must be set
    assert m.threshold == 0
    assert m.window_seconds == 3600
    assert m.counters == ["ifInErrors", "ifOutErrors"]
    assert m.db_path.name == "metrics.db"


def test_enabled_and_window_duration(tmp_path: Path) -> None:
    m = _metrics(tmp_path, '[watch.metrics]\nenabled = true\nwindow = "30m"\n')
    assert m.enabled is True
    assert m.window_seconds == 1800


def test_custom_counters(tmp_path: Path) -> None:
    m = _metrics(tmp_path, '[watch.metrics]\ncounters = ["ifInErrors", "ifInDiscards"]\n')
    assert m.counters == ["ifInErrors", "ifInDiscards"]


def test_ignore_interfaces_default_and_custom(tmp_path: Path) -> None:
    assert _metrics(tmp_path, "[watch.metrics]\n").ignore_interfaces == ["wireguard"]
    m = _metrics(tmp_path, '[watch.metrics]\nignore_interfaces = ["wireguard", "gre"]\n')
    assert m.ignore_interfaces == ["wireguard", "gre"]
    assert _metrics(tmp_path, "[watch.metrics]\nignore_interfaces = []\n").ignore_interfaces == []


def test_unknown_counter_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _metrics(tmp_path, '[watch.metrics]\ncounters = ["bogus"]\n')


def test_negative_threshold_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _metrics(tmp_path, "[watch.metrics]\nthreshold = -1\n")


def test_bad_window_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _metrics(tmp_path, '[watch.metrics]\nwindow = "soon"\n')
