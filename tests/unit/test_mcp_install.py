"""T009 (US3): `obi mcp install` — surgical, idempotent registration of both clients.

Everything runs against a temp HOME and a recorded stub for the `claude` CLI: the Desktop
half must touch ONLY `mcpServers.obi` (foreign entries survive, malformed JSON aborts
untouched, re-runs are byte-identical no-ops), and the Claude Code half must converge through
`claude mcp get/add/remove --scope user` without obi ever parsing `~/.claude.json` itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from obi.mcp import install as mcp_install

OBI = Path("/fake/venv/bin/obi")


def _desktop_path(home: Path) -> Path:
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


class StubRunner:
    """A recorded `claude` CLI: maps argv[1:3] ("mcp get" etc.) to canned results."""

    def __init__(
        self,
        *,
        get_returncode: int = 1,
        get_stdout: str = "",
        missing: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.get_returncode = get_returncode
        self.get_stdout = get_stdout
        self.missing = missing

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if self.missing:
            raise FileNotFoundError("claude")
        self.calls.append(argv)
        if argv[:3] == ["claude", "mcp", "get"]:
            return subprocess.CompletedProcess(argv, self.get_returncode, self.get_stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


# --- Claude Desktop ---------------------------------------------------------------------------


def test_desktop_fresh_file_created_with_parents(tmp_path: Path) -> None:
    outcome = mcp_install.desktop_install(OBI, home=tmp_path, dry_run=False)
    assert outcome.outcome == "created"
    data = json.loads(_desktop_path(tmp_path).read_text())
    assert data["mcpServers"]["obi"] == {"command": str(OBI), "args": ["mcp", "serve"]}


def test_desktop_preserves_foreign_entries_and_keys(tmp_path: Path) -> None:
    path = _desktop_path(tmp_path)
    path.parent.mkdir(parents=True)
    existing = {
        "globalShortcut": "Cmd+Space",
        "mcpServers": {"other-server": {"command": "/usr/local/bin/other", "args": ["--x"]}},
    }
    path.write_text(json.dumps(existing))

    outcome = mcp_install.desktop_install(OBI, home=tmp_path, dry_run=False)

    assert outcome.outcome == "created"
    data = json.loads(path.read_text())
    assert data["globalShortcut"] == "Cmd+Space"
    assert data["mcpServers"]["other-server"] == existing["mcpServers"]["other-server"]
    assert data["mcpServers"]["obi"]["command"] == str(OBI)


def test_desktop_rerun_is_a_byte_identical_noop(tmp_path: Path) -> None:
    mcp_install.desktop_install(OBI, home=tmp_path, dry_run=False)
    before = _desktop_path(tmp_path).read_bytes()

    outcome = mcp_install.desktop_install(OBI, home=tmp_path, dry_run=False)

    assert outcome.outcome == "already registered"
    assert _desktop_path(tmp_path).read_bytes() == before


def test_desktop_changed_path_reports_updated(tmp_path: Path) -> None:
    mcp_install.desktop_install(OBI, home=tmp_path, dry_run=False)

    outcome = mcp_install.desktop_install(Path("/moved/bin/obi"), home=tmp_path, dry_run=False)

    assert outcome.outcome == "updated"
    data = json.loads(_desktop_path(tmp_path).read_text())
    assert data["mcpServers"]["obi"]["command"] == "/moved/bin/obi"


def test_desktop_malformed_json_aborts_untouched(tmp_path: Path) -> None:
    path = _desktop_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    outcome = mcp_install.desktop_install(OBI, home=tmp_path, dry_run=False)

    assert outcome.outcome == "error"
    assert "not valid JSON" in outcome.detail
    assert path.read_text() == "{not json"


def test_desktop_dry_run_writes_nothing(tmp_path: Path) -> None:
    outcome = mcp_install.desktop_install(OBI, home=tmp_path, dry_run=True)
    assert outcome.outcome == "created"
    assert "would" in outcome.detail
    assert not _desktop_path(tmp_path).exists()


# --- Claude Code (via the `claude` CLI) -------------------------------------------------------


def test_code_absent_registers_user_scope_stdio(tmp_path: Path) -> None:
    runner = StubRunner(get_returncode=1)
    outcome = mcp_install.code_install(OBI, runner=runner, dry_run=False)

    assert outcome.outcome == "created"
    assert runner.calls[0][:4] == ["claude", "mcp", "get", "obi"]
    add = runner.calls[1]
    assert add[:4] == ["claude", "mcp", "add", "--scope"]
    assert "user" in add and "--transport" in add and "stdio" in add
    assert add[-3:] == [str(OBI), "mcp", "serve"]
    assert "--" in add  # the server command is passed verbatim after the separator


def test_code_identical_registration_is_a_noop(tmp_path: Path) -> None:
    runner = StubRunner(get_returncode=0, get_stdout=f"obi: {OBI} mcp serve (stdio, user)")
    outcome = mcp_install.code_install(OBI, runner=runner, dry_run=False)

    assert outcome.outcome == "already registered"
    assert [c[:3] for c in runner.calls] == [["claude", "mcp", "get"]]  # no mutating call


def test_code_different_registration_converges_via_remove_add(tmp_path: Path) -> None:
    runner = StubRunner(get_returncode=0, get_stdout="obi: /old/bin/obi mcp serve (stdio, user)")
    outcome = mcp_install.code_install(OBI, runner=runner, dry_run=False)

    assert outcome.outcome == "updated"
    verbs = [c[2] for c in runner.calls]
    assert verbs == ["get", "remove", "add"]


def test_code_missing_cli_is_skipped_with_manual_command(tmp_path: Path) -> None:
    runner = StubRunner(missing=True)
    outcome = mcp_install.code_install(OBI, runner=runner, dry_run=False)

    assert outcome.outcome == "skipped"
    assert "claude mcp add" in outcome.detail and str(OBI) in outcome.detail


def test_code_dry_run_never_mutates(tmp_path: Path) -> None:
    runner = StubRunner(get_returncode=1)
    outcome = mcp_install.code_install(OBI, runner=runner, dry_run=True)

    assert outcome.outcome == "created"
    assert [c[2] for c in runner.calls] == ["get"]  # read-only probe only


# --- composition + path resolution ------------------------------------------------------------


def test_install_reports_both_targets(tmp_path: Path) -> None:
    runner = StubRunner(get_returncode=1)
    outcomes = mcp_install.install(
        dry_run=False, home=tmp_path, runner=runner, obi_path=OBI
    )
    assert [o.target for o in outcomes] == ["claude desktop", "claude code"]
    assert all(o.outcome == "created" for o in outcomes)


def test_resolve_obi_path_falls_back_to_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_install.sys, "executable", "/nonexistent/python")
    monkeypatch.setattr(mcp_install.shutil, "which", lambda _name: "/from/path/obi")
    assert mcp_install.resolve_obi_path() == Path("/from/path/obi")


def test_resolve_obi_path_error_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_install.sys, "executable", "/nonexistent/python")
    monkeypatch.setattr(mcp_install.shutil, "which", lambda _name: None)
    with pytest.raises(OSError, match="obi executable"):
        mcp_install.resolve_obi_path()
