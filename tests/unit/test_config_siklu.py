"""Unit: the [siklu] config section (feature 020)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(body)
    config.chmod(0o600)
    return config


def test_siklu_section_parsed(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[siklu]
hosts = ["172.18.89.130", "basswood-ptp-tamarack-70.net.example.net"]
username = "admin"
password = "fakesecret44028f"
port = 22
timeout = 8.0
concurrency = 4
pairs = [["alpha", "beta"]]
name_filter = "ptp"
""",
        )
    )
    assert config.siklu is not None
    assert config.siklu.hosts == [
        "172.18.89.130",
        "basswood-ptp-tamarack-70.net.example.net",
    ]
    assert config.siklu.username == "admin"
    assert config.siklu.password.reveal() == "fakesecret44028f"
    assert config.siklu.timeout == 8.0
    assert config.siklu.concurrency == 4
    assert config.siklu.pairs == [("alpha", "beta")]
    assert config.siklu.name_filter == "ptp"
    # The password is masked in repr (never leaks).
    assert "fakesecret44028f" not in repr(config.siklu)


def test_siklu_defaults(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[siklu]
hosts = ["172.25.110.1"]
username = "admin"
password = "pw"
""",
        )
    )
    assert config.siklu is not None
    assert config.siklu.port == 22
    assert config.siklu.timeout == 6.0
    assert config.siklu.concurrency == 8
    assert config.siklu.pairs is None


def test_empty_hosts_is_clean_no_radios(tmp_path: Path) -> None:
    # An empty/missing roster ⇒ siklu is simply not configured (None), not an error (FR-010).
    # A second source keeps the config valid.
    config = load_config(
        _write(
            tmp_path,
            """
[uisp]
base_url = "https://u"
token = "t"

[siklu]
hosts = []
""",
        )
    )
    assert config.siklu is None


def test_hosts_without_credentials_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="username.*password"):
        load_config(
            _write(
                tmp_path,
                """
[siklu]
hosts = ["172.25.110.1"]
""",
            )
        )


def test_hosts_deduped_order_preserved(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[siklu]
hosts = ["172.25.110.1", "172.25.110.2", "172.25.110.1"]
username = "admin"
password = "pw"
""",
        )
    )
    assert config.siklu is not None
    assert config.siklu.hosts == ["172.25.110.1", "172.25.110.2"]


def test_bad_pair_shape_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="pairs"):
        load_config(
            _write(
                tmp_path,
                """
[siklu]
hosts = ["172.25.110.1"]
username = "admin"
password = "pw"
pairs = [["only-one"]]
""",
            )
        )
