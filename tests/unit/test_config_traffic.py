"""Unit (feature 112): parsing the optional ``[traffic]`` section.

Asserts the section is ``None`` when absent (traffic is OFF — feature 137: no built-in
dashboard), that a present section needs base_url, the defaults (timeout, season retention),
custom values with ``~`` expansion, and that bad values fail fast at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import (
    ConfigError,
    TrafficSettings,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_absent_traffic_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.traffic is None  # obi traffic run still works with an all-default TrafficSettings


URL = 'base_url = "https://traffic.example.net"\n'


def test_traffic_requires_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        load_config(_write(tmp_path, "[traffic]\n"))


def test_traffic_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[traffic]\n" + URL))
    traffic = cfg.traffic
    assert traffic is not None
    assert traffic.base_url == "https://traffic.example.net"
    assert traffic.timeout_s == 20.0
    assert traffic.retention_days == 120
    assert traffic.db_path.name == "traffic.db"


def test_traffic_custom_values(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[traffic]\nbase_url = "http://localhost:8080/"\n'
            f'timeout_s = 5.0\nretention_days = 30\ndb_path = "{tmp_path}/tr.db"\n',
        )
    )
    traffic = cfg.traffic
    assert traffic is not None
    assert traffic.base_url == "http://localhost:8080"  # trailing slash normalized off
    assert traffic.timeout_s == 5.0
    assert traffic.retention_days == 30
    assert traffic.db_path == tmp_path / "tr.db"


def test_traffic_db_path_expands_tilde(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[traffic]\n" + URL + 'db_path = "~/traffic.db"\n'))
    traffic = cfg.traffic
    assert traffic is not None
    assert traffic.db_path == Path.home() / "traffic.db"


@pytest.mark.parametrize(
    "body",
    [
        '[traffic]\nbase_url = ""\n',
        '[traffic]\nbase_url = "ftp://traffic.example.net"\n',
        "[traffic]\n" + URL + "timeout_s = 0\n",
        "[traffic]\n" + URL + "timeout_s = -1.0\n",
        "[traffic]\n" + URL + "retention_days = 0\n",
    ],
)
def test_traffic_bad_values_fail_at_load(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


def test_traffic_settings_defaults_standalone() -> None:
    settings = TrafficSettings(base_url="https://traffic.example.net")
    assert settings.base_url == "https://traffic.example.net"
    assert settings.db_path.name == "traffic.db"
