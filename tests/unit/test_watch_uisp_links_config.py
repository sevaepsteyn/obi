"""Unit (feature 034): parsing the opt-in ``[watch.uisp_links]`` subsection.

Asserts the pass is inactive without the subsection, the defaults, custom values, single-string
ignore normalization, that a non-positive ``signal_drop_db`` fails fast, and the cross-section
rule: enabling the pass without a ``[uisp]`` section is a load-time ConfigError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, WatchUispLinksSettings, load_config


def _write(tmp_path: Path, watch_block: str, *, uisp: bool = True) -> Path:
    path = tmp_path / "config.toml"
    sections = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    if uisp:
        sections += '[uisp]\nbase_url = "https://uisp.example.com"\ntoken = "tok"\n\n'
    path.write_text(f"{sections}[watch]\nenabled = true\n{watch_block}")
    path.chmod(0o600)
    return path


def _links(tmp_path: Path, block: str, *, uisp: bool = True) -> WatchUispLinksSettings:
    cfg = load_config(_write(tmp_path, block, uisp=uisp))
    assert cfg.watch.uisp_links is not None
    return cfg.watch.uisp_links


def test_absent_uisp_links_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.watch.uisp_links is None  # other passes unchanged


def test_defaults(tmp_path: Path) -> None:
    settings = _links(tmp_path, "[watch.uisp_links]\n")
    assert settings.enabled is False  # presence alone does not activate
    assert settings.signal_drop_db == 6.0
    assert settings.ignore_links == []


def test_enabled_and_custom_values(tmp_path: Path) -> None:
    settings = _links(
        tmp_path,
        '[watch.uisp_links]\nenabled = true\nsignal_drop_db = 3.5\n'
        'ignore_links = ["lab-", "test"]\n',
    )
    assert settings.enabled is True
    assert settings.signal_drop_db == 3.5
    assert settings.ignore_links == ["lab-", "test"]


def test_single_string_ignore_normalizes_to_list(tmp_path: Path) -> None:
    settings = _links(tmp_path, '[watch.uisp_links]\nignore_links = "lab-"\n')
    assert settings.ignore_links == ["lab-"]


def test_zero_signal_drop_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _links(tmp_path, "[watch.uisp_links]\nsignal_drop_db = 0\n")


def test_negative_signal_drop_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _links(tmp_path, "[watch.uisp_links]\nsignal_drop_db = -2\n")


def test_enabled_without_uisp_section_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[watch.uisp_links\] requires a \[uisp\] section"):
        load_config(_write(tmp_path, "[watch.uisp_links]\nenabled = true\n", uisp=False))


def test_disabled_without_uisp_section_is_fine(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[watch.uisp_links]\n", uisp=False))
    assert cfg.watch.uisp_links is not None
    assert cfg.watch.uisp_links.enabled is False
