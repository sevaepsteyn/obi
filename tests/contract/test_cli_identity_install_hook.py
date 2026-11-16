"""`obi identity install-hook` (T083): one install covers every worktree; foreign hooks chained."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from tests.fixtures.identity_env import tree  # noqa: F401 - pytest fixture


def run(cwd: Path, monkeypatch, *args: str):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(app, ["identity", "install-hook", *args])


def test_installs_once_for_all_worktrees(tree, monkeypatch, tmp_path: Path) -> None:  # noqa: F811
    root = tree({"a.txt": "x\n"})
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt)], check=True)
    result = run(wt, monkeypatch)
    assert result.exit_code == 0, result.output
    hook = root / ".git" / "hooks" / "pre-commit"
    assert hook.exists() and "obi.core.identity.hook" in hook.read_text()
    assert run(root, monkeypatch).exit_code == 0  # idempotent


def test_foreign_hook_needs_force_and_is_chained(tree, monkeypatch) -> None:  # noqa: F811
    root = tree({"a.txt": "x\n"})
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    assert run(root, monkeypatch).exit_code == 2
    assert run(root, monkeypatch, "--force").exit_code == 0
    assert (root / ".git" / "hooks" / "pre-commit.local").read_text().endswith("echo mine\n")
    assert "pre-commit.local" in hook.read_text()
