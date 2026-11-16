"""Contract (feature 092, US2): `obi radio say` refuses safely and never keys directly.

Two kinds of guard here. Behavioural: every refusal path exits without enqueuing anything,
and each reason is distinguishable (contract tx-request.md Q8). Structural: the command
does not acquire the station lock and does not build a TxExecutor — the station holds an
exclusive flock on the serial port on purpose, and routing around it is how a PTT gets
stuck (Q6).

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


def _requests(tmp_path: Path) -> list[radiostore.TxRequest]:
    db = tmp_path / "radio.db"
    if not db.exists():
        return []
    conn = radiostore.connect_readonly(db)
    try:
        rows = conn.execute("SELECT id FROM tx_requests").fetchall()
        return [r for r in (radiostore.get_tx_request(conn, int(x[0])) for x in rows) if r]
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


def test_empty_text_is_refused_before_any_insert(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _station_running(monkeypatch)
    result = runner.invoke(_app(), ["radio", "say", "   ", "--scope", str(_config(tmp_path))])
    assert result.exit_code != 0
    assert "empty" in result.output.lower()
    assert _requests(tmp_path) == []


def test_station_not_running_is_refused_and_says_so(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Q6 — the CLI never keys; without the station there is nothing to key through."""

    _station_stopped(monkeypatch)
    result = runner.invoke(_app(), ["radio", "say", "hello", "--scope", str(_config(tmp_path))])
    assert result.exit_code != 0
    assert "not running" in result.output.lower()
    assert _requests(tmp_path) == []


def test_missing_radio_section_exits_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n')
    path.chmod(0o600)
    result = runner.invoke(_app(), ["radio", "say", "hello", "--scope", str(path)])
    assert result.exit_code != 0


def test_enqueues_and_reports_the_settled_outcome(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The happy path: the row is queued, then the station's verdict is read back (Q8)."""

    _station_running(monkeypatch)
    config = _config(tmp_path)

    # Stand in for the station: settle the request as soon as it appears.
    import obi.cli.main as cli

    real_sleep = cli._time.sleep  # noqa: SLF001

    def settle_then_sleep(seconds: float) -> None:
        conn = radiostore.connect(tmp_path / "radio.db")
        try:
            for row in radiostore.pending_tx_requests(conn):
                radiostore.claim_tx_request(conn, row.id, now_us=1)
                radiostore.settle_tx_request(
                    conn,
                    row.id,
                    settled_us=2,
                    disposition="transmitted",
                    detail="keyed 1200 ms",
                    outbound_id=1,
                )
        finally:
            conn.close()
        real_sleep(0)

    monkeypatch.setattr(cli._time, "sleep", settle_then_sleep)  # noqa: SLF001
    result = runner.invoke(_app(), ["radio", "say", "radio check", "--scope", str(config)])
    assert result.exit_code == 0, result.output
    assert "transmitted" in result.output
    assert "keyed 1200 ms" in result.output
    rows = _requests(tmp_path)
    assert len(rows) == 1 and rows[0].text == "radio check"


def test_a_refused_transmission_exits_non_zero(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _station_running(monkeypatch)
    config = _config(tmp_path)
    import obi.cli.main as cli

    real_sleep = cli._time.sleep  # noqa: SLF001

    def settle_busy(seconds: float) -> None:
        conn = radiostore.connect(tmp_path / "radio.db")
        try:
            for row in radiostore.pending_tx_requests(conn):
                radiostore.claim_tx_request(conn, row.id, now_us=1)
                radiostore.settle_tx_request(
                    conn,
                    row.id,
                    settled_us=2,
                    disposition="dropped_busy",
                    detail="channel never cleared — waited 5000 ms, then gave up",
                    outbound_id=None,
                )
        finally:
            conn.close()
        real_sleep(0)

    monkeypatch.setattr(cli._time, "sleep", settle_busy)  # noqa: SLF001
    result = runner.invoke(_app(), ["radio", "say", "hello", "--scope", str(config)])
    assert result.exit_code != 0
    assert "dropped_busy" in result.output
    assert "never cleared" in result.output


# -- structural guards (Q6) ----------------------------------------------------------------


def _say_source() -> str:
    text = (SRC / "cli" / "main.py").read_text(encoding="utf-8")
    match = re.search(r"def radio_say\(.*?(?=\n@radio_app|\n@app|\Z)", text, re.DOTALL)
    assert match, "radio_say not found in cli/main.py"
    return match.group(0)


def test_say_never_acquires_the_station_lock() -> None:
    """The daemon's exclusive flock is a stuck-PTT safeguard — say must not contend for it."""

    assert "acquire_station_lock" not in _say_source()


def test_say_never_builds_a_transmitter() -> None:
    body = _say_source()
    assert "TxExecutor" not in body
    assert "PttController" not in body
    assert "SounddevicePlayer" not in body


def test_say_is_not_reachable_from_the_toolset() -> None:
    """Q2 — the model produces text; it cannot enqueue RF. Only the CLI writes the queue."""

    offenders = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "enqueue_tx_request" in path.read_text(encoding="utf-8")
        and str(path.relative_to(SRC)) not in ("cli/main.py", "core/radiostore.py")
    }
    assert offenders == set(), f"enqueue_tx_request reachable outside the CLI: {offenders}"
