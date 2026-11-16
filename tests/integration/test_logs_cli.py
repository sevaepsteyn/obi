"""Integration (feature 013): `obi logs ingest` and `obi logs query` end to end via the CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app

runner = CliRunner()

_LINE = (
    "Jun  3 10:53:51.207065 chestnut-gw dhcp,info dhcp-brst-bmorg assigned 100.90.13.55 "
    "for AA:99:1F:66:4E:DD Pixel-8-Pro\n"
)
_ERR = "Jun  3 11:00:02.000000 chestnut-gw system,error login failure for admin\n"


def _setup(tmp_path: Path) -> Path:
    mirror = tmp_path / "logs"
    day = mirror / "core-vms-1" / "2026" / "06" / "03"
    day.mkdir(parents=True)
    (day / "chestnut-gw.log").write_text(_LINE + _ERR)

    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'mirror_root = "{mirror}"\n'
        f'db_path = "{tmp_path / "logs.db"}"\n'
        f'[audit]\npath = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)  # avoid the insecure-perms warning polluting captured output
    return config


def test_ingest_then_query_table(tmp_path: Path) -> None:
    config = _setup(tmp_path)

    ingest = runner.invoke(app, ["logs", "ingest", "--scope", str(config)])
    assert ingest.exit_code == 0, ingest.output
    # Rich may wrap the status line at the terminal width, so collapse whitespace before matching.
    assert "2 new" in " ".join(ingest.output.split())

    query = runner.invoke(app, ["logs", "query", "--scope", str(config), "--severity", "error"])
    assert query.exit_code == 0, query.output
    # The Message column may wrap at the terminal width, so assert on the short, unwrapped columns.
    assert "chestnut-gw" in query.output
    assert "error" in query.output
    assert "1 line(s)" in query.output


def test_query_renders_message_with_brackets(tmp_path: Path) -> None:
    # Regression: a log line containing bracketed text (e.g. a file path or an audit detail) must
    # not be parsed as Rich markup — it previously crashed `obi logs query` with a MarkupError.
    mirror = tmp_path / "logs"
    day = mirror / "core-vms-1" / "2026" / "06" / "03"
    day.mkdir(parents=True)
    (day / "chestnut-gw.log").write_text(
        "Jun  3 11:05:00.000000 chestnut-gw script,info saved config to [/etc/ble-config.json] ok\n"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'mirror_root = "{mirror}"\n'
        f'db_path = "{tmp_path / "logs.db"}"\n'
        f'[audit]\npath = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)

    runner.invoke(app, ["logs", "ingest", "--scope", str(config)])
    query = runner.invoke(app, ["logs", "query", "--scope", str(config)])
    assert query.exit_code == 0, query.output  # no MarkupError (the regression)
    # The row rendered and was counted (the message may wrap in the narrow test terminal).
    assert "1 line(s)" in query.output


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    config = _setup(tmp_path)
    runner.invoke(app, ["logs", "ingest", "--scope", str(config)])
    second = runner.invoke(app, ["logs", "ingest", "--scope", str(config)])
    assert second.exit_code == 0, second.output
    assert "0 new" in " ".join(second.output.split())  # unchanged files re-ingest to nothing


def test_query_json(tmp_path: Path) -> None:
    config = _setup(tmp_path)
    runner.invoke(app, ["logs", "ingest", "--scope", str(config)])

    result = runner.invoke(app, ["logs", "query", "--scope", str(config), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["outcome"] == "reachable"
    assert payload["counts"]["total"] == 2
    messages = {e["message"] for e in payload["events"]}
    assert any("login failure" in m for m in messages)


def test_query_without_logs_section_errors(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[uisp]\nbase_url = "https://u"\ntoken = "t"\n')
    result = runner.invoke(app, ["logs", "query", "--scope", str(config)])
    assert result.exit_code == 2
    assert "logs not configured" in result.output
