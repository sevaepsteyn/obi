"""Contract (feature 124, US1): `obi radio call` refuses safely and never keys directly.

The test_cli_radio_say shape: behavioural refusals (each distinguishable, nothing
enqueued), the happy path reading back the settled outcome, and the structural guards —
the command never contends for the station lock and never builds a transmitter (the
daemon's exclusive flock is the stuck-PTT safeguard).

Nothing here keys a radio or opens a serial port.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from obi.core import radiostore

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


def _requests(tmp_path: Path) -> list[radiostore.CallRequest]:
    db = tmp_path / "radio.db"
    if not db.exists():
        return []
    conn = radiostore.connect_readonly(db)
    try:
        rows = conn.execute("SELECT id FROM call_requests").fetchall()
        return [r for r in (radiostore.get_call_request(conn, int(x[0])) for x in rows) if r]
    finally:
        conn.close()


def _station_stopped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from obi.core import service as svc

    monkeypatch.setattr(
        svc,
        "service_state",
        lambda spec: svc.LaunchdState(installed=True, loaded=False, pid=None, last_exit=None),
    )


def _station_running(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from obi.core import service as svc

    monkeypatch.setattr(
        svc,
        "service_state",
        lambda spec: svc.LaunchdState(installed=True, loaded=True, pid=4242, last_exit=None),
    )


# -- behavioural refusals ------------------------------------------------------------------


def test_empty_callee_is_refused_before_any_insert(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _station_running(monkeypatch)
    result = runner.invoke(
        _app(), ["radio", "call", "  ", "hello", "--scope", str(_config(tmp_path))]
    )
    assert result.exit_code != 0
    assert "empty" in result.output.lower()
    assert _requests(tmp_path) == []


def test_empty_message_is_refused_before_any_insert(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _station_running(monkeypatch)
    result = runner.invoke(
        _app(), ["radio", "call", "Dana", "   ", "--scope", str(_config(tmp_path))]
    )
    assert result.exit_code != 0
    assert "empty" in result.output.lower()
    assert _requests(tmp_path) == []


def test_over_length_message_is_refused_with_guidance(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec edge case: obi never truncates someone's message on the air — refuse instead."""

    _station_running(monkeypatch)
    config = _config(tmp_path, "outbound_max_chars = 40\n")
    result = runner.invoke(_app(), ["radio", "call", "Dana", "x" * 41, "--scope", str(config)])
    assert result.exit_code != 0
    assert "too long" in result.output.lower()
    assert _requests(tmp_path) == []


def test_station_not_running_is_refused_and_says_so(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _station_stopped(monkeypatch)
    result = runner.invoke(
        _app(), ["radio", "call", "Dana", "hello", "--scope", str(_config(tmp_path))]
    )
    assert result.exit_code != 0
    assert "not running" in result.output.lower()
    assert _requests(tmp_path) == []


# -- the happy path ------------------------------------------------------------------------


def test_enqueues_framed_text_and_reports_delivered(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The on-air text is fixed and SHOWN at submission (contract O3), then the
    station's outcome is read back."""

    _station_running(monkeypatch)
    config = _config(tmp_path)
    import obi.cli.main as cli

    real_sleep = cli._time.sleep  # noqa: SLF001

    def settle_then_sleep(seconds: float) -> None:
        conn = radiostore.connect(tmp_path / "radio.db")
        try:
            for row in radiostore.pending_call_requests(conn):
                radiostore.claim_call_request(conn, row.id, now_us=1)
                radiostore.settle_call_request(
                    conn,
                    row.id,
                    settled_us=2,
                    outcome="delivered",
                    attempts=0,
                    detail="go-ahead 'go for dana'",
                    call_id=7,
                )
        finally:
            conn.close()
        real_sleep(0)

    monkeypatch.setattr(cli._time, "sleep", settle_then_sleep)  # noqa: SLF001
    result = runner.invoke(
        _app(), ["radio", "call", "Dana", "the link is up", "--scope", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert "Dana, Dana, obi" in result.output  # the hail, shown before anything airs
    assert "the link is up, over" in result.output  # the framed on-air text
    assert "delivered" in result.output
    rows = _requests(tmp_path)
    assert len(rows) == 1
    assert rows[0].callee == "Dana"
    assert rows[0].message == "the link is up, over"  # framed at submission
    assert rows[0].origin == "cli"


def test_an_unanswered_call_exits_non_zero_with_the_reason(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _station_running(monkeypatch)
    config = _config(tmp_path)
    import obi.cli.main as cli

    real_sleep = cli._time.sleep  # noqa: SLF001

    def settle_unanswered(seconds: float) -> None:
        conn = radiostore.connect(tmp_path / "radio.db")
        try:
            for row in radiostore.pending_call_requests(conn):
                radiostore.claim_call_request(conn, row.id, now_us=1)
                radiostore.settle_call_request(
                    conn,
                    row.id,
                    settled_us=2,
                    outcome="unanswered",
                    attempts=3,
                    detail="no go-ahead after 3 hails",
                    call_id=7,
                )
        finally:
            conn.close()
        real_sleep(0)

    monkeypatch.setattr(cli._time, "sleep", settle_unanswered)  # noqa: SLF001
    result = runner.invoke(_app(), ["radio", "call", "Dana", "hello", "--scope", str(config)])
    assert result.exit_code != 0
    assert "unanswered" in result.output
    assert "3 hails" in result.output
    assert "never aired" in result.output


# -- structural guards ---------------------------------------------------------------------


def _call_source() -> str:
    text = (SRC / "cli" / "main.py").read_text(encoding="utf-8")
    match = re.search(r"def radio_call\(.*?(?=\n@radio_app|\n@app|\Z)", text, re.DOTALL)
    assert match, "radio_call not found in cli/main.py"
    return match.group(0)


def test_call_never_acquires_the_station_lock() -> None:
    assert "acquire_station_lock" not in _call_source()


def test_call_never_builds_a_transmitter() -> None:
    body = _call_source()
    assert "TxExecutor" not in body
    assert "PttController" not in body
    assert "SounddevicePlayer" not in body
