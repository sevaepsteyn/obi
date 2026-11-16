"""Integration (feature 092, US2): `obi radio say` through the live station's real gate.

The CLI cannot key the radio — the station holds an exclusive lock on the serial port, on
purpose. So a test transmission is queued in the store and the running station drains it.
These tests drive ``RadioRunner.drain_tx_requests()`` directly (the single-pass seam) so no
thread or wall-clock is involved, and assert the operator path gets NO exemptions from the
transmit gate: clear-channel, rate backstops, duration cap, stage gate (contract
tx-request.md Q1–Q9).

Nothing here keys a radio, opens a serial port, or emits audio — FakePtt/FakePlayer only.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np

from obi.core import radiostore
from obi.core.config import RadioSettings
from obi.radio.capture import LevelMonitor
from obi.radio.runner import RadioRunner
from obi.radio.tx import TxExecutor
from tests.fakes import FakeTranscriber

S = 1_000_000


def _wav_ms(duration_ms: int, *, rate: int = 22_050) -> bytes:
    frames = int(rate * duration_ms / 1000)
    pcm = (np.full(frames, 0.9) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


class Clock:
    def __init__(self) -> None:
        self.t_us = 0

    def now_us(self) -> int:
        return self.t_us

    def sleep(self, seconds: float) -> None:
        self.t_us += int(seconds * 1_000_000)


@dataclass
class FakePtt:
    keyed: bool = False
    key_calls: int = 0

    def key(self) -> None:
        self.keyed = True
        self.key_calls += 1

    def unkey(self) -> None:
        self.keyed = False


@dataclass
class FakePlayer:
    played: list[bytes] = field(default_factory=list)

    def preflight(self) -> None:
        pass

    def play(self, wav_bytes: bytes) -> None:
        self.played.append(wav_bytes)

    def stop(self) -> None:
        pass


class AlwaysClear:
    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return True


class NeverClear:
    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return False


@dataclass
class Harness:
    runner: RadioRunner
    clock: Clock
    ptt: FakePtt
    player: FakePlayer
    settings: RadioSettings

    def enqueue(self, text: str, *, ttl_s: int = 120) -> int:
        conn = radiostore.connect(self.settings.db_path)
        try:
            return radiostore.enqueue_tx_request(
                conn, created_us=self.clock.now_us(), text=text, ttl_s=ttl_s, tz=UTC
            )
        finally:
            conn.close()

    def request(self, request_id: int) -> radiostore.TxRequest:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            row = radiostore.get_tx_request(conn, request_id)
            assert row is not None
            return row
        finally:
            conn.close()

    def outbound(self) -> list[tuple[str, str, int | None]]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            rows = conn.execute(
                "SELECT kind, disposition, call_id FROM outbound ORDER BY id"
            ).fetchall()
            return [(str(r[0]), str(r[1]), r[2]) for r in rows]
        finally:
            conn.close()

    def transmissions(self) -> list[radiostore.Transmission]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            return radiostore.query_window(conn, limit=100, include_self=True)
        finally:
            conn.close()


def _harness(
    tmp_path: Path,
    *,
    tx_enabled: bool = True,
    monitor: Any = None,
    render_ms: int = 900,
    tx_cap_s: int = 15,
    max_tx_per_minute: int = 6,
    with_tx: bool = True,
) -> Harness:
    settings = RadioSettings(
        tx_enabled=tx_enabled,
        tx_gain=1.0,
        tx_cap_s=tx_cap_s,
        max_tx_per_minute=max_tx_per_minute,
        patience_s=5,
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
    )
    clock = Clock()
    ptt = FakePtt()
    player = FakePlayer()
    runner = RadioRunner(
        settings=settings,
        source_factory=lambda: None,
        transcriber=FakeTranscriber(results=[]),
        sleep=clock.sleep,
        now_us=clock.now_us,
        monitor=LevelMonitor(),
        spawn=lambda target, *args: target(*args),
    )
    if with_tx:
        executor = TxExecutor(
            settings=settings,
            monitor=monitor if monitor is not None else AlwaysClear(),
            ptt=ptt,
            renderer=lambda text: _wav_ms(render_ms),
            player=player,
            now_us=clock.now_us,
            sleep=clock.sleep,
        )
        runner._tx = executor  # the enable_calls seam without a call machine
    return Harness(runner=runner, clock=clock, ptt=ptt, player=player, settings=settings)


def test_operator_request_transmits_through_the_real_gate(tmp_path: Path) -> None:
    """Q3/Q9 — same executor, same store write, recorded as `operator` with no call."""

    h = _harness(tmp_path)
    req_id = h.enqueue("radio check, this is obi, over")

    assert h.runner.drain_tx_requests() == 1

    row = h.request(req_id)
    assert row.state == "done"
    assert row.disposition == "transmitted"
    assert row.outbound_id is not None
    assert h.ptt.key_calls == 1
    assert len(h.player.played) == 1
    # Filed as a human's words, never as an `answer` the model composed (Q1).
    assert h.outbound() == [("operator", "transmitted", None)]
    obi_rows = [r for r in h.transmissions() if r.kind == "obi"]
    assert len(obi_rows) == 1
    assert obi_rows[0].transcript == "radio check, this is obi, over"
    assert obi_rows[0].call_id is None


def test_operator_request_retains_a_clip(tmp_path: Path) -> None:
    """Q9 — the channel record includes obi's own audio, operator transmissions included."""

    h = _harness(tmp_path)
    h.enqueue("hello")
    h.runner.drain_tx_requests()
    clips = list((tmp_path / "clips").rglob("*.wav"))
    assert len(clips) == 1


def test_tx_disabled_never_keys(tmp_path: Path) -> None:
    """Q3 — the stage gate applies to operator transmissions exactly as to answers."""

    h = _harness(tmp_path, tx_enabled=False)
    req_id = h.enqueue("should not air")

    h.runner.drain_tx_requests()

    assert h.ptt.key_calls == 0
    assert h.player.played == []
    row = h.request(req_id)
    assert row.state == "done"
    assert row.disposition == "shadow_logged"
    assert row.detail is not None and "shadow" in row.detail


def test_busy_channel_drops_at_the_deadline(tmp_path: Path) -> None:
    """Q3 — listen-before-transmit, bounded by patience, then reported (Q8)."""

    h = _harness(tmp_path, monitor=NeverClear())
    req_id = h.enqueue("nobody can hear this")

    h.runner.drain_tx_requests()

    assert h.ptt.key_calls == 0
    row = h.request(req_id)
    assert row.disposition == "dropped_busy"
    assert row.detail is not None and "never cleared" in row.detail


def test_rate_backstop_suppresses_and_says_so(tmp_path: Path) -> None:
    """Q3 — a test burst spends the same budget as real traffic, and the operator sees it."""

    h = _harness(tmp_path, max_tx_per_minute=2)
    for _ in range(3):
        h.enqueue("burst")
    dispositions = []
    for _ in range(3):
        h.runner.drain_tx_requests()
    conn = radiostore.connect_readonly(h.settings.db_path)
    try:
        dispositions = [
            str(r[0]) for r in conn.execute("SELECT disposition FROM tx_requests ORDER BY id")
        ]
    finally:
        conn.close()
    assert dispositions[:2] == ["transmitted", "transmitted"]
    assert dispositions[2] == "suppressed_rate"
    assert h.ptt.key_calls == 2


def test_over_cap_text_is_truncated_by_the_existing_rule(tmp_path: Path) -> None:
    """Q3 — the deterministic pre-flight cap, no LLM, applies to operator text too."""

    h = _harness(tmp_path, render_ms=30_000, tx_cap_s=15)
    req_id = h.enqueue("First sentence here. Second sentence here. Third sentence here.")

    h.runner.drain_tx_requests()

    row = h.request(req_id)
    # The renderer is fixed-length in this harness, so the loop exhausts its passes and
    # still transmits — what matters is that the cap path ran and the text was shortened.
    conn = radiostore.connect_readonly(h.settings.db_path)
    try:
        text = conn.execute("SELECT text FROM outbound ORDER BY id").fetchone()[0]
    finally:
        conn.close()
    assert len(text) < len("First sentence here. Second sentence here. Third sentence here.")
    assert row.state == "done"


def test_expired_request_is_never_handed_to_the_transmitter(tmp_path: Path) -> None:
    """Q5 — a request that sat through a daemon outage must not air late."""

    h = _harness(tmp_path)
    req_id = h.enqueue("stale", ttl_s=10)
    h.clock.t_us += 11 * S

    h.runner.drain_tx_requests()

    assert h.ptt.key_calls == 0
    assert h.player.played == []
    row = h.request(req_id)
    assert row.state == "expired"
    assert h.outbound() == []


def test_a_request_is_attempted_at_most_once(tmp_path: Path) -> None:
    """Q4 — a settled request is never re-claimed and re-aired."""

    h = _harness(tmp_path)
    h.enqueue("only once")
    h.runner.drain_tx_requests()
    h.runner.drain_tx_requests()
    h.runner.drain_tx_requests()
    assert h.ptt.key_calls == 1
    assert len(h.outbound()) == 1


def test_rx_only_station_refuses_without_keying(tmp_path: Path) -> None:
    """Split-brain: RX works without the PTT half, so say must refuse, not crash."""

    h = _harness(tmp_path, with_tx=False)
    req_id = h.enqueue("no ptt half here")

    h.runner.drain_tx_requests()

    row = h.request(req_id)
    assert row.state == "done"
    assert row.disposition == "blocked_tx_off"
    assert row.detail is not None and "RX-only" in row.detail


def test_drain_is_idle_and_cheap_with_an_empty_queue(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    assert h.runner.drain_tx_requests() == 0
    assert h.ptt.key_calls == 0


def test_one_transmission_per_pass(tmp_path: Path) -> None:
    """A backlog drains one at a time — never a burst that spends the budget unseen."""

    h = _harness(tmp_path)
    h.enqueue("one")
    h.enqueue("two")
    h.runner.drain_tx_requests()
    assert h.ptt.key_calls == 1
    h.runner.drain_tx_requests()
    assert h.ptt.key_calls == 2
