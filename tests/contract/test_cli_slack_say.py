"""Contract (feature 092, US3): `obi slack say` proves the real notification path works.

The point of this command is that a success here is evidence about the path the WATCHER
uses — so it must go through SlackNotifier, not a second client wired up for testing.

No test touches Slack; the WebClient is a fake.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

runner = CliRunner()
SRC = Path(__file__).resolve().parents[2] / "src" / "obi"


def _app() -> object:
    from obi.cli.main import app

    return app


def _config(tmp_path: Path, *, slack: bool = True) -> Path:
    path = tmp_path / "config.toml"
    body = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    if slack:
        body += (
            '[slack]\nbot_token = "xoxb-test"\napp_token = "xapp-test"\n'
            'allowlist = ["U0FAKEDCE78"]\n\n'
        )
    body += (
        f'[radio]\nftdi_serial = "FX00TEST"\ndb_path = "{tmp_path}/radio.db"\n'
        f'clips_dir = "{tmp_path}/clips"\n'
    )
    path.write_text(body)
    path.chmod(0o600)
    return path


class FakeSlackError(Exception):
    """Shaped like slack_sdk's SlackApiError: the error code hangs off `.response`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"error": code}


class FakeWeb:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.opened: list[list[str]] = []
        self.posts: list[tuple[str, str]] = []

    def conversations_open(self, users: list[str]) -> dict[str, Any]:
        if self.fail:
            raise FakeSlackError("channel_not_found")
        self.opened.append(users)
        return {"channel": {"id": f"D-{users[0]}"}}

    def chat_postMessage(
        self, channel: str, text: str, thread_ts: str | None = None
    ) -> dict[str, Any]:
        if self.fail:
            raise FakeSlackError("not_in_channel")
        self.posts.append((channel, text))
        return {"ts": "1.0"}


def _patch_web(monkeypatch, web: FakeWeb) -> None:  # type: ignore[no-untyped-def]
    import slack_sdk.web

    monkeypatch.setattr(slack_sdk.web, "WebClient", lambda token: web)


def test_no_target_goes_to_the_operator_dm(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    web = FakeWeb()
    _patch_web(monkeypatch, web)
    result = runner.invoke(
        _app(), ["slack", "say", "test from the CLI", "--scope", str(_config(tmp_path))]
    )
    assert result.exit_code == 0, result.output
    assert web.opened == [["U0FAKEDCE78"]]
    assert web.posts == [("D-U0FAKEDCE78", "test from the CLI")]


def test_explicit_user_target_overrides(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    web = FakeWeb()
    _patch_web(monkeypatch, web)
    result = runner.invoke(
        _app(),
        ["slack", "say", "hi", "--to", "U999", "--scope", str(_config(tmp_path))],
    )
    assert result.exit_code == 0, result.output
    assert web.opened == [["U999"]]


def test_channel_target_posts_without_opening_a_dm(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A channel is not a user — conversations_open would fail on it."""

    web = FakeWeb()
    _patch_web(monkeypatch, web)
    result = runner.invoke(
        _app(),
        ["slack", "say", "hi", "--to", "#it-ops", "--scope", str(_config(tmp_path))],
    )
    assert result.exit_code == 0, result.output
    assert web.opened == []
    assert web.posts == [("#it-ops", "hi")]


def test_send_failure_is_reported_and_exits_non_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_web(monkeypatch, FakeWeb(fail=True))
    result = runner.invoke(_app(), ["slack", "say", "hi", "--scope", str(_config(tmp_path))])
    assert result.exit_code != 0
    assert "not_in_channel" in result.output or "channel_not_found" in result.output


def test_slack_not_configured_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(
        _app(), ["slack", "say", "hi", "--scope", str(_config(tmp_path, slack=False))]
    )
    assert result.exit_code != 0
    assert "slack" in result.output.lower()


def test_empty_message_is_refused(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    web = FakeWeb()
    _patch_web(monkeypatch, web)
    result = runner.invoke(_app(), ["slack", "say", "   ", "--scope", str(_config(tmp_path))])
    assert result.exit_code != 0
    assert web.posts == []


def test_say_uses_the_notifier_not_a_second_client() -> None:
    """The command's value is that it exercises the watcher's real path."""

    text = (SRC / "cli" / "main.py").read_text(encoding="utf-8")
    match = re.search(r"def slack_say\(.*?(?=\n@slack_app|\n@app|\Z)", text, re.DOTALL)
    assert match, "slack_say not found"
    body = match.group(0)
    assert "SlackNotifier" in body
    assert "WebClient" not in body, "the command must not build its own Slack client"
