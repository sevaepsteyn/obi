"""Integration (feature 133): the garble-triggered capture reopen through the runner.

Clones the 122 reopen harness (scripted source feeding the runner monitor + an injectable
clock) and adds the call flow the exchange harness uses: inline ``spawn``, a real
``TxExecutor`` over a fake PTT/player, a ``CallMachine`` and a stub composer. The TX
executor reads an always-clear monitor so the inline "say again" cannot spin on the
fresh (reset) capture monitor — R7's fresh-ears rule has its own 122 tests.

Contract references G1–G10: specs/133-radio-garble-reopen/contracts/garble-reopen.md.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from obi.core import auditstore, radiostore
from obi.core.audit import AuditRecorder
from obi.core.config import AuditSettings, RadioSettings
from obi.radio.calls import CallMachine
from obi.radio.capture import BLOCK_MS, dbfs
from obi.radio.devices import DevicesReport, HalfReport
from obi.radio.runner import RadioRunner
from obi.radio.tx import TxExecutor
from tests.fakes import FakeTranscriber

RATE = 16_000
BLOCK = RATE * BLOCK_MS // 1000
BLOCK_US = BLOCK_MS * 1000
S = 1_000_000

_RNG = np.random.default_rng(133)
_NOISE = _RNG.normal(0, 1e-4, BLOCK).astype(np.float32)  # ≈ −80 dBFS: healthy quiet
_SPEECH = _RNG.normal(0, 0.15, BLOCK).astype(np.float32)  # broadband, never a key beep

HAIL = "obi obi this is mira"
GARBLE = "Bitcoin Bitcoin Bitcoin"  # the repetition signature (rx #5986, 2026-09-07)
ICELANDIC = "Ó, píða, og fór í hana."  # rx #6095, 2026-09-07 21:00:51 PT
REPEAT = "is the mountain link up"


def _line(*runs: tuple[np.ndarray, int], start_us: int = 0) -> list[Any]:
    items: list[Any] = []
    t = start_us
    for block, count in runs:
        for _ in range(count):
            items.append((t, block))
            t += BLOCK_US
    return items


def _burst(*, start_us: int, lead: int = 4, talk: int = 20, tail: int = 60) -> list[Any]:
    """quiet, one transmission, quiet tail (hang 1.2 s closes the segment inside it)."""

    return _line((_NOISE, lead), (_SPEECH, talk), (_NOISE, tail), start_us=start_us)


def _wav_ms(duration_ms: int) -> bytes:
    frames = int(22_050 * duration_ms / 1000)
    pcm = (np.full(frames, 0.9) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(22_050)
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _tr(text: str) -> Any:
    from obi.voice.stt import TranscriptionResult

    return TranscriptionResult(text=text, gate="ok", seconds=1.0, level=0.2)


def _report() -> DevicesReport:
    return DevicesReport(
        audio=HalfReport(present=True, detail="audio"),
        serial=HalfReport(present=True, detail="serial"),
    )


@dataclass
class GarbleSource:
    """Scripted AudioSource: feeds the runner monitor and advances the shared clock.

    Items are (t_us, block), an Exception (raised before any block), or a callable.
    """

    script: list[Any]
    clock: dict[str, int]
    name: str
    events: list[str]
    stopped: bool = False
    _monitor: Any = None

    def attach_monitor(self, monitor: Any) -> None:
        self._monitor = monitor

    def blocks(self) -> Any:
        self.events.append(f"opened {self.name}")
        try:
            for item in self.script:
                if self.stopped:
                    return
                if isinstance(item, Exception):
                    raise item
                if callable(item):
                    item()
                    continue
                t_us, block = item
                if self._monitor is not None:
                    peak, rms = dbfs(block)
                    self._monitor.add(t_us, peak_dbfs=peak, rms_dbfs=rms)
                self.clock["now"] = max(self.clock["now"], t_us + BLOCK_US)
                yield item
        finally:
            self.events.append(f"closed {self.name}")

    def stop(self) -> None:
        self.stopped = True


@dataclass
class FakePtt:
    keyed: bool = False

    def key(self) -> None:
        self.keyed = True

    def unkey(self) -> None:
        self.keyed = False


@dataclass
class EventPlayer:
    events: list[str]
    played: int = 0

    def preflight(self) -> None:
        pass

    def play(self, wav_bytes: bytes) -> None:
        self.played += 1
        self.events.append("played")

    def stop(self) -> None:
        pass


class AlwaysClearMonitor:
    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return True


@dataclass
class RecordingNotifier:
    notes: list[str] = dc_field(default_factory=list)

    def deliver_status(self, text: str) -> None:
        self.notes.append(text)


def _settings(tmp_path: Path, **overrides: Any) -> RadioSettings:
    # watchdog=False is the DEPLOYED state (G10). 122 off: these scenarios prove the
    # 133 trigger alone ends a source. floor 10 s (validation minimum) keeps timelines
    # short; min age 0 so a young fake stream is suspected unless a test says otherwise.
    defaults: dict[str, Any] = dict(
        sample_rate=RATE,
        tx_enabled=True,
        tx_gain=1.0,
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
        operator_handles=["mira"],
        slack_call_notes=False,  # per-call DMs are off in the field; cap notes are not
        max_tx_per_minute=30,  # compressed timelines: the G9 rate backstop is not under test
        watchdog=False,
        capture_reopen_after_tx=False,
        capture_reopen_max_uptime_s=0,
        capture_reopen_floor_s=10,
        garble_reopen_min_age_s=0,
    )
    defaults.update(overrides)
    return RadioSettings(**defaults)


@dataclass
class Harness:
    runner: RadioRunner
    executor: TxExecutor
    notifier: RecordingNotifier
    logs: list[str]
    events: list[str]
    sources: list[GarbleSource]
    audit_db: Path
    tmp_path: Path
    clock: dict[str, int]

    def rows(self) -> list[radiostore.Transmission]:
        conn = radiostore.connect_readonly(self.tmp_path / "radio.db")
        try:
            return radiostore.query_window(conn, limit=100, include_self=True)
        finally:
            conn.close()

    def calls(self) -> list[tuple[str, str | None]]:
        conn = radiostore.connect_readonly(self.tmp_path / "radio.db")
        try:
            rows = conn.execute("SELECT state, closure FROM calls ORDER BY id").fetchall()
            return [(str(r[0]), r[1]) for r in rows]
        finally:
            conn.close()

    def outbound(self) -> list[tuple[str, str]]:
        conn = radiostore.connect_readonly(self.tmp_path / "radio.db")
        try:
            rows = conn.execute("SELECT kind, disposition FROM outbound ORDER BY id").fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        finally:
            conn.close()

    def watchdog_events(self) -> list[dict[str, Any]]:
        if not self.audit_db.exists():
            return []
        conn = auditstore.connect_readonly(self.audit_db)
        try:
            events, _total = auditstore.query_events(
                conn, sources=["radio"], limit=200, include_payload=True
            )
        finally:
            conn.close()
        # query_events returns newest first; tests read the trail chronologically.
        return [e for e in reversed(events) if e["session_id"] == "radio-watchdog"]

    def garble_events(self, summary_prefix: str) -> list[dict[str, Any]]:
        return [e for e in self.watchdog_events() if e["summary"].startswith(summary_prefix)]

    def executed(self) -> list[dict[str, Any]]:
        return self.garble_events("capture reopen: garble")

    def suppressed(self) -> list[dict[str, Any]]:
        return self.garble_events("capture reopen suppressed: garble")

    def outcomes(self) -> list[dict[str, Any]]:
        return self.garble_events("garble reopen outcome")


def _harness(
    tmp_path: Path,
    scripts: list[list[Any]],
    transcripts: list[Any],
    settings: RadioSettings,
    *,
    composer: Any = "default",
) -> Harness:
    clock = {"now": 0}

    def sleep(seconds: float) -> None:
        clock["now"] += int(seconds * S)

    notifier = RecordingNotifier()
    logs: list[str] = []
    events: list[str] = []
    audit_db = tmp_path / "audit.db"
    sources = [
        GarbleSource(script, clock, name=str(i + 1), events=events)
        for i, script in enumerate(scripts)
    ]
    queue = list(sources)
    runner = RadioRunner(
        settings=settings,
        source_factory=lambda: queue.pop(0) if queue else None,
        transcriber=FakeTranscriber(results=transcripts),
        probe_fn=_report,
        notifier=notifier,
        sleep=sleep,
        log=logs.append,
        recorder=AuditRecorder(AuditSettings(db_path=audit_db)),
        now_us=lambda: clock["now"],
        spawn=lambda target, *args: target(*args),  # inline: deterministic
    )
    executor = TxExecutor(
        settings=settings,
        monitor=AlwaysClearMonitor(),
        ptt=FakePtt(),
        renderer=lambda text: _wav_ms(len(text) * 10),
        player=EventPlayer(events),
        now_us=lambda: clock["now"],
        sleep=sleep,
        on_keyed_window=runner.note_self_tx,
        on_keyed_start=runner.note_self_tx_start,
    )
    machine = CallMachine(
        ack_text="go for obi",
        say_again_text="ten nine, say again",
        unavailable_text="unable to comply, obi out",
        call_timeout_s=45,
        compose_deadline_s=20,
        operator_handles=list(settings.operator_handles),
        garble_close_text="unable to copy, out",
    )
    if composer == "default":

        def composer(request: str, **_: Any) -> str:
            return f"{request}: yes"

    runner.enable_calls(machine=machine, tx=executor, composer=composer)
    return Harness(runner, executor, notifier, logs, events, sources, audit_db, tmp_path, clock)


# Source 1: the hail at 0 s (acked inline), the garbled request at 6 s. Source 2 (after
# the reopen): 3 s of quiet for the released "say again" and its self window, then the
# caller's repeat at 12 s, then a tail. Timelines are block timestamps; the clock only
# moves forward (a TX's lead/tail sleeps advance it too).
_SRC1 = _burst(start_us=0) + _burst(start_us=6 * S)


def _src2(repeat_at_s: int = 12, tail: int = 60) -> list[Any]:
    return _line((_NOISE, 60), start_us=9 * S) + _burst(start_us=repeat_at_s * S, tail=tail)


def _played_indices(events: list[str]) -> list[int]:
    return [i for i, e in enumerate(events) if e == "played"]


# -- US1: reopen before "say again" (G2, G3, G5) --------------------------------------------


def test_garbled_request_reopens_capture_before_say_again_and_repeat_is_answered(
    tmp_path: Path,
) -> None:
    h = _harness(
        tmp_path, [_SRC1, _src2()], [_tr(HAIL), _tr(GARBLE), _tr(REPEAT)], _settings(tmp_path)
    )
    s1, s2 = h.sources
    h.runner.run()

    assert s1.stopped, "the garble reopen stopped source 1"
    assert not s2.stopped
    # Ordering on the air: ack (source 1) → source 1 closed → source 2 opened → say again →
    # answer. The say-again keyed AFTER the fresh stream was live (G5).
    played = _played_indices(h.events)
    assert len(played) == 3, h.events
    ack_i, say_again_i, answer_i = played
    assert ack_i < h.events.index("closed 1") < h.events.index("opened 2") < say_again_i < answer_i
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("say_again", "transmitted"),
        ("answer", "transmitted"),
    ]
    rx = [(r.transcript, r.stt_gate) for r in h.rows() if r.kind == "rx"]
    assert rx == [(HAIL, "ok"), (GARBLE, "unintelligible"), (REPEAT, "ok")]
    executed = h.executed()
    assert len(executed) == 1
    payload = executed[0]["payload"]
    assert payload["trigger_kind"] == "say_again"
    assert payload["signature"] == "repetition"
    assert payload["excerpt"] == GARBLE
    assert payload["call_id"] == 1
    assert h.suppressed() == []
    assert any("garble reopen: closing capture after" in line for line in h.logs)
    assert any("capture reopened:" in line for line in h.logs)
    assert h.notifier.notes == []  # maintenance never alerts (G7)
    assert h.runner.watchdog_exit_requested is False


def test_widened_judgement_row_reaches_the_reopen(tmp_path: Path) -> None:
    # T014: the Icelandic-looking row is stored unintelligible, logs its signature, and
    # triggers the device recovery exactly like a looping blob does.
    h = _harness(
        tmp_path, [_SRC1, _src2()], [_tr(HAIL), _tr(ICELANDIC), _tr(REPEAT)], _settings(tmp_path)
    )
    h.runner.run()

    rx = [(r.transcript, r.stt_gate) for r in h.rows() if r.kind == "rx"]
    assert rx[1] == (ICELANDIC, "unintelligible")
    assert "garble signature: foreign_orthography" in h.logs
    assert h.executed()[0]["payload"]["signature"] == "foreign_orthography"
    assert h.outbound()[1] == ("say_again", "transmitted")


# -- G6: conversational parity — one say-again per call, then the on-air close ---------------


def test_second_garble_closes_on_air_without_a_second_reopen(tmp_path: Path) -> None:
    h = _harness(
        tmp_path, [_SRC1, _src2()], [_tr(HAIL), _tr(GARBLE), _tr(GARBLE)], _settings(tmp_path)
    )
    s1, s2 = h.sources
    h.runner.run()

    assert s1.stopped and not s2.stopped
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("say_again", "transmitted"),
        ("garble_close", "transmitted"),
    ]
    assert h.calls() == [("garble", "garble")]
    assert len(h.executed()) == 1
    suppressed = h.suppressed()
    assert [e["payload"]["reason"] for e in suppressed] == ["per_call"]
    assert suppressed[0]["payload"]["trigger_kind"] == "garble_close"
    # G8: the garbled repeat is the outcome of the reopen, resolved once.
    outcomes = h.outcomes()
    assert [e["payload"]["outcome"] for e in outcomes] == ["repeat_garbled"]
    assert outcomes[0]["payload"]["excerpt"] == GARBLE


# -- G4: gates suppress, never defer --------------------------------------------------------


def _assert_suppressed_only(h: Harness, reason: str) -> None:
    (s1,) = h.sources[:1]
    assert not s1.stopped, "no reopen: the source ran to its scripted end"
    assert h.executed() == []
    assert [e["payload"]["reason"] for e in h.suppressed()] == [reason]
    # The conversational path is untouched: say again went out at once (no deferral).
    assert h.outbound()[:2] == [("ack", "transmitted"), ("say_again", "transmitted")]
    assert any(f"garble reopen suppressed ({reason}" in line for line in h.logs)


def test_stream_fresh_suppresses(tmp_path: Path) -> None:
    settings = _settings(tmp_path, garble_reopen_min_age_s=600)
    h = _harness(tmp_path, [_SRC1], [_tr(HAIL), _tr(GARBLE)], settings)
    h.runner.run()
    _assert_suppressed_only(h, "stream_fresh")


def test_shared_floor_suppresses(tmp_path: Path) -> None:
    # 122 R4 both ways: a reopen of ANY cause 5 s earlier blocks the garble reopen.
    h = _harness(tmp_path, [_SRC1], [_tr(HAIL), _tr(GARBLE)], _settings(tmp_path))
    h.runner._last_reopen_us = 3 * S  # the garble lands at ~8 s: inside the 10 s floor
    h.runner.run()
    _assert_suppressed_only(h, "floor")


def test_tx_busy_suppresses(tmp_path: Path) -> None:
    h = _harness(tmp_path, [_SRC1], [_tr(HAIL), _tr(GARBLE)], _settings(tmp_path))
    real = h.executor
    h.runner._tx = SimpleNamespace(busy=lambda: True, transmit=real.transmit)  # type: ignore[assignment]
    h.runner.run()
    _assert_suppressed_only(h, "tx_busy")


def test_feature_off_is_byte_for_byte_today(tmp_path: Path) -> None:
    # G10: no candidate evaluation, no audit rows, say again immediate, source untouched.
    h = _harness(
        tmp_path, [_SRC1], [_tr(HAIL), _tr(GARBLE)], _settings(tmp_path, garble_reopen=False)
    )
    (s1,) = h.sources
    h.runner.run()

    assert not s1.stopped
    assert h.watchdog_events() == []
    assert h.outbound() == [("ack", "transmitted"), ("say_again", "transmitted")]
    assert "garble reopen disabled" in h.logs
    assert not any("garble reopen" in line for line in h.logs if "disabled" not in line)


def test_idle_channel_garble_is_not_a_candidate(tmp_path: Path) -> None:
    # G2 / clarification 2: no call open ⇒ obi stays silent and nothing arms.
    h = _harness(tmp_path, [_burst(start_us=0)], [_tr(GARBLE)], _settings(tmp_path))
    (s1,) = h.sources
    h.runner.run()

    assert not s1.stopped
    assert h.outbound() == []
    assert h.watchdog_events() == []
    assert [(r.transcript, r.stt_gate) for r in h.rows()] == [(GARBLE, "unintelligible")]


def test_startup_policy_line(tmp_path: Path) -> None:
    settings = _settings(tmp_path, garble_reopen_max_per_hour=4)
    h = _harness(tmp_path, [_line((_NOISE, 3))], [], settings)
    h.runner.run()
    assert "garble reopen: on (min age 0s, max 4/h)" in h.logs


# -- G5: the held "say again" is never stranded -----------------------------------------------


def test_fresh_source_failure_still_airs_say_again(tmp_path: Path) -> None:
    # Source 2 dies before its first block ⇒ run()'s degrade path releases the held
    # phrase; source 3 then carries the channel.
    h = _harness(
        tmp_path,
        [_SRC1, [RuntimeError("stream died")], _line((_NOISE, 10), start_us=20 * S)],
        [_tr(HAIL), _tr(GARBLE)],
        _settings(tmp_path),
    )
    s1, _s2, s3 = h.sources
    h.runner.run()

    assert s1.stopped
    assert h.outbound() == [("ack", "transmitted"), ("say_again", "transmitted")]
    assert h.events.index("closed 2") < h.events.index("played", h.events.index("closed 1"))
    assert "opened 3" in h.events
    assert any("radio capture failed" in note for note in h.notifier.notes)


def test_stop_drops_the_held_phrase(tmp_path: Path) -> None:
    # A stopping station never keys on the way out: stop() between the reopen and the
    # fresh source's first block drops the held say-again.
    h = _harness(
        tmp_path, [_SRC1, _src2()], [_tr(HAIL), _tr(GARBLE), _tr(REPEAT)], _settings(tmp_path)
    )
    h.sources[1].script.insert(0, h.runner.stop)
    h.runner.run()

    assert h.outbound() == [("ack", "transmitted")]
    assert len(h.executed()) == 1


# -- 122 interplay: the garble reopen stamps the shared floor ---------------------------------


def test_garble_reopen_stamps_the_preventive_floor(tmp_path: Path) -> None:
    # After the exchange (call times out at ~58 s) the 122 after-tx reopen is due, but the
    # 60 s floor anchored by the garble reopen (~8 s) holds until ~68 s: with the script
    # ending at ~66 s it never executes — it is logged as due, not run.
    h = _harness(
        tmp_path,
        [_SRC1, _src2(tail=1080)],  # tail to ~66 s
        [_tr(HAIL), _tr(GARBLE), _tr(REPEAT)],
        _settings(tmp_path, capture_reopen_after_tx=True, capture_reopen_floor_s=60),
    )
    s1, s2 = h.sources
    h.runner.run()

    assert s1.stopped and not s2.stopped
    assert h.calls() == [("timeout", "timeout")]
    assert len(h.executed()) == 1
    assert not any("preventive" in e["summary"] for e in h.watchdog_events())
    assert any("preventive reopen due (after_tx" in line for line in h.logs)


# -- US3: outcomes (G8) and the hourly cap note (G9) ------------------------------------------


def test_outcome_repeat_ok_carries_the_measured_gap(tmp_path: Path) -> None:
    h = _harness(
        tmp_path, [_SRC1, _src2()], [_tr(HAIL), _tr(GARBLE), _tr(REPEAT)], _settings(tmp_path)
    )
    h.runner.run()

    outcomes = h.outcomes()
    assert len(outcomes) == 1
    payload = outcomes[0]["payload"]
    assert payload["outcome"] == "repeat_ok"
    assert payload["excerpt"] == REPEAT
    assert payload["call_id"] == 1
    gap_line = next(line for line in h.logs if "capture reopened:" in line)
    gap_ms = int(gap_line.split("capture reopened: ")[1].split("ms")[0])
    assert payload["gap_ms"] == gap_ms
    assert any(f"garble reopen outcome: repeat_ok ({gap_ms}ms deaf" in line for line in h.logs)


def test_outcome_no_repeat_when_the_call_closes_first(tmp_path: Path) -> None:
    # Source 2: the say-again airs, then only silence until the 45 s call timeout.
    src2 = _line((_NOISE, 60), start_us=9 * S) + _line((_NOISE, 20), start_us=58 * S)
    h = _harness(tmp_path, [_SRC1, src2], [_tr(HAIL), _tr(GARBLE)], _settings(tmp_path))
    h.runner.run()

    assert h.calls() == [("timeout", "timeout")]
    assert [e["payload"]["outcome"] for e in h.outcomes()] == ["no_repeat"]
    assert "excerpt" not in h.outcomes()[0]["payload"]


def test_hourly_cap_suppresses_and_notes_once_per_hour(tmp_path: Path) -> None:
    # max 1/h. Each call is hail → garble → garble (the second garble closes it on the
    # air, so the next hail opens a fresh call). Call 1 (0 s): reopen executes. Calls 2
    # (30 s) and 3 (60 s): both garbles capped — ONE note for the hour. Call 4 (3700 s):
    # the hour rolled, executes. Call 5 (3730 s): capped again, a SECOND note (the first
    # was > 1 h ago). FR-014 throughout: no exit, no watchdog rows.
    def call(at: int) -> list[Any]:
        return (
            _burst(start_us=at * S)
            + _burst(start_us=(at + 6) * S)
            + _burst(start_us=(at + 12) * S)
        )

    def call_transcripts() -> list[Any]:
        return [_tr(HAIL), _tr(GARBLE), _tr(GARBLE)]

    src1 = _burst(start_us=0) + _burst(start_us=6 * S)  # ends at the reopen
    src2 = _line((_NOISE, 60), start_us=9 * S) + _burst(start_us=12 * S) + call(30) + call(60)
    src2 += _burst(start_us=3700 * S) + _burst(start_us=3706 * S)  # ends at reopen #2
    src3 = _line((_NOISE, 60), start_us=3709 * S) + _burst(start_us=3712 * S) + call(3730)
    transcripts = (
        call_transcripts() + call_transcripts() + call_transcripts()
        + call_transcripts() + call_transcripts()
    )
    h = _harness(
        tmp_path, [src1, src2, src3], transcripts, _settings(tmp_path, garble_reopen_max_per_hour=1)
    )
    h.runner.run()

    assert len(h.executed()) == 2
    assert [e["payload"]["call_id"] for e in h.executed()] == [1, 4]
    reasons = [e["payload"]["reason"] for e in h.suppressed()]
    assert reasons == [
        "per_call",  # call 1's garbled repeat on the fresh stream
        "hourly_cap", "hourly_cap",  # call 2
        "hourly_cap", "hourly_cap",  # call 3
        "per_call",  # call 4's garbled repeat
        "hourly_cap", "hourly_cap",  # call 5
    ]
    assert [e["payload"]["outcome"] for e in h.outcomes()] == ["repeat_garbled", "repeat_garbled"]
    cap_notes = [n for n in h.notifier.notes if "garble reopens capped" in n]
    assert len(cap_notes) == 2 and h.notifier.notes == cap_notes
    assert h.calls() == [("garble", "garble")] * 5
    assert not any("rate backstop" in n for n in h.notifier.notes)
    assert h.runner.watchdog_exit_requested is False
    assert not any(e["summary"].startswith("watchdog") for e in h.watchdog_events())
