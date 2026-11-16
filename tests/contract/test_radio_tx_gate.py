"""Contract (feature 088): structural guards for tx-gate G1 — one path to RF.

Grep-level assertions over the source tree: the ONLY call site of the PTT controller's
``key()`` is the TxExecutor, and the only raw line manipulation outside the controller is
the operator-run selftest command. Plus the provenance rule: the hardware-behavior modules
cite their criticalrf measurements. A failure here means someone added a new way to key
the transmitter — that is a spec conversation, not a code review nit.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "obi"


def _sources() -> dict[str, str]:
    return {
        str(path.relative_to(SRC)): path.read_text(encoding="utf-8")
        for path in SRC.rglob("*.py")
    }


def test_ptt_key_is_called_only_by_the_tx_executor() -> None:
    offenders = {
        name
        for name, text in _sources().items()
        if re.search(r"\.key\(\)", text) and name not in ("radio/tx.py", "radio/ptt.py")
    }
    assert offenders == set(), f"new .key() call sites outside the TX gate: {offenders}"
    # ptt.py may only DEFINE key(), never call it on itself.
    ptt = _sources()["radio/ptt.py"]
    assert not re.search(r"self\.key\(\)", ptt)


def test_raw_line_manipulation_is_confined() -> None:
    # Asserting RTS/DTR outside the controller is allowed in exactly one place: the
    # operator-run selftest (typed confirmation, never CI). Everything else must go
    # through PttController.
    allowed = {"radio/ptt.py", "cli/main.py"}
    offenders = {
        name
        for name, text in _sources().items()
        if re.search(r"(set_rts\(True\)|\.rts\s*=\s*True|set_dtr\(True\)|\.dtr\s*=\s*True)", text)
        and name not in allowed
    }
    assert offenders == set(), f"raw PTT line assertion outside ptt.py/selftest: {offenders}"


def test_transmit_is_called_only_by_the_runner_and_gate_tests() -> None:
    # The TxExecutor's transmit() is reachable from exactly one production call site: the
    # runner's TX worker (fed only by call-machine effects — no announcement path exists).
    sources = _sources()
    offenders = {
        name
        for name, text in sources.items()
        if re.search(r"\btx\.transmit\(|_tx\.transmit\(|executor\.transmit\(", text)
        and name not in ("radio/runner.py",)
    }
    assert offenders == set(), f"unexpected transmit() call sites: {offenders}"


def test_hardware_modules_cite_their_criticalrf_provenance() -> None:
    sources = _sources()
    for name in ("radio/__init__.py", "radio/ptt.py", "radio/capture.py"):
        assert "criticalrf" in sources[name], f"{name} lost its measurement provenance"
    # The two central measured facts stay stated where they bind behavior.
    assert "1-10 ms" in sources["radio/ptt.py"] or "1–10 ms" in sources["radio/ptt.py"]
    assert "-0.2 dBFS" in sources["radio/capture.py"] or "−0.2 dBFS" in sources["radio/capture.py"]


def test_cli_surface_matches_the_contract() -> None:
    # T041's drift guard: every command in specs/088-radio/contracts/cli.md exists.
    from typer.testing import CliRunner

    from obi.cli.main import app

    runner = CliRunner()
    for command in (
        "run", "start", "stop", "restart", "status", "logs",
        "tail", "search", "devices", "level", "selftest",
    ):
        result = runner.invoke(app, ["radio", command, "--help"])  # type: ignore[arg-type]
        assert result.exit_code == 0, f"obi radio {command} --help failed"
