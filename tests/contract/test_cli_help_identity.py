"""Feature 137 (T039): no `obi … --help` page names the real deployment.

Walks every command of the typer app, renders its help, and runs the identity guard over it —
with the developer's private inputs when present (tests/identity_render.py).
"""

from __future__ import annotations

import typer
from typer.main import get_command
from typer.testing import CliRunner

from obi.cli.main import app
from tests.identity_render import fatal_in


def _paths(group, prefix: tuple[str, ...] = ()):
    yield prefix
    for name, command in getattr(group, "commands", {}).items():
        yield from _paths(command, (*prefix, name))


def test_every_help_page_is_identity_free() -> None:
    root = get_command(app)
    runner = CliRunner()
    leaks: dict[str, list[str]] = {}
    pages = 0
    for path in _paths(root):
        result = runner.invoke(app, [*path, "--help"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, (path, result.output[:200])
        pages += 1
        hits = fatal_in(result.output, "obi " + " ".join(path))
        if hits:
            leaks[" ".join(path) or "<root>"] = hits
    assert pages > 50
    assert leaks == {}


def test_typer_is_the_cli_framework() -> None:  # guards the walker's assumption
    assert isinstance(app, typer.Typer)
