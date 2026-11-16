"""T002: [victoria] config parsing (feature 108) — the ADMIN metrics store endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    # [logs] satisfies the ">=1 source" gate; [victoria] is a capability, not a source.
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    config.write_text(logs + body)
    config.chmod(0o600)
    return config


def test_absent_victoria_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.victoria is None


def test_victoria_parses_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[victoria]\nbase_url = "http://core-vms-1.example.org:8428/"\n')
    )
    assert cfg.victoria is not None
    assert cfg.victoria.base_url == "http://core-vms-1.example.org:8428"  # slash stripped
    assert cfg.victoria.timeout == 10.0
    assert cfg.victoria.rf_window == "24h"
    assert cfg.victoria.weather_stale_s == 900


def test_victoria_custom_values(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[victoria]
base_url = "https://vm.example.org:8428"
timeout = 4.5
rf_window = "6h"
weather_stale_s = 300
""",
        )
    )
    assert cfg.victoria is not None
    assert cfg.victoria.base_url == "https://vm.example.org:8428"
    assert cfg.victoria.timeout == 4.5
    assert cfg.victoria.rf_window == "6h"
    assert cfg.victoria.weather_stale_s == 300


def test_victoria_requires_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[victoria\].*base_url"):
        load_config(_write(tmp_path, "[victoria]\ntimeout = 5.0\n"))


def test_victoria_rejects_non_http_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="http"):
        load_config(_write(tmp_path, '[victoria]\nbase_url = "core-vms-1:8428"\n'))


def test_victoria_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="timeout"):
        load_config(
            _write(tmp_path, '[victoria]\nbase_url = "http://vm:8428"\ntimeout = 0\n')
        )


def test_victoria_rejects_non_positive_stale(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="weather_stale_s"):
        load_config(
            _write(
                tmp_path,
                '[victoria]\nbase_url = "http://vm:8428"\nweather_stale_s = -1\n',
            )
        )


def test_victoria_alone_does_not_satisfy_source_gate(tmp_path: Path) -> None:
    """[victoria] is a capability section — a victoria-only config still fails the gate."""

    config = tmp_path / "config.toml"
    config.write_text('[victoria]\nbase_url = "http://vm:8428"\n')
    config.chmod(0o600)
    with pytest.raises(ConfigError, match="no source configured"):
        load_config(config)
