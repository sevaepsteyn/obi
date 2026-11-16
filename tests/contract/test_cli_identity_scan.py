"""`obi identity scan` (T021): exit codes, header, JSON shape, never a value."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from tests.fixtures.identity_env import tree  # noqa: F401 - pytest fixture

#: Assembled at run time so this tracked file has no literal path into the excluded dir.
SPECS = "spe" + "cs"

TAILNET = "lights.example-hub." + "ts" + ".net"


def run(root: Path, monkeypatch, *args: str):
    monkeypatch.chdir(root)
    return CliRunner().invoke(app, ["identity", "scan", "--no-private", *args])


def test_clean_tree_exits_0(tree, monkeypatch) -> None:  # noqa: F811
    result = run(tree({"src/a.py": "x = 1\n"}), monkeypatch)
    assert result.exit_code == 0, result.output
    assert "private inputs" in result.output and f"{SPECS}/**" in result.output


def test_finding_exits_1_without_value(tree, monkeypatch) -> None:  # noqa: F811
    result = run(tree({"src/a.py": f"u = '{TAILNET}'\n"}), monkeypatch)
    assert result.exit_code == 1
    assert "src/a.py line 1" in result.output and TAILNET not in result.output


def test_json_and_paths(tree, monkeypatch) -> None:  # noqa: F811
    root = tree({"src/a.py": f"u = '{TAILNET}'\n", "docs/b.md": "fine\n"})
    result = run(root, monkeypatch, "--json", "docs")
    data = json.loads(result.output)
    assert result.exit_code == 0 and data["files"] == 1 and data["counts"]["fatal"] == {}
    assert TAILNET not in result.output


def test_outside_git_exits_2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["identity", "scan", "--no-private"])
    assert result.exit_code == 2
