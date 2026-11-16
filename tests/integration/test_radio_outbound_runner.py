"""Integration (feature 124): outbound calls through the runner's real wiring.

Drives ``RadioRunner.drain_call_requests()`` (the single-pass seam) plus scripted
received transmissions through ``_handle_segment`` — so the go-ahead computation, the
call machine, the TxExecutor gate, and the request settle/requeue bookkeeping all run
exactly as in production, with fakes at the hardware edges only (FR-036). No thread,
no wall clock, nothing keyed for real.
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
from obi.radio.calls import CallMachine
from obi.radio.capture import Segment
from obi.radio.runner import RadioRunner
from obi.radio.tx import TxExecutor
from obi.voice.stt import TranscriptionResult

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
    key_calls: int = 0

    def key(self) -> None:
        self.key_calls += 1

    def unkey(self) -> None:
        pass


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

    def reset(self) -> None:
        pass


class NeverClear(AlwaysClear):
    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return False


@dataclass
class ScriptedTranscriber:
    queue: list[str] = field(default_factory=list)

    def transcribe(self, audio: Any) -> TranscriptionResult:
        text = self.queue.pop(0)
        return TranscriptionResult(text=text, gate="ok")


@dataclass
class FakeNotifier:
    notes: list[str] = field(default_factory=list)

    def deliver_status(self, text: str) -> None:
        self.notes.append(text)


# Broadband speech-shaped noise: passes classify_non_speech (voiced, not tonal).
_SPEECH = np.random.default_rng(2424).normal(0, 0.15, 16_000).astype(np.float32)


@dataclass
class Harness:
    runner: RadioRunner
    clock: Clock
    ptt: FakePtt
    player: FakePlayer
    settings: RadioSettings
    transcriber: ScriptedTranscriber
    notifier: FakeNotifier

    def enqueue(self, callee: str = "Dana", message: str = "the link is up, over",
                *, ttl_s: int = 600) -> int:
        conn = radiostore.connect(self.settings.db_path)
        try:
            return radiostore.enqueue_call_request(
                conn,
                created_us=self.clock.now_us(),
                callee=callee,
                message=message,
                ttl_s=ttl_s,
                tz=UTC,
            )
        finally:
            conn.close()

    def request(self, request_id: int) -> radiostore.CallRequest:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            row = radiostore.get_call_request(conn, request_id)
            assert row is not None
            return row
        finally:
            conn.close()

    def hear(self, text: str) -> None:
        """One received transmission through the full RX wiring."""

        self.transcriber.queue.append(text)
        started = self.clock.now_us()
        self.clock.t_us += 1 * S
        self.runner._handle_segment(
            Segment(
                started_us=started,
                ended_us=self.clock.now_us(),
                audio=_SPEECH,
                peak_dbfs=-8.0,
                rms_dbfs=-20.0,
                overlaps_self_tx=False,
            )
        )

    def tick(self) -> None:
        self.runner._dispatch(lambda machine: machine.tick(self.clock.now_us()))

    def outbound_rows(self) -> list[tuple[str, str]]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            rows = conn.execute("SELECT kind, disposition FROM outbound ORDER BY id").fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        finally:
            conn.close()

    def call_directions(self) -> list[str]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            return [
                str(r[0])
                for r in conn.execute("SELECT direction FROM calls ORDER BY id").fetchall()
            ]
        finally:
            conn.close()


def _harness(
    tmp_path: Path,
    *,
    tx_enabled: bool = True,
    monitor: Any = None,
    known_callers: list[str] | None = None,
) -> Harness:
    settings = RadioSettings(
        tx_enabled=tx_enabled,
        tx_gain=1.0,
        sample_rate=16_000,
        patience_s=5,
        outbound_ack_window_s=30,
        outbound_max_rehails=2,
        known_callers=known_callers or [],
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
    )
    clock = Clock()
    ptt = FakePtt()
    player = FakePlayer()
    transcriber = ScriptedTranscriber()
    notifier = FakeNotifier()
    runner = RadioRunner(
        settings=settings,
        source_factory=lambda: None,
        transcriber=transcriber,
        notifier=notifier,
        sleep=clock.sleep,
        now_us=clock.now_us,
        monitor=monitor if monitor is not None else AlwaysClear(),  # type: ignore[arg-type]
        spawn=lambda target, *args: target(*args),
    )
    executor = TxExecutor(
        settings=settings,
        monitor=monitor if monitor is not None else AlwaysClear(),
        ptt=ptt if tx_enabled else None,
        renderer=lambda text: _wav_ms(900),
        player=player,
        now_us=clock.now_us,
        sleep=clock.sleep,
    )
    machine = CallMachine(
        ack_text=settings.ack_text,
        say_again_text=settings.say_again_text,
        unavailable_text=settings.unavailable_text,
        call_timeout_s=settings.call_timeout_s,
        compose_deadline_s=settings.compose_deadline_s,
        operator_handles=list(settings.operator_handles),
        outbound_ack_window_s=settings.outbound_ack_window_s,
        outbound_max_rehails=settings.outbound_max_rehails,
    )
    runner.enable_calls(machine=machine, tx=executor, composer=None)
    return Harness(
        runner=runner, clock=clock, ptt=ptt, player=player,
        settings=settings, transcriber=transcriber, notifier=notifier,
    )


def test_outbound_flow_end_to_end(tmp_path: Path) -> None:
    """Enqueue → claim → hail → go-ahead → delivery → settled 'delivered' (US1)."""

    h = _harness(tmp_path)
    req_id = h.enqueue("Dana", "the link is up, over")

    assert h.runner.drain_call_requests() == 1
    assert h.outbound_rows() == [("hail", "transmitted")]
    assert h.call_directions() == ["outbound"]
    assert h.ptt.key_calls == 1

    h.hear("go for dana")

    assert h.outbound_rows() == [("hail", "transmitted"), ("delivery", "transmitted")]
    row = h.request(req_id)
    assert row.state == "done"
    assert row.outcome == "delivered"
    assert row.call_id is not None
    assert any("delivered to Dana" in n for n in h.notifier.notes)
    # The exchange continues as a normal open call; the callee clears it.
    h.hear("copy that, out")
    conn = radiostore.connect_readonly(h.settings.db_path)
    try:
        closure = conn.execute("SELECT closure FROM calls WHERE id = ?", (row.call_id,)).fetchone()
    finally:
        conn.close()
    assert closure[0] == "cleared"


def test_drain_refuses_while_a_call_is_open(tmp_path: Path) -> None:
    h = _harness(tmp_path, known_callers=["mira"])
    h.hear("obi obi this is mira")  # an inbound call opens
    req_id = h.enqueue()
    assert h.runner.drain_call_requests() == 0
    assert h.request(req_id).state == "pending"
    assert h.outbound_rows() == [("ack", "transmitted")]  # only the inbound ack aired


def test_expired_request_never_hails(tmp_path: Path) -> None:
    """O5 — a request that sat through a daemon outage settles 'expired', silently."""

    h = _harness(tmp_path)
    req_id = h.enqueue(ttl_s=10)
    h.clock.t_us += 11 * S
    h.runner.drain_call_requests()
    assert h.ptt.key_calls == 0
    row = h.request(req_id)
    assert row.state == "expired"
    assert row.outcome == "expired"


def test_unanswered_walk_hails_thrice_and_reports(tmp_path: Path) -> None:
    """US2 — exactly 3 hails, zero payload, one honest report."""

    h = _harness(tmp_path)
    req_id = h.enqueue()
    h.runner.drain_call_requests()
    for _ in range(3):
        h.clock.t_us += 31 * S
        h.tick()
    rows = h.outbound_rows()
    assert rows == [("hail", "transmitted")] * 3
    row = h.request(req_id)
    assert row.state == "done"
    assert row.outcome == "unanswered"
    assert row.attempts == 3
    assert row.detail is not None and "3 hails" in row.detail
    assert any("unanswered" in n and "never aired" in n for n in h.notifier.notes)


def test_inbound_supersede_requeues_and_the_retry_delivers(tmp_path: Path) -> None:
    """O7 — a human hailing obi outranks the attempt; the requeue delivers later."""

    h = _harness(tmp_path, known_callers=["mira"])
    req_id = h.enqueue("Dana")
    h.runner.drain_call_requests()
    assert h.outbound_rows() == [("hail", "transmitted")]

    h.hear("obi obi this is mira")  # inbound caller wins; obi acks them
    row = h.request(req_id)
    assert row.state == "pending"
    assert row.attempts == 0  # the supersede burned no retry
    assert ("ack", "transmitted") in h.outbound_rows()

    h.hear("out")  # mira clears; the station is idle again
    assert h.runner.drain_call_requests() == 1  # the request is re-claimed
    h.hear("dana, go for obi")
    assert ("delivery", "transmitted") in h.outbound_rows()
    assert h.request(req_id).outcome == "delivered"


def test_busy_channel_requeues_without_burning_a_retry(tmp_path: Path) -> None:
    h = _harness(tmp_path, monitor=NeverClear())
    req_id = h.enqueue()
    h.runner.drain_call_requests()
    assert h.ptt.key_calls == 0
    row = h.request(req_id)
    assert row.state == "pending"
    assert row.attempts == 0
    assert row.detail is not None and "busy" in row.detail


def test_reopen_gate_holds_during_an_outbound_attempt(tmp_path: Path) -> None:
    """122/D7 — no deaf window while obi must stay hearing."""

    h = _harness(tmp_path)
    assert h.runner._preventive_gate_clear(h.clock.now_us()) is True
    req_id = h.enqueue()
    # Pending request: the gate defers before anything is even claimed.
    assert h.runner._preventive_gate_clear(h.clock.now_us()) is False
    h.runner.drain_call_requests()
    # In-flight attempt (call open, awaiting the go-ahead): still deferred.
    assert h.runner._preventive_gate_clear(h.clock.now_us()) is False
    h.hear("go for dana")
    assert h.request(req_id).outcome == "delivered"


def test_shadow_stage_walks_the_whole_flow_without_keying(tmp_path: Path) -> None:
    h = _harness(tmp_path, tx_enabled=False)
    req_id = h.enqueue()
    h.runner.drain_call_requests()
    h.hear("go for dana")
    assert h.ptt.key_calls == 0
    assert h.player.played == []
    assert h.outbound_rows() == [("hail", "shadow_logged"), ("delivery", "shadow_logged")]
    assert h.request(req_id).outcome == "delivered"  # the rehearsal exercises the walk


def test_third_party_go_ahead_does_not_deliver(tmp_path: Path) -> None:
    """US3 — 'go for mike' answering someone else is not our go-ahead (O8)."""

    h = _harness(tmp_path, known_callers=["mike"])
    req_id = h.enqueue("Dana")
    h.runner.drain_call_requests()
    h.hear("go for mike")
    assert h.outbound_rows() == [("hail", "transmitted")]  # no delivery
    assert h.request(req_id).state == "claimed"  # still waiting inside the window
    h.hear("go for dana")
    assert ("delivery", "transmitted") in h.outbound_rows()
    assert h.request(req_id).outcome == "delivered"
