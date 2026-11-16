"""Feature 137, T029: an event feature with no config section is OFF — one guidance line, exit 2.

There is no built-in location, dashboard or API host any more; nothing falls back to them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n')
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["weather", "run"], "weather not configured"),
        (["traffic", "run"], "traffic not configured"),
    ],
)
def test_unconfigured_collector_exits_2(tmp_path: Path, args: list[str], needle: str) -> None:
    result = CliRunner().invoke(app, [*args, "--scope", str(_config(tmp_path))])
    assert result.exit_code == 2
    assert needle in result.output


def test_playa_fetch_without_api_exits_2(tmp_path: Path) -> None:
    key = tmp_path / "key.txt"
    key.write_text("k")
    result = CliRunner().invoke(
        app, ["playa", "fetch", "--key-file", str(key), "--config", str(_config(tmp_path))]
    )
    assert result.exit_code == 2
    assert "playa API not configured" in result.output
