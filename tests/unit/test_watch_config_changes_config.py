"""Unit (feature 070): parsing the opt-in ``[watch.config_changes]`` subsection.

Asserts the pass is inactive without the subsection, the defaults, custom values, single-string
ignore normalization, and that a bad severity or non-positive ``max_diff_lines`` fails fast. Unlike
``[watch.uisp_links]`` there is NO cross-section requirement — the pass reads a local git repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import (
    ConfigError,
    WatchConfigChangesSettings,
    default_config_repo_dir,
    load_config,
)


def _write(tmp_path: Path, watch_block: str) -> Path:
    path = tmp_path / "config.toml"
    sections = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(f"{sections}[watch]\nenabled = true\n{watch_block}")
    path.chmod(0o600)
    return path


def _cc(tmp_path: Path, block: str) -> WatchConfigChangesSettings:
    cfg = load_config(_write(tmp_path, block))
    assert cfg.watch.config_changes is not None
    return cfg.watch.config_changes


def test_absent_config_changes_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.watch.config_changes is None  # other passes unchanged


def test_defaults(tmp_path: Path) -> None:
    settings = _cc(tmp_path, "[watch.config_changes]\n")
    assert settings.enabled is False  # presence alone does not activate
    assert settings.severity == "warning"
    assert settings.max_diff_lines == 120
    assert settings.ignore_devices == []
    assert settings.repo_path == default_config_repo_dir()


def test_enabled_and_custom_values(tmp_path: Path) -> None:
    settings = _cc(
        tmp_path,
        "[watch.config_changes]\nenabled = true\nseverity = \"info\"\nmax_diff_lines = 40\n"
        'ignore_devices = ["lab-", "test"]\nrepo_path = "/tmp/repo"\n',
    )
    assert settings.enabled is True
    assert settings.severity == "info"
    assert settings.max_diff_lines == 40
    assert settings.ignore_devices == ["lab-", "test"]
    assert str(settings.repo_path) == "/tmp/repo"


def test_single_string_ignore_normalizes_to_list(tmp_path: Path) -> None:
    settings = _cc(tmp_path, '[watch.config_changes]\nignore_devices = "lab-"\n')
    assert settings.ignore_devices == ["lab-"]


def test_bad_severity_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[watch.config_changes\] severity"):
        _cc(tmp_path, '[watch.config_changes]\nseverity = "loud"\n')


def test_non_positive_max_diff_lines_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[watch.config_changes\] max_diff_lines"):
        _cc(tmp_path, "[watch.config_changes]\nmax_diff_lines = 0\n")
