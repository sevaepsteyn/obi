"""Integration: superseded commands fail loudly, naming their replacement (033 FR-008/SC-006).

The fleet precedent (`obi snmp` → `obi metrics live`, `[mikrotik]` → `[routeros]`) is a hard
rename; 033 goes one step further — every superseded form MUST tell the operator the exact
replacement instead of a bare "no such command". No side effects, no audit records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app

runner = CliRunner()


@pytest.mark.parametrize(
    ("argv", "replacement"),
    [
        (["ar"], "obi web"),
        (["ar", "export"], "obi web export"),
        (["ar", "serve"], "obi web serve"),
        (["ar", "serve", "--voice", "--port", "9443"], "obi web serve"),
        (["ar", "bogus-subcommand"], "obi web"),
        (["voice", "web"], "obi web serve"),
        (["voice", "web", "--port", "8444"], "obi web serve"),
    ],
)
def test_superseded_forms_exit_2_naming_the_replacement(
    argv: list[str], replacement: str
) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 2, result.output
    flat = result.output.replace("\n", "")
    assert replacement in flat, f"{argv}: expected {replacement!r} in output:\n{result.output}"
    assert "033" in flat or "feature 033" in flat  # the rename is attributable


def test_superseded_forms_leave_no_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    audit = tmp_path / "state" / "obi" / "audit.jsonl"
    runner.invoke(app, ["ar", "serve"])
    runner.invoke(app, ["voice", "web"])
    if audit.exists():
        records = [json.loads(line) for line in audit.read_text().splitlines() if line]
        assert records == []


def test_voice_talk_is_untouched() -> None:
    """023's terminal push-to-talk is NOT superseded (spec: `obi voice talk` untouched)."""

    result = runner.invoke(app, ["voice", "talk", "--help"])
    assert result.exit_code == 0
    assert "push-to-talk" in result.output


def test_stubs_are_hidden_from_help() -> None:
    """The stubs exist to guide, not to advertise: top-level --help lists web, not ar."""

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if "ar " in line.split("│")[0]]
    assert not [line for line in lines if line.strip().startswith("│ ar")]
