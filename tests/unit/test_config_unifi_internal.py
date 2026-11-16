"""T003: config tests for the 3-section UniFi config (feature 016) + feature-011 back-compat."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config


def _write(tmp_path: Path, text: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    cfg.chmod(0o600)
    return cfg


_THREE_SECTION = """
[unifi]
base_url = "https://console.unifi-hosting.ui.com"
sites = ["Alder", "Chestnut"]
verify_tls = true

[unifi-network]
api_key = "x-api-key-value"

[unifi-internal]
username = "admin"
password = "leave-no-trace"
"""


def test_three_section_parses_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _THREE_SECTION))
    assert cfg.unifi_network is not None
    assert cfg.unifi_network.base_url == "https://console.unifi-hosting.ui.com"
    assert cfg.unifi_network.sites == ["Alder", "Chestnut"]
    assert cfg.unifi_network.api_key is not None
    assert cfg.unifi_network.api_key.reveal() == "x-api-key-value"

    ui = cfg.unifi_internal
    assert ui is not None
    assert ui.base_url == "https://console.unifi-hosting.ui.com"  # inherited from [unifi]
    assert ui.sites == ["Alder", "Chestnut"]  # inherited from [unifi]
    assert ui.username.reveal() == "admin"
    assert ui.password.reveal() == "leave-no-trace"
    # CLIENT_DEVICES is excluded by default (FR-005); other categories present.
    assert "CLIENT_DEVICES" not in ui.categories
    assert "UNIFI_DEVICES" in ui.categories
    assert ui.retention_window == "7d"  # default first-run backfill lookback (FR-017)


def test_secrets_are_wrapped(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _THREE_SECTION))
    ui = cfg.unifi_internal
    assert ui is not None
    assert "leave-no-trace" not in repr(ui)
    assert str(ui.password) == "***"


_BACKCOMPAT_011 = """
[unifi-network]
base_url = "https://console.unifi-hosting.ui.com"
sites = ["Alder"]
verify_tls = true
api_key = "x-api-key-value"
"""


def test_backcompat_011_loads_unchanged(tmp_path: Path) -> None:
    # An existing feature-011 config (base_url/sites under [unifi-network], no [unifi]) still loads,
    # and ingestion stays inactive without [unifi-internal] (SC-007, FR-013).
    cfg = load_config(_write(tmp_path, _BACKCOMPAT_011))
    assert cfg.unifi_network is not None
    assert cfg.unifi_network.base_url == "https://console.unifi-hosting.ui.com"
    assert cfg.unifi_network.sites == ["Alder"]
    assert cfg.unifi_internal is None


_INHERITS_FROM_NETWORK = """
[unifi-network]
base_url = "https://console.unifi-hosting.ui.com"
sites = ["Alder"]
api_key = "x-api-key-value"

[unifi-internal]
username = "admin"
password = "leave-no-trace"
"""


def test_internal_inherits_coordinates_from_network(tmp_path: Path) -> None:
    # No [unifi] block: [unifi-internal] inherits base_url/sites from [unifi-network] (back-compat).
    cfg = load_config(_write(tmp_path, _INHERITS_FROM_NETWORK))
    ui = cfg.unifi_internal
    assert ui is not None
    assert ui.base_url == "https://console.unifi-hosting.ui.com"
    assert ui.sites == ["Alder"]


def test_categories_override_uppercased(tmp_path: Path) -> None:
    text = """
[unifi]
base_url = "https://c.example.com"

[unifi-internal]
username = "admin"
password = "pw"
categories = ["unifi_devices", "power"]
retention_window = "3d"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.unifi_internal is not None
    assert cfg.unifi_internal.categories == ["UNIFI_DEVICES", "POWER"]
    assert cfg.unifi_internal.retention_window == "3d"


def test_internal_requires_username_and_password(tmp_path: Path) -> None:
    text = """
[unifi]
base_url = "https://c.example.com"

[unifi-internal]
username = "admin"
"""
    with pytest.raises(ConfigError, match="username.*password"):
        load_config(_write(tmp_path, text))


def test_internal_requires_base_url(tmp_path: Path) -> None:
    # [logs] satisfies the >=1-source gate so parsing reaches the [unifi-internal] base_url check.
    text = """
[logs]

[unifi-internal]
username = "admin"
password = "pw"
"""
    with pytest.raises(ConfigError, match="base_url"):
        load_config(_write(tmp_path, text))


def test_unifi_internal_alone_is_a_valid_source(tmp_path: Path) -> None:
    text = """
[unifi]
base_url = "https://c.example.com"

[unifi-internal]
username = "admin"
password = "pw"
"""
    cfg = load_config(_write(tmp_path, text))  # does not raise "no source configured"
    assert cfg.unifi_internal is not None
