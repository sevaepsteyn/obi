"""Integration (feature 088, US1): `obi radio` CLI data commands end to end via CliRunner.

Covers tail/search/devices/status against a tmp config + tmp store, the missing-section
exit, and the absent-store message. Hardware-touching paths (run/level) stay untested by
CI (FR-036); `devices` is exercised through a monkeypatched probe.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.core import radiostore
from obi.core.radiostore import TransmissionRecord
from obi.radio.devices import DevicesReport, HalfReport

runner = CliRunner()


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


def _seed(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    radiostore.record_transmission(
        conn,
        TransmissionRecord(
            started_us=1_000_000,
            ended_us=3_000_000,
            kind="rx",
            transcript="obi obi this is nono",
            stt_gate="ok",
            caller="nono",
        ),
        tz=UTC,
    )
    radiostore.record_transmission(
        conn,
        TransmissionRecord(
            started_us=5_000_000,
            ended_us=7_000_000,
            kind="rx",
            transcript="the mountain link is down",
            stt_gate="ok",
        ),
        tz=UTC,
    )
    conn.close()


def test_missing_radio_section_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n')
    path.chmod(0o600)
    result = runner.invoke(_app(), ["radio", "tail", "--scope", str(path)])  # type: ignore[arg-type]
    assert result.exit_code == 2
    assert "radio not configured" in result.output


def test_tail_shows_rows(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed(tmp_path)
    result = runner.invoke(_app(), ["radio", "tail", "--scope", str(config)])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "nono" in result.output
    assert "mountain link" in result.output


def test_tail_absent_store_is_a_hint_not_an_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = runner.invoke(_app(), ["radio", "tail", "--scope", str(config)])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "obi radio start" in result.output


def test_tail_bad_kind_exits_2(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = runner.invoke(
        _app(),
        ["radio", "tail", "--scope", str(config), "--kind", "bogus"],  # type: ignore[arg-type]
    )
    assert result.exit_code == 2


def test_search_finds_by_text(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed(tmp_path)
    # The stored rows are from 1970: a 1-day window excludes them (the bound applies)…
    result = runner.invoke(
        _app(),
        ["radio", "search", "mountain", "--scope", str(config), "--window", "1d"],  # type: ignore[arg-type]
    )
    assert result.exit_code == 0
    assert "no matches" in result.output
    # …and a huge window finds them.
    result = runner.invoke(
        _app(),
        ["radio", "search", "mountain", "--scope", str(config), "--window", "999999d"],  # type: ignore[arg-type]
    )
    assert result.exit_code == 0
    assert "mountain link" in result.output


def test_search_bad_window_exits_2(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = runner.invoke(
        _app(),
        ["radio", "search", "x", "--scope", str(config), "--window", "soon"],  # type: ignore[arg-type]
    )
    assert result.exit_code == 2


def test_devices_exit_codes_follow_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obi.radio import devices as radio_devices

    config = _config(tmp_path)

    def present(**_kw: object) -> DevicesReport:
        return DevicesReport(
            audio=HalfReport(True, "'USB PnP Sound Device' (input #2, output #2)"),
            serial=HalfReport(True, "/dev/cu.usbserial-FBC3793547DF"),
        )

    monkeypatch.setattr(radio_devices, "probe", present)
    result = runner.invoke(_app(), ["radio", "devices", "--scope", str(config)])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "present" in result.output

    def half(**_kw: object) -> DevicesReport:
        return DevicesReport(
            audio=HalfReport(True, "audio ok"),
            serial=HalfReport(False, "no serial node"),
        )

    monkeypatch.setattr(radio_devices, "probe", half)
    result = runner.invoke(_app(), ["radio", "devices", "--scope", str(config)])  # type: ignore[arg-type]
    assert result.exit_code == 1
    assert "absent" in result.output


def test_selftest_refuses_without_the_exact_phrase(tmp_path: Path) -> None:
    # The refusal path is the ONLY selftest path a test may exercise (FR-036): the port is
    # resolved strictly after the typed confirmation, so nothing hardware is ever touched.
    config = _config(tmp_path)
    result = runner.invoke(
        _app(),
        ["radio", "selftest", "--scope", str(config)],
        input="no thanks\n",  # type: ignore[arg-type]
    )
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()


def test_mishears_lists_unresolved_hail_handles(tmp_path: Path) -> None:
    # Field 2026-08-20: "Obi, Obi, sir/User-41/Hever" — unidentified hails become
    # paste-ready [radio.caller_aliases] suggestions, grouped by frequency.
    conn = radiostore.connect(tmp_path / "radio.db")
    for started, text in (
        (1_000_000, "Obi, Obi, sir."),
        (5_000_000, "Obi, Obi, User-41."),
        (9_000_000, "Obi, Obi, sir."),
        (13_000_000, "Obi, Obi, Seven."),  # resolves via the alias below — curated
    ):
        radiostore.record_transmission(
            conn,
            TransmissionRecord(
                started_us=started,
                ended_us=started + 2_000_000,
                kind="rx",
                transcript=text,
                stt_gate="ok",
                trigger_fired=True,
                caller=None,
            ),
            tz=UTC,
        )
    conn.close()
    config = _config(
        tmp_path,
        'operator_handles = ["Mira"]\n[radio.caller_aliases]\nseven = "Mira"\n',
    )
    result = runner.invoke(_app(), ["radio", "mishears", "--scope", str(config)])  # type: ignore[arg-type]
    assert result.exit_code == 0, result.output
    assert "sir" in result.output and "user 41" in result.output
    assert "seven" not in result.output  # already curated under current aliases
    assert 'sir = "?"' in result.output


def test_mishears_empty_store_is_a_hint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = runner.invoke(_app(), ["radio", "mishears", "--scope", str(config)])  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert "no radio store" in result.output
