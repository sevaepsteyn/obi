"""`obi identity parity` (T014): record + compare; diffs only on the terminal, counts audited."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from tests.fixtures.identity_env import fake_config  # noqa: F401 - pytest fixture


def run(*args: str):
    return CliRunner().invoke(app, ["identity", *args], catch_exceptions=False)


def test_record_then_compare_equal(fake_config: Path) -> None:  # noqa: F811
    assert run("parity", "--record", "a", "--config", str(fake_config)).exit_code == 0
    assert run("parity", "--record", "b", "--config", str(fake_config)).exit_code == 0
    result = run("parity", "--compare", "a", "b", "--config", str(fake_config))
    assert result.exit_code == 0, result.output
    assert "equal" in result.output and "different" not in result.output


def test_compare_reports_difference_and_exits_1(fake_config: Path, tmp_path: Path) -> None:  # noqa: F811
    from obi.core.identity import parity

    run("parity", "--record", "a", "--config", str(fake_config))
    path = parity.snapshot_path("a")
    data = json.loads(path.read_text())
    data["surfaces"]["settings"] += "\nextra = 1"
    parity.snapshot_path("b").write_text(json.dumps(data))
    result = run("parity", "--compare", "a", "b", "--config", str(fake_config))
    assert result.exit_code == 1
    assert "different  settings" in result.output


def test_requires_exactly_one_mode(fake_config: Path) -> None:  # noqa: F811
    assert run("parity", "--config", str(fake_config)).exit_code == 2
