"""T008: [uisp] config parsing and the relaxed (>=1 vendor) validation (feature 003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, Secret, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(body)
    config.chmod(0o600)
    return config


def test_uisp_only_config_parses_without_scope(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[uisp]
base_url = "https://uisp.example.com/"
token = "TOK123"
verify_tls = false
timeout = 9.0
name_filter = "ptp"
""",
        )
    )
    assert cfg.routeros is None
    assert cfg.scope is None  # UISP-only needs no discovery scope
    assert cfg.uisp is not None
    assert cfg.uisp.base_url == "https://uisp.example.com"  # trailing slash stripped
    assert cfg.uisp.token == Secret("TOK123")
    assert cfg.uisp.verify_tls is False
    assert cfg.uisp.timeout == 9.0
    assert cfg.uisp.name_filter == "ptp"


def test_uisp_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[uisp]\nbase_url = "https://u.example.com"\ntoken = "T"\n')
    )
    assert cfg.uisp is not None
    assert cfg.uisp.verify_tls is True  # default-on (FR-018)
    assert cfg.uisp.timeout == 15.0
    assert cfg.uisp.name_filter is None
    assert cfg.uisp.ignore_names == []  # feature 035: default empty ⇒ excludes nothing
    assert cfg.uisp.location_sanity_m == 75.0  # feature 079: default site-anchoring radius


def test_uisp_location_sanity_m_explicit(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[uisp]\nbase_url = "https://u"\ntoken = "T"\nlocation_sanity_m = 40\n')
    )
    assert cfg.uisp is not None
    assert cfg.uisp.location_sanity_m == 40.0


def test_uisp_location_sanity_m_zero_preserved_disables(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[uisp]\nbase_url = "https://u"\ntoken = "T"\nlocation_sanity_m = 0\n')
    )
    assert cfg.uisp is not None
    assert cfg.uisp.location_sanity_m == 0.0  # 0 ⇒ correction disabled (not coerced to default)


def test_uisp_location_sanity_m_non_numeric_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="location_sanity_m"):
        load_config(
            _write(
                tmp_path,
                '[uisp]\nbase_url = "https://u"\ntoken = "T"\nlocation_sanity_m = "far"\n',
            )
        )


def test_uisp_ignore_names_list(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[uisp]\nbase_url = "https://u"\ntoken = "T"\nignore_names = ["aspen", "playa-2"]\n',
        )
    )
    assert cfg.uisp is not None
    assert cfg.uisp.ignore_names == ["aspen", "playa-2"]


def test_uisp_ignore_names_single_string_normalizes_to_list(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[uisp]\nbase_url = "https://u"\ntoken = "T"\nignore_names = "aspen"\n')
    )
    assert cfg.uisp is not None
    assert cfg.uisp.ignore_names == ["aspen"]


def test_token_is_secret_not_plaintext(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[uisp]\nbase_url = "https://u"\ntoken = "SECRET-XYZ"\n'))
    assert cfg.uisp is not None
    assert "SECRET-XYZ" not in repr(cfg.uisp)
    assert "SECRET-XYZ" not in str(cfg.uisp.token)


def test_uisp_requires_base_url_and_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires both"):
        load_config(_write(tmp_path, '[uisp]\nbase_url = "https://u"\n'))


def test_no_vendor_configured_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no source configured"):
        load_config(_write(tmp_path, '[audit]\npath = "/tmp/a.jsonl"\n'))


def test_mikrotik_only_still_requires_scope(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="scope"):
        load_config(_write(tmp_path, '[routeros]\nusername = "u"\npassword = "p"\n'))


def test_both_vendors_configured(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[discovery]
seeds = ["172.25.110.1"]
[routeros]
username = "u"
password = "p"
[uisp]
base_url = "https://u"
token = "T"
""",
        )
    )
    assert cfg.routeros is not None
    assert cfg.uisp is not None
    assert cfg.scope is not None
