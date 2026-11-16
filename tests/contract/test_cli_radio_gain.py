"""Contract (feature 092, US4): `obi radio gain` reports drift and never fixes it silently.

The exit code carries the verdict so this is usable from a shell check, and the structural
guard enforces H5 — the ONLY code in obi that writes a host gain is the restore command.
An automatic restore would be an invisible action on state obi did not create.

No test calls CoreAudio; the backend is injected.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

runner = CliRunner()
SRC = Path(__file__).resolve().parents[2] / "src" / "obi"


def _app() -> object:
    from obi.cli.main import app

    return app


def _config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
        f'[radio]\nftdi_serial = "FX00TEST"\ndb_path = "{tmp_path}/radio.db"\n'
        f'clips_dir = "{tmp_path}/clips"\n' + body
    )
    path.chmod(0o600)
    return path


def _patch_gain(monkeypatch, report) -> None:  # type: ignore[no-untyped-def]
    from obi.radio import hostgain

    monkeypatch.setattr(hostgain, "read", lambda *a, **k: report)
    monkeypatch.setattr(hostgain, "restore", lambda *a, **k: report)


def _report(verdict: str, *, detail: str | None = None):  # type: ignore[no-untyped-def]
    from obi.radio.hostgain import Control, GainReport, ScopeReport

    drifted = verdict == "drifted"
    return GainReport(
        device="USB PnP Sound Device",
        input=ScopeReport(
            "input",
            [Control(0, 0.504 if drifted else 0.0)],
            0.0,
            verdict,
            "hotter than the calibrated floor — clips reference speech" if drifted else "",
        ),
        output=ScopeReport("output", [Control(1, 0.901), Control(2, 0.901)], 0.90, "calibrated"),
        verdict=verdict,
        detail=detail,
    )


def test_calibrated_reports_and_exits_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_gain(monkeypatch, _report("calibrated"))
    result = runner.invoke(_app(), ["radio", "gain", "--scope", str(_config(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "calibrated" in result.output
    assert "0.901" in result.output
    # H7 — the knob obi cannot see is surfaced as an operator checklist line.
    assert "1/10" in result.output


def test_drift_exits_non_zero_and_names_the_consequence(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _patch_gain(monkeypatch, _report("drifted"))
    result = runner.invoke(_app(), ["radio", "gain", "--scope", str(_config(tmp_path))])
    assert result.exit_code != 0
    assert "DRIFTED" in result.output.upper()
    assert "clips reference speech" in result.output
    assert "restore" in result.output  # tells the operator the fix


def test_unknown_is_not_reported_as_a_pass(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """H1 — a failed read must never read as 'calibrated'."""

    _patch_gain(monkeypatch, _report("unknown", detail="audio device not found"))
    result = runner.invoke(_app(), ["radio", "gain", "--scope", str(_config(tmp_path))])
    assert result.exit_code != 0
    assert "unknown" in result.output.lower()
    assert "not found" in result.output


def test_restore_is_opt_in(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """H5 — a plain `obi radio gain` reports; it must not write."""

    from obi.radio import hostgain

    wrote: list[int] = []
    monkeypatch.setattr(hostgain, "read", lambda *a, **k: _report("drifted"))
    monkeypatch.setattr(
        hostgain, "restore", lambda *a, **k: (wrote.append(1), _report("calibrated"))[1]
    )
    runner.invoke(_app(), ["radio", "gain", "--scope", str(_config(tmp_path))])
    assert wrote == []
    runner.invoke(_app(), ["radio", "gain", "--restore", "--scope", str(_config(tmp_path))])
    assert wrote == [1]


# -- structural guard (H5) ------------------------------------------------------------------


def test_only_the_restore_command_writes_a_host_gain() -> None:
    """Nothing may set a host gain as a side effect of anything else."""

    offenders = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if re.search(r"\bhostgain\.restore\(|\bwrite_scope\(", path.read_text(encoding="utf-8"))
        and str(path.relative_to(SRC)) not in ("cli/main.py", "radio/hostgain.py")
    }
    assert offenders == set(), f"host gain written outside the restore command: {offenders}"


def test_the_station_runner_never_restores() -> None:
    """H5 — start-up checks REPORT drift; they do not correct it."""

    runner_src = (SRC / "radio" / "runner.py").read_text(encoding="utf-8")
    assert "restore(" not in runner_src
