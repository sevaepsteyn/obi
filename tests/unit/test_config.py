"""T006: unit tests for config loading."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from obi.core.config import Config, ConfigError, Secret, load_config, perms_warning

_VALID = """
[discovery]
ranges = ["172.25.110.0/24"]
seeds = ["172.25.111.1"]

[routeros]
username = "obi-readonly"
password = "super-secret-pw"
use_tls = true
"""


def _write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(text)
    cfg.chmod(mode)
    return cfg


def test_load_valid_config(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID))
    assert isinstance(cfg, Config)
    assert cfg.scope.ranges == ["172.25.110.0/24"]
    assert cfg.scope.seeds == ["172.25.111.1"]
    assert cfg.routeros.username == "obi-readonly"
    assert cfg.routeros.use_tls is True
    assert cfg.routeros.port == 8729  # TLS default
    assert cfg.routeros.password.reveal() == "super-secret-pw"


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_empty_scope_raises_config_error(tmp_path: Path) -> None:
    text = """
[discovery]
ranges = []
seeds = []

[routeros]
username = "u"
password = "p"
"""
    with pytest.raises(ConfigError, match="scope is empty"):
        load_config(_write(tmp_path, text))


def test_missing_credentials_raises_config_error(tmp_path: Path) -> None:
    text = """
[discovery]
seeds = ["172.25.111.1"]

[routeros]
username = "u"
"""
    with pytest.raises(ConfigError, match="username.*password|password"):
        load_config(_write(tmp_path, text))


def test_password_never_appears_in_repr() -> None:
    secret = Secret("fakesecret44028f")
    assert "fakesecret44028f" not in repr(secret)
    assert "fakesecret44028f" not in str(secret)
    assert secret.reveal() == "fakesecret44028f"


def test_password_never_appears_in_config_repr(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID))
    assert "super-secret-pw" not in repr(cfg)
    assert "super-secret-pw" not in repr(cfg.routeros)


def test_password_never_leaks_through_logging(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = load_config(_write(tmp_path, _VALID))
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("obi").info("loaded config %s", cfg)
        logging.getLogger("obi").info("settings %s", cfg.routeros)
        logging.getLogger("obi").info("password field %s", cfg.routeros.password)
    assert "super-secret-pw" not in caplog.text


def test_perms_warning_flags_group_readable(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _VALID, mode=0o644)
    assert perms_warning(cfg) is not None


def test_perms_warning_clean_for_0600(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _VALID, mode=0o600)
    assert perms_warning(cfg) is None


# --- Slack delivery config (feature 015) ------------------------------------------------------

_SLACK = """
[slack]
bot_token = "xoxb-x"
app_token = "xapp-x"
allowlist = ["U_OP"]
"""


def test_watch_slack_keys_parse(tmp_path: Path) -> None:
    text = _VALID + _SLACK + (
        "\n[watch]\nenabled = true\nslack_notify = true\n"
        'slack_min_severity = "critical"\nslack_user = "U_OTHER"\n'
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.watch.slack_notify is True
    assert cfg.watch.slack_min_severity == "critical"
    assert cfg.watch.slack_user == "U_OTHER"


def test_watch_slack_defaults_when_absent(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID + "\n[watch]\nenabled = true\n"))
    assert cfg.watch.slack_notify is False
    assert cfg.watch.slack_min_severity == "warning"
    assert cfg.watch.slack_user is None


# --- feature 039: [watch] ignore_collectors ---------------------------------------------------


def test_watch_ignore_collectors_parses_list(tmp_path: Path) -> None:
    text = _VALID + '\n[watch]\nenabled = true\nignore_collectors = ["unifi", "uisp"]\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.watch.ignore_collectors == ["unifi", "uisp"]


def test_watch_ignore_collectors_defaults_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID + "\n[watch]\nenabled = true\n"))
    assert cfg.watch.ignore_collectors == []


def test_watch_ignore_collectors_non_list_is_empty(tmp_path: Path) -> None:
    # A scalar (mis-typed) value is ignored rather than raising — mirrors ignore_topics.
    text = _VALID + '\n[watch]\nenabled = true\nignore_collectors = "unifi"\n'
    cfg = load_config(_write(tmp_path, text))
    assert cfg.watch.ignore_collectors == []


def test_invalid_slack_min_severity_raises(tmp_path: Path) -> None:
    text = _VALID + '\n[watch]\nslack_min_severity = "bogus"\n'
    with pytest.raises(ConfigError, match="slack_min_severity"):
        load_config(_write(tmp_path, text))


def test_slack_notify_without_bot_token_raises(tmp_path: Path) -> None:
    text = _VALID + "\n[watch]\nslack_notify = true\n"  # no [slack] section
    with pytest.raises(ConfigError, match="bot_token"):
        load_config(_write(tmp_path, text))


# --- feature 026: [configs] config backup -----------------------------------------------------


def test_configs_absent_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID))
    assert cfg.configs is None


def test_configs_present_inherits_mikrotik_creds(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _VALID + "\n[configs]\n"))
    assert cfg.configs is not None
    # creds fall back to [routeros] when [configs] gives none of its own
    assert cfg.configs.username == "obi-readonly"
    assert cfg.configs.password.reveal() == "super-secret-pw"
    assert cfg.configs.ssh_port == 22
    assert cfg.configs.targets == ""  # default: every device (whole fleet)


def test_configs_own_creds_and_overrides(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    text = _VALID + (
        f'\n[configs]\nusername = "backup"\npassword = "bkp-pw"\n'
        f'ssh_port = 2222\ntargets = "-gw"\nconcurrency = 4\nrepo_path = "{repo}"\n'
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.configs.username == "backup"
    assert cfg.configs.password.reveal() == "bkp-pw"
    assert cfg.configs.ssh_port == 2222
    assert cfg.configs.targets == "-gw"
    assert cfg.configs.concurrency == 4
    assert cfg.configs.repo_path == repo


def test_configs_without_any_creds_raises(tmp_path: Path) -> None:
    # [configs] present, its own creds absent, and NO [routeros] to inherit from → error.
    text = """
[uisp]
base_url = "https://uisp.example.com"
token = "UISP-TOKEN"

[configs]
"""
    with pytest.raises(ConfigError, match=r"\[configs\]"):
        load_config(_write(tmp_path, text))


def test_configs_bad_ssh_port_raises(tmp_path: Path) -> None:
    text = _VALID + "\n[configs]\nssh_port = 70000\n"
    with pytest.raises(ConfigError, match="ssh_port"):
        load_config(_write(tmp_path, text))
