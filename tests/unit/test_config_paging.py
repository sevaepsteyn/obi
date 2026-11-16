"""Unit: the [paging] config section (feature 102)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, default_paging_roster_path, load_config

URL = "https://pager.example.net/api.php"


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text('[inventory]\npath = "inv.json"\n[logs]\ndb_path = "l.db"\n' + body)
    config.chmod(0o600)
    return config


def test_paging_section_parsed_with_defaults(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[paging]
url = "https://pager.example.net/api.php"
username = "mira"
password = "hunter2"
""",
        )
    )
    assert config.paging is not None
    assert config.paging.username == "mira"
    assert config.paging.password.reveal() == "hunter2"
    assert config.paging.url == "https://pager.example.net/api.php"
    assert config.paging.roster_path == default_paging_roster_path()
    assert config.paging.message_max_chars == 200
    assert config.paging.send_spacing_s == 2.0
    assert config.paging.alerts.enabled is False
    assert config.paging.alerts.people == []
    assert config.paging.alerts.max_pages_per_hour == 6
    # The password never leaks through repr (Secret discipline).
    assert "hunter2" not in repr(config.paging)


def test_paging_absent_is_none(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, ""))
    assert config.paging is None


def test_paging_overrides(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            f"""
[paging]
username = "mira"
password = "pw"
url = "https://203.0.113.21/api.php"
roster_path = "{tmp_path / "roster.csv"}"
message_max_chars = 120
send_spacing_s = 0.0

[paging.alerts]
enabled = true
people = ["24", "430"]
max_pages_per_hour = 3
""",
        )
    )
    assert config.paging is not None
    assert config.paging.url.startswith("https://203.0.113.21/")
    assert config.paging.roster_path == tmp_path / "roster.csv"
    assert config.paging.message_max_chars == 120
    assert config.paging.send_spacing_s == 0.0
    assert config.paging.alerts.enabled is True
    assert config.paging.alerts.people == ["24", "430"]
    assert config.paging.alerts.max_pages_per_hour == 3


def test_paging_requires_credentials(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[paging\].*username.*password"):
        load_config(
            _write(
                tmp_path, '[paging]\nurl = "https://pager.example.net/api.php"\nusername = "mira"\n'
            )
        )


def test_paging_alert_people_must_be_numeric_ids(tmp_path: Path) -> None:
    # Clarification #2: the automated path is person-IDs-only, enforced at load — a name or
    # group here could page a ~100-person group on a malfunction.
    with pytest.raises(ConfigError, match=r"numeric person IDs only.*'WeatherAlert'"):
        load_config(
            _write(
                tmp_path,
                """
[paging]
url = "https://pager.example.net/api.php"
username = "mira"
password = "pw"

[paging.alerts]
people = ["24", "WeatherAlert"]
""",
            )
        )


def test_paging_alerts_enabled_requires_people(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[paging.alerts\].*'people' is empty"):
        load_config(
            _write(
                tmp_path,
                """
[paging]
url = "https://pager.example.net/api.php"
username = "mira"
password = "pw"

[paging.alerts]
enabled = true
""",
            )
        )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('url = "ftp://x"', r"url must be an http\(s\) URL"),
        ("message_max_chars = 20", r"message_max_chars must be >= 40"),
        ("send_spacing_s = -1", r"send_spacing_s must be >= 0"),
    ],
)
def test_paging_bounds_fail_at_load(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(
            _write(
                tmp_path,
                '[paging]\nusername = "s"\npassword = "p"\n'
                + (body if "url" in body else f'url = "{URL}"\n' + body)
                + "\n",
            )
        )


def test_paging_alert_cap_bound(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"max_pages_per_hour must be >= 1"):
        load_config(
            _write(
                tmp_path,
                """
[paging]
url = "https://pager.example.net/api.php"
username = "s"
password = "p"

[paging.alerts]
people = ["24"]
max_pages_per_hour = 0
""",
            )
        )


# --- [paging.slack] — Slack-DM member paging (feature 130, constitution 3.0.0) ----------------


def test_paging_slack_absent_is_none(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            '[paging]\nurl = "https://pager.example.net/api.php"\nusername = "s"\npassword = "p"\n',
        )
    )
    assert config.paging is not None
    assert config.paging.slack is None  # feature off ⇒ exactly pre-130 behavior


def test_paging_slack_parsed_with_defaults(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[paging]
url = "https://pager.example.net/api.php"
username = "s"
password = "p"

[paging.slack]
channels = ["C0FAKEC7B64"]
recipients = ["mira", "wan on call", "towers on call"]
""",
        )
    )
    assert config.paging is not None
    slack = config.paging.slack
    assert slack is not None
    assert slack.channels == ["C0FAKEC7B64"]
    assert slack.recipients == ["mira", "wan on call", "towers on call"]
    assert slack.max_pages_per_hour == 4


def test_paging_slack_empty_channels_is_operators_only(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            """
[paging]
url = "https://pager.example.net/api.php"
username = "s"
password = "p"

[paging.slack]
recipients = ["mira"]
max_pages_per_hour = 2
""",
        )
    )
    assert config.paging is not None and config.paging.slack is not None
    assert config.paging.slack.channels == []
    assert config.paging.slack.max_pages_per_hour == 2


def test_paging_slack_requires_recipients(tmp_path: Path) -> None:
    # The recipient allowlist is load-bearing (operator decision 2026-09-03): its absence
    # must never mean "the whole roster".
    for body in ("", "recipients = []\n"):
        with pytest.raises(ConfigError, match=r"non-empty 'recipients'"):
            load_config(
                _write(
                    tmp_path,
                    f'[paging]\nurl = "https://pager.example.net/api.php"\nusername = "s"\n'
                    f'password = "p"\n[paging.slack]\n{body}',
                )
            )


def test_paging_slack_cap_bound(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[paging.slack\] max_pages_per_hour must be >= 1"):
        load_config(
            _write(
                tmp_path,
                """
[paging]
url = "https://pager.example.net/api.php"
username = "s"
password = "p"

[paging.slack]
recipients = ["mira"]
max_pages_per_hour = 0
""",
            )
        )
