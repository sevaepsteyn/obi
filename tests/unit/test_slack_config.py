"""T012: the [slack] config section parses, validates, and keeps tokens secret (feature 009)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, load_config

_BASE = """
[routeros]
username = "obi"
password = "pw"

[discovery]
seeds = ["172.25.110.1"]
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_BASE + body)
    return path


def test_slack_section_parses(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[slack]
bot_token = "xoxb-abc"
app_token = "xapp-xyz"
allowlist = ["U0FAKEDCE78", "U999"]
""",
        )
    )
    assert config.slack is not None
    assert config.slack.allowlist == ["U0FAKEDCE78", "U999"]
    assert config.slack.bot_token.reveal() == "xoxb-abc"
    assert config.slack.app_token.reveal() == "xapp-xyz"


def test_slack_absent_is_none(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, ""))
    assert config.slack is None


def test_slack_missing_app_token_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, '\n[slack]\nbot_token = "xoxb-abc"\nallowlist = ["U1"]\n')
        )


def test_slack_empty_allowlist_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                '\n[slack]\nbot_token = "xoxb-abc"\napp_token = "xapp-xyz"\nallowlist = []\n',
            )
        )


def test_slack_tokens_are_secret(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[slack]
bot_token = "xoxb-secret"
app_token = "xapp-secret"
allowlist = ["U1"]
""",
        )
    )
    assert config.slack is not None
    assert "xoxb-secret" not in repr(config.slack.bot_token)
    assert str(config.slack.bot_token) == "***"


# --- feature 104: authorized_channels -----------------------------------------------------------

_TOKENS = '\n[slack]\nbot_token = "xoxb-abc"\napp_token = "xapp-xyz"\nallowlist = ["U1"]\n'


def test_authorized_channels_absent_is_empty(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _TOKENS))
    assert config.slack is not None
    assert config.slack.authorized_channels == []


def test_authorized_channels_parses(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, _TOKENS + 'authorized_channels = ["C0FAKE66336", "G0FAKEC323B"]\n')
    )
    assert config.slack is not None
    assert config.slack.authorized_channels == ["C0FAKE66336", "G0FAKEC323B"]


def test_authorized_channels_not_a_list_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _TOKENS + 'authorized_channels = "C0FAKE66336"\n'))


def test_authorized_channels_empty_entry_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _TOKENS + 'authorized_channels = ["C0FAKE66336", ""]\n'))


def test_authorized_channels_non_string_entry_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, _TOKENS + "authorized_channels = [123]\n"))


def test_authorized_channels_empty_list_is_allowed(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _TOKENS + "authorized_channels = []\n"))
    assert config.slack is not None
    assert config.slack.authorized_channels == []
