"""T008: [netbox] config parsing and the relaxed (>=1 source) validation (feature 007)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, Secret, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(body)
    config.chmod(0o600)
    return config


def test_netbox_only_config_parses_without_scope(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[netbox]
base_url = "https://netbox.example.com/"
token = "TOK123"
verify_tls = false
timeout = 9.0
name_filter = "core"
""",
        )
    )
    assert cfg.routeros is None
    assert cfg.uisp is None
    assert cfg.scope is None  # NetBox-only needs no discovery scope
    assert cfg.netbox is not None
    assert cfg.netbox.base_url == "https://netbox.example.com"  # trailing slash stripped
    assert cfg.netbox.token == Secret("TOK123")
    assert cfg.netbox.verify_tls is False
    assert cfg.netbox.timeout == 9.0
    assert cfg.netbox.name_filter == "core"


def test_netbox_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[netbox]\nbase_url = "https://nb.example.com"\ntoken = "T"\n')
    )
    assert cfg.netbox is not None
    assert cfg.netbox.verify_tls is True  # default-on (FR-018)
    assert cfg.netbox.timeout == 15.0
    assert cfg.netbox.name_filter is None


def test_token_is_secret_not_plaintext(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[netbox]\nbase_url = "https://nb"\ntoken = "SECRET-XYZ"\n')
    )
    assert cfg.netbox is not None
    assert "SECRET-XYZ" not in repr(cfg.netbox)
    assert "SECRET-XYZ" not in str(cfg.netbox.token)


def test_netbox_requires_base_url_and_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="requires both"):
        load_config(_write(tmp_path, '[netbox]\nbase_url = "https://nb"\n'))


def test_no_source_configured_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no source configured"):
        load_config(_write(tmp_path, '[audit]\npath = "/tmp/a.jsonl"\n'))


def test_netbox_alongside_other_sources(tmp_path: Path) -> None:
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
[netbox]
base_url = "https://nb"
token = "NT"
""",
        )
    )
    assert cfg.routeros is not None
    assert cfg.uisp is not None
    assert cfg.netbox is not None
    assert cfg.scope is not None
