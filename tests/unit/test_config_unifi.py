"""T003: [unifi-network] and [unifi-sm] config parsing + validation (feature 011, FR-012)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, Secret, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(body)
    config.chmod(0o600)
    return config


# --- [unifi-network] ---------------------------------------------------------------------------


def test_network_api_key_only_parses(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[unifi-network]
base_url = "https://console.unifi-hosting.example.com/"
api_key = "KEY123"
sites = ["SF", "Alder"]
verify_tls = false
timeout = 9.0
name_filter = "ridge"
""",
        )
    )
    assert cfg.unifi_network is not None
    un = cfg.unifi_network
    assert un.base_url == "https://console.unifi-hosting.example.com"  # trailing slash stripped
    assert un.api_key == Secret("KEY123")
    assert un.token is None
    assert un.sites == ["SF", "Alder"]
    assert un.verify_tls is False
    assert un.timeout == 9.0
    assert un.name_filter == "ridge"


def test_network_token_only_parses(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, '[unifi-network]\nbase_url = "https://c"\ntoken = "TOK"\n')
    )
    assert cfg.unifi_network is not None
    assert cfg.unifi_network.api_key is None
    assert cfg.unifi_network.token == Secret("TOK")


def test_network_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[unifi-network]\nbase_url = "https://c"\napi_key = "K"\n'))
    assert cfg.unifi_network is not None
    assert cfg.unifi_network.sites == []  # empty = all sites
    assert cfg.unifi_network.verify_tls is True
    assert cfg.unifi_network.timeout == 15.0
    assert cfg.unifi_network.name_filter is None


def test_network_requires_base_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        load_config(_write(tmp_path, '[unifi-network]\napi_key = "K"\n'))


def test_network_requires_at_least_one_credential(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least one"):
        load_config(_write(tmp_path, '[unifi-network]\nbase_url = "https://c"\n'))


def test_network_credentials_are_secret(tmp_path: Path) -> None:
    body = '[unifi-network]\nbase_url = "https://c"\napi_key = "KEY-XYZ"\ntoken = "TOK-XYZ"\n'
    cfg = load_config(_write(tmp_path, body))
    assert cfg.unifi_network is not None
    assert "KEY-XYZ" not in repr(cfg.unifi_network)
    assert "TOK-XYZ" not in repr(cfg.unifi_network)


# --- [unifi-sm] --------------------------------------------------------------------------------


def test_sm_parses_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[unifi-sm]\napi_key = "SMKEY"\n'))
    assert cfg.unifi_sm is not None
    assert cfg.unifi_sm.api_key == Secret("SMKEY")
    assert cfg.unifi_sm.base_url == "https://api.ui.com"  # default endpoint
    assert cfg.unifi_sm.verify_tls is True
    assert cfg.unifi_sm.timeout == 15.0


def test_sm_requires_api_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="api_key"):
        load_config(_write(tmp_path, '[unifi-sm]\nbase_url = "https://api.ui.com"\n'))


def test_sm_api_key_is_secret(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[unifi-sm]\napi_key = "SM-SECRET"\n'))
    assert cfg.unifi_sm is not None
    assert "SM-SECRET" not in repr(cfg.unifi_sm)
    assert "SM-SECRET" not in str(cfg.unifi_sm.api_key)


# --- cross-cutting -----------------------------------------------------------------------------


def test_each_unifi_section_is_a_valid_standalone_vendor(tmp_path: Path) -> None:
    net = load_config(_write(tmp_path, '[unifi-network]\nbase_url = "https://c"\napi_key = "K"\n'))
    assert net.routeros is None and net.uisp is None and net.netbox is None and net.scope is None
    assert net.unifi_network is not None and net.unifi_sm is None

    sm = load_config(_write(tmp_path, '[unifi-sm]\napi_key = "K"\n'))
    assert sm.unifi_sm is not None and sm.unifi_network is None


def test_no_vendor_configured_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no source configured"):
        load_config(_write(tmp_path, '[audit]\npath = "/tmp/a.jsonl"\n'))
