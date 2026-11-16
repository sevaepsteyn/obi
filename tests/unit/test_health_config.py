"""Unit (feature 096): parsing the opt-in ``[health.linux]`` subsection."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config

_BASE = """
[logs]
mirror_root = "{root}/mirror"
db_path = "{root}/logs.db"
"""


def _load(tmp_path: Path, block: str):
    path = tmp_path / "config.toml"
    path.write_text(_BASE.format(root=tmp_path) + block)
    path.chmod(0o600)
    return load_config(path)


def test_absent_section_is_none(tmp_path: Path) -> None:
    assert _load(tmp_path, "").health_linux is None


def test_empty_hosts_is_absence_not_error(tmp_path: Path) -> None:
    assert _load(tmp_path, "[health.linux]\nhosts = []\n").health_linux is None


def test_defaults_and_dedupe(tmp_path: Path) -> None:
    block = (
        '[health.linux]\nusername = "probe"\n'
        'hosts = ["collector-1", "collector-2", "collector-1"]\n'
    )
    s = _load(tmp_path, block).health_linux
    assert s is not None
    assert s.hosts == ["collector-1", "collector-2"]  # order-preserving de-dupe
    assert s.username == "probe"
    assert s.timeout == 15.0
    assert s.concurrency == 5


def test_custom_values(tmp_path: Path) -> None:
    block = (
        '[health.linux]\nhosts = ["a"]\nusername = "probe"\ntimeout = 5.5\n'
        f'concurrency = 2\ndb_path = "{tmp_path}/m.db"\n'
    )
    s = _load(tmp_path, block).health_linux
    assert s is not None
    assert s.username == "probe" and s.timeout == 5.5 and s.concurrency == 2
    assert s.db_path == tmp_path / "m.db"


@pytest.mark.parametrize(
    "bad",
    ['hosts = "core-vms-1"', 'hosts = ["a"]\ntimeout = 0', 'hosts = ["a"]\nconcurrency = 0'],
)
def test_bad_values_raise_config_error(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match=r"\[health\.linux\]"):
        _load(tmp_path, f"[health.linux]\n{bad}\n")


def test_no_password_field_exists(tmp_path: Path) -> None:
    """Key-based trust only — the section cannot even carry a secret."""

    from obi.core.config import HealthLinuxSettings

    assert "password" not in HealthLinuxSettings.__dataclass_fields__
