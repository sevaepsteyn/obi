"""Integration: the web surface stays additive — existing surfaces unchanged (022 T030, 033).

The full suite regression-guards existing behavior; these assertions pin the additive seams
specifically: the ``web`` group registers alongside every other command, and a config written
before the viewer existed still parses without demanding anything of the operator.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.config import WebSettings, load_config

runner = CliRunner()


def test_web_subapp_is_registered_alongside_existing_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("routeros", "status", "siklu", "web", "watch"):
        assert command in result.output


def test_web_help_names_both_commands() -> None:
    result = runner.invoke(app, ["web", "--help"])
    assert result.exit_code == 0
    assert "export" in result.output and "serve" in result.output


def test_pre_022_config_loads_unchanged_with_default_web(tmp_path: Path) -> None:
    """A config written before the viewer existed (no [web] section) parses as before."""

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "\n".join(
            [
                "[uisp]",
                'base_url = "https://uisp.example.com"',
                'token = "tok"',
            ]
        )
    )
    config_file.chmod(0o600)
    config = load_config(config_file)
    assert config.uisp is not None
    assert config.web == WebSettings()  # present, all-default, demanded nothing of the operator
