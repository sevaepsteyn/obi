"""Integration (feature 088, US2): full radio exchanges through the whole runner with fakes.

Scripted channel audio drives capture → trigger → call machine → TxExecutor → store, with
a shared fake clock (the audio source advances it, so channel time and gate time agree)
and inline effect execution (the runner's spawn seam) for determinism. FakePtt/FakePlayer
record RF-side actions; nothing real is touched (FR-036).

Also covers the tier-selection remediation (analysis G2): compose_reply offers the safe or
operator toolset to the model depending on the caller, asserted via StubLLMClient.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from obi.core import radiostore
from obi.core.config import RadioSettings
from obi.radio.calls import CallMachine, Turn
from obi.radio.capture import BLOCK_MS
from obi.radio.devices import DevicesReport, HalfReport
from obi.radio.runner import RadioRunner
from obi.radio.tx import TxExecutor
from tests.fakes import FakeTranscriber

RATE = 16_000
BLOCK = RATE * BLOCK_MS // 1000
BLOCK_US = BLOCK_MS * 1000
S = 1_000_000


def _silence() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.float32)


# Harness gaps are CARRIER-FLOOR quiet (peak −50 / rms −53 dBFS), not zeros: zero
# amplitude is TRUE silence, which the deep-quiet splitter (field 2026-08-20) treats as a
# transmission boundary — segments would close ~900 ms earlier and, under this inline
# harness (no blocks flow during a TX wait, so the monitor goes stale), every ack would
# resolve dropped_busy. Carrier-floor gaps keep every close on the hang path, freezing
# all the block arithmetic these tests were derived with. The split path itself is
# covered by dedicated tests using honest boundary physics (carrier tail → zeros).
_GAP_BLOCK = (0.00317 * np.sin(2 * np.pi * 440 * np.arange(BLOCK, dtype=np.float32) / RATE)).astype(
    np.float32
)


def _gap() -> np.ndarray:
    return _GAP_BLOCK


# Broadband, seeded: a pure sine would (correctly) be gated as a key-up beep by
# classify_non_speech before reaching the fake transcriber.
_SPEECH_BLOCK = np.random.default_rng(4242).normal(0, 0.15, BLOCK).astype(np.float32)


def _speech() -> np.ndarray:
    return _SPEECH_BLOCK


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


class Clock:
    def __init__(self) -> None:
        self.t_us = 0

    def now_us(self) -> int:
        return self.t_us

    def sleep(self, seconds: float) -> None:
        self.t_us += int(seconds * 1_000_000)


class ClockedSource:
    """Feeds scripted blocks and keeps the shared clock at each block's timestamp, so the
    TX gate's `now` and the monitor's block times share one timeline. Like the real
    capture source, it feeds the gate's monitor itself (attach_monitor) — the runner no
    longer wires the consumer-side Segmenter to the shared monitor."""

    def __init__(self, clock: Clock, script: list[tuple[int, np.ndarray]]) -> None:
        self._clock = clock
        self._script = script
        self._monitor: Any = None
        self.stopped = False

    def attach_monitor(self, monitor: Any) -> None:
        self._monitor = monitor

    def blocks(self) -> Any:
        from obi.radio.capture import dbfs

        for t_us, block in self._script:
            self._clock.t_us = max(self._clock.t_us, t_us)
            if self._monitor is not None:
                peak, rms = dbfs(block)
                self._monitor.add(t_us, peak_dbfs=peak, rms_dbfs=rms)
            yield t_us, block

    def stop(self) -> None:
        self.stopped = True


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


@dataclass
class RecordingNotifier:
    notes: list[str] = field(default_factory=list)

    def deliver_status(self, text: str) -> None:
        self.notes.append(text)


class NeverClearMonitor:
    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return False


def _tr(text: str, gate: str = "ok") -> Any:
    from obi.voice.stt import TranscriptionResult

    return TranscriptionResult(text=text, gate=gate, seconds=1.0, level=0.2)  # type: ignore[arg-type]


def _script(*bursts: tuple[int, bool], start_block: int = 0) -> list[tuple[int, np.ndarray]]:
    """(count, speaking) runs → timestamped blocks. Gaps are carrier-floor quiet (_gap)."""

    blocks: list[tuple[int, np.ndarray]] = []
    index = start_block
    for count, speaking in bursts:
        for _ in range(count):
            blocks.append((index * BLOCK_US, _speech() if speaking else _gap()))
            index += 1
    return blocks


@dataclass
class Harness:
    runner: RadioRunner
    clock: Clock
    ptt: FakePtt
    player: FakePlayer
    notifier: RecordingNotifier
    settings: RadioSettings
    transcriber: FakeTranscriber

    def rows(self) -> list[radiostore.Transmission]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            return radiostore.query_window(conn, limit=100, include_self=True)
        finally:
            conn.close()

    def calls(self) -> list[tuple[str, str | None]]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            rows = conn.execute("SELECT state, closure FROM calls ORDER BY id").fetchall()
            return [(str(r[0]), r[1]) for r in rows]
        finally:
            conn.close()

    def outbound(self) -> list[tuple[str, str]]:
        conn = radiostore.connect_readonly(self.settings.db_path)
        try:
            rows = conn.execute("SELECT kind, disposition FROM outbound ORDER BY id").fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        finally:
            conn.close()


def _harness(
    tmp_path: Path,
    script: list[tuple[int, np.ndarray]],
    transcripts: list[Any],
    *,
    composer: Any = None,
    monitor_override: Any = None,
    tx_enabled: bool = True,
    shadow_tx: bool = False,
    known_callers: list[str] | None = None,
    max_answers_per_call: int | None = None,
    slack_call_notes: bool = True,
    pre_tx: list[tuple[str, str]] | None = None,
) -> Harness:
    settings = RadioSettings(
        sample_rate=RATE,
        tx_enabled=tx_enabled,
        shadow_tx=shadow_tx,
        tx_gain=1.0,
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
        operator_handles=["nono"],
        known_callers=known_callers or [],
        slack_call_notes=slack_call_notes,
        # 137: the channel's mis-hearing alias is config now (no built-in default).
        speech_aliases={"knock": "NOC"},
        # These scenarios script ONE source; a preventive reopen (122) between exchanges
        # would end the run early. Reopen behavior has its own suite
        # (test_radio_reopen_runner.py) — off here, exactly the R10 off state.
        capture_reopen_after_tx=False,
        capture_reopen_max_uptime_s=0,
    )
    clock = Clock()
    ptt = FakePtt()
    player = FakePlayer()
    notifier = RecordingNotifier()
    sources: list[Any] = [ClockedSource(clock, script)]
    transcriber = FakeTranscriber(results=transcripts)
    runner = RadioRunner(
        settings=settings,
        source_factory=lambda: sources.pop(0) if sources else None,
        transcriber=transcriber,
        probe_fn=lambda: DevicesReport(
            audio=HalfReport(True, "audio"), serial=HalfReport(True, "serial")
        ),
        notifier=notifier,
        sleep=clock.sleep,
        now_us=clock.now_us,
        spawn=lambda target, *args: target(*args),  # inline: deterministic
    )
    executor = TxExecutor(
        settings=settings,
        monitor=monitor_override or runner.monitor,
        ptt=ptt,
        renderer=lambda text: _wav_ms(len(text) * 10),
        player=player,
        now_us=clock.now_us,
        sleep=clock.sleep,
        on_keyed_window=runner.note_self_tx,
        on_keyed_start=runner.note_self_tx_start,
    )
    machine_kwargs: dict[str, Any] = {}
    if max_answers_per_call is not None:
        machine_kwargs["max_answers_per_call"] = max_answers_per_call
    machine = CallMachine(
        ack_text="go for obi",
        say_again_text="ten nine, say again",
        unavailable_text="unable to comply, obi out",
        call_timeout_s=45,
        compose_deadline_s=20,
        operator_handles=list(settings.operator_handles),
        **machine_kwargs,
    )
    runner.enable_calls(machine=machine, tx=executor, composer=composer)
    for kind, text in pre_tx or []:
        # 093 rate-replay seam: put transmissions on the air through the SAME gate and
        # rate windows before the scripted channel runs (the inline spawn seam cannot
        # interleave a standby with its compose, so the incident's standbys are seeded).
        executor.transmit(kind, text, deadline_us=clock.now_us() + 60 * S)
    runner.run()
    return Harness(runner, clock, ptt, player, notifier, settings, transcriber)


# Scripted channel: silence, the call, a gap long enough for obi's own ack transmission
# (whose keyed window + margin would otherwise mark the next speech as kind='self'), the
# question, then 46+ s of silence (timeout).
_CALL_THEN_QUESTION = _script((10, False), (20, True), (60, False), (20, True), (940, False))
# Transcripts served in segment order: the call, then the question.
_CALL_Q_TRANSCRIPTS = [_tr("obi obi this is nono"), _tr("is the mountain link up")]


def test_happy_path_call_ack_answer_timeout(tmp_path: Path) -> None:
    def composer(request: str, **_kw: Any) -> str:
        assert request == "is the mountain link up"
        return "mountain link is up, over"

    h = _harness(tmp_path, _CALL_THEN_QUESTION, _CALL_Q_TRANSCRIPTS, composer=composer)
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]
    obi_rows = [r for r in h.rows() if r.kind == "obi"]
    # "nono" is roster-identified (operator_handles), so the ack is personalized.
    assert [r.transcript for r in obi_rows] == [
        "nono, go for obi",
        "mountain link is up, over",
    ]
    assert h.ptt.key_calls == 2 and not h.ptt.keyed
    assert len(h.player.played) == 2
    # SC-004: the ack reached the air within 5 s (channel time) of the caller unkeying.
    call_row = next(r for r in h.rows() if r.trigger_fired)
    ack_row = obi_rows[0]
    assert ack_row.started_us - call_row.ended_us <= 5 * S
    # 1.13.0 replay: obi's own transmissions are retained as clips on disk.
    for row in obi_rows:
        assert row.clip_path is not None
        assert (h.settings.clips_dir / row.clip_path).is_file()
    # FR-010: expiry closed the call silently — exactly two transmissions, closure=timeout.
    assert h.calls() == [("timeout", "timeout")]
    # The obi rows carry the call id (one exchange, one thread).
    assert {r.call_id for r in obi_rows} == {call_row.call_id}


def test_caller_clear_closes_cleared(tmp_path: Path) -> None:
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (10, True), (40, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("radio check"),
        _tr("copy that, nono clear"),
    ]
    h = _harness(tmp_path, script, transcripts, composer=lambda *a, **k: "five by five, over")
    assert h.calls() == [("cleared", "cleared")]
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]


def test_double_garble_closes_with_note(tmp_path: Path) -> None:
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (20, True), (40, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("", gate="no_speech"),
        _tr("", gate="no_speech"),
    ]
    h = _harness(tmp_path, script, transcripts, composer=lambda *a, **k: "never called")
    assert h.calls() == [("garble", "garble")]
    # 093: the give-up goes ON the air before the close — "unable to copy, out".
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("say_again", "transmitted"),
        ("garble_close", "transmitted"),
    ]
    assert any("unintelligible" in n for n in h.notifier.notes)


def test_compose_reply_strips_an_echoed_bracket_prefix(tmp_path: Path) -> None:
    # Field 2026-08-20 14:09: the model echoed the input framing — "[mira] COPY, …"
    # went to air. The leading bracket tag is scrubbed deterministically.
    from obi.radio.runner import compose_reply

    runtime, _stub = _stub_runtime(tmp_path, answer="[mira] NOC is up, over")
    answer = compose_reply(
        runtime,
        request="status of the noc",
        caller="mira",
        tier="operator",
        call_id=11,
        live_calls=3,
        recorder=None,
    )
    assert answer is not None and answer.text == "NOC is up, over"
    assert answer.expects_reply is False


def test_post_answer_chatter_composes_in_window(tmp_path: Path) -> None:
    # Feature 109 reverses the 14:09 rule IN-WINDOW (operator decision 2026-08-28):
    # the window belongs to the caller, so post-answer intelligible traffic composes —
    # the accepted cost of never dropping a real follow-up. The call then expires.
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (30, True), (940, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("is the mountain link up"),
        _tr("well i'm glad i was on the wrong radio channel for that one"),
    ]
    composed: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        composed.append(request)
        return "link is up, over"

    h = _harness(tmp_path, script, transcripts, composer=composer)
    assert composed == [
        "is the mountain link up",
        "well i'm glad i was on the wrong radio channel for that one",
    ]
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("answer", "transmitted"),
        ("answer", "transmitted"),
    ]
    assert h.calls() == [("timeout", "timeout")]


def test_clarification_after_a_soliciting_answer_gets_answered(tmp_path: Path) -> None:
    # The 14:28 field replay: a mis-heard request ("manzanita, mountain, Luke?") drew an
    # answer ending "say again on that third name, over"; the caller's clarification
    # must compose a second answer — not close the call 'complete' (the 5229d34 rule
    # applies only to answers that did NOT solicit a reply).
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (30, True), (1400, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("status of the manzanita mountain luke"),
        _tr("i meant the seventy gigahertz siklu link"),
    ]
    answers = iter(
        ["both routers up — say again on that third name, over", "siklu link is up, over"]
    )
    composed: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        composed.append(request)
        return next(answers)

    class AlwaysClearMonitor:
        def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
            return True

    h = _harness(
        tmp_path,
        script,
        transcripts,
        composer=composer,
        monitor_override=AlwaysClearMonitor(),
    )
    assert composed == [
        "status of the manzanita mountain luke",
        "i meant the seventy gigahertz siklu link",
    ]
    assert [k for k, _d in h.outbound()] == ["ack", "answer", "answer"]
    assert h.calls() == [("timeout", "timeout")]  # answered, then silent expiry


def test_slack_call_notes_off_silences_the_close_note(tmp_path: Path) -> None:
    # Operator direction 2026-08-20: no per-call DMs. The garble close still happens —
    # only its Slack note is dropped; malfunction alerts elsewhere are unaffected.
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (20, True), (40, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("", gate="no_speech"),
        _tr("", gate="no_speech"),
    ]
    h = _harness(
        tmp_path,
        script,
        transcripts,
        composer=lambda *a, **k: "never called",
        slack_call_notes=False,
    )
    assert h.calls() == [("garble", "garble")]
    assert h.notifier.notes == []


def test_compose_failure_transmits_unavailability_and_notes(tmp_path: Path) -> None:
    h = _harness(tmp_path, _CALL_THEN_QUESTION, _CALL_Q_TRANSCRIPTS, composer=lambda *a, **k: None)
    assert h.outbound() == [("ack", "transmitted"), ("unavailability", "transmitted")]
    assert h.calls() == [("failure", "failure")]
    assert any("compose failed" in n for n in h.notifier.notes)


def test_busy_channel_drops_ack_with_note_and_no_rf(tmp_path: Path) -> None:
    h = _harness(
        tmp_path,
        _CALL_THEN_QUESTION,
        _CALL_Q_TRANSCRIPTS,
        composer=lambda *a, **k: "never",
        monitor_override=NeverClearMonitor(),
    )
    assert h.outbound() == [("ack", "dropped_busy")]
    assert h.ptt.key_calls == 0 and h.player.played == []
    assert h.calls() == [("dropped", "dropped")]
    assert any("dropped" in n for n in h.notifier.notes)


def test_own_roger_beep_is_marked_self_and_never_burns_say_again(tmp_path: Path) -> None:
    # Field replay (2026-08-19 calls 5/6): obi's radio bleeds its roger beep into the RX
    # tap during obi's own transmissions. Timing arithmetic: the call segment closes at
    # block 53 (t=2650 ms) → inline ack TX keys at 2650 with lead 300 + tail 200 ⇒ keyed
    # window [2650, 3150] ms, ±250 ms margin ⇒ [2400, 3400]. The 12 "bleed" blocks land
    # at 2700–3300 ms (inside); they are BROADBAND speech blocks so only the keyed window
    # (L1), never the tone classifier (L2), can be what marks them self. The question at
    # blocks 90–109 (4500–5500 ms; pre-roll 4300 > 3400) stays clean rx.
    script = _script(
        (10, False), (20, True), (24, False), (12, True), (24, False), (20, True), (940, False)
    )
    transcripts = [_tr("obi obi this is nono"), _tr("is the mountain link up")]
    h = _harness(tmp_path, script, transcripts, composer=lambda *a, **k: "link is up, over")
    rows = h.rows()
    assert [r.kind for r in rows] == ["rx", "obi", "self", "rx", "obi"]
    self_row = rows[2]
    assert self_row.stt_gate == "self" and self_row.transcript == ""
    assert not self_row.trigger_fired
    # STT never ran on the bleed: exactly the call + the question reached the transcriber.
    assert len(h.transcriber.heard) == 2
    # No say-again burned; the call answered cleanly and timed out silently.
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]
    assert h.calls() == [("timeout", "timeout")]


# --- tier selection (analysis G2) ---------------------------------------------------------


def _stub_runtime(tmp_path: Path, answer: str = "all up, over") -> tuple[Any, Any]:
    from obi.chat.session import ChatRuntime, build_tool_context
    from obi.core.config import LogsSettings, load_config
    from obi.core.llm import LLMResponse, TextBlock
    from obi.core.toolset import build_tools
    from tests.fakes import StubLLMClient

    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n')
    config_path.chmod(0o600)
    config = load_config(config_path)
    assert isinstance(config.logs, LogsSettings)
    ctx = build_tool_context(config)
    stub = StubLLMClient(
        responses=[LLMResponse(stop_reason="end_turn", content=[TextBlock(text=answer)])]
    )
    runtime = ChatRuntime(
        client=stub,
        ctx=ctx,
        tools=build_tools(),
        model="claude-test",
        max_tokens=200,
        system="radio persona",
        context_warning=None,
        context_loaded=False,
    )
    return runtime, stub


def _available(runtime: object) -> set[str]:
    """137: a tier is narrowed to the tools whose config sections are configured."""

    from obi.core.toolset import available_tools, build_operator_tools

    config = runtime.ctx.config  # type: ignore[attr-defined]
    return {t.name for t in available_tools(build_operator_tools(), config)}


def test_safe_tier_offers_only_the_safe_toolset(tmp_path: Path) -> None:
    from obi.core.toolset import RADIO_SAFE_TOOL_NAMES
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    answer = compose_reply(
        runtime,
        request="is the mountain link up",
        caller="stranger",
        tier="safe",
        call_id=1,
        live_calls=3,
        recorder=None,
    )
    assert answer is not None and answer.text == "all up, over"
    offered = {t["name"] for t in stub.calls[0]["tools"]}
    assert offered == set(RADIO_SAFE_TOOL_NAMES) & _available(runtime)


def test_operator_tier_offers_the_full_readonly_set(tmp_path: Path) -> None:
    from obi.core.toolset import RADIO_OPERATOR_TOOL_NAMES
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    compose_reply(
        runtime,
        request="show me the chestnut logs",
        caller="nono",
        tier="operator",
        call_id=2,
        live_calls=3,
        recorder=None,
    )
    offered = {t["name"] for t in stub.calls[0]["tools"]}
    assert offered == set(RADIO_OPERATOR_TOOL_NAMES) & _available(runtime)


def test_zero_live_budget_strips_budgeted_tools(tmp_path: Path) -> None:
    from obi.core.toolset import BUDGETED_LIVE_TOOL_NAMES, RADIO_OPERATOR_TOOL_NAMES
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    compose_reply(
        runtime,
        request="status",
        caller="nono",
        tier="operator",
        call_id=3,
        live_calls=0,
        recorder=None,
    )
    offered = {t["name"] for t in stub.calls[0]["tools"]}
    assert offered == (set(RADIO_OPERATOR_TOOL_NAMES) & _available(runtime)) - set(
        BUDGETED_LIVE_TOOL_NAMES
    )


def test_compose_reply_prefixes_the_caller_for_addressing(tmp_path: Path) -> None:
    # Operator direction (2026-08-19): the model addresses the caller by callsign, or as
    # "caller" when unidentified — the prefix is how it learns which case it is in.
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    compose_reply(
        runtime,
        request="radio check",
        caller="sawdust",
        tier="safe",
        call_id=9,
        live_calls=3,
        recorder=None,
    )
    assert stub.calls[0]["messages"][0]["content"] == "[sawdust] radio check"
    runtime2, stub2 = _stub_runtime(tmp_path)
    compose_reply(
        runtime2,
        request="radio check",
        caller=None,
        tier="safe",
        call_id=10,
        live_calls=3,
        recorder=None,
    )
    assert stub2.calls[0]["messages"][0]["content"] == "[unidentified caller] radio check"


def test_neighbor_beep_in_call_does_not_burn_say_again(tmp_path: Path) -> None:
    # L2+L3 end-to-end: ANOTHER station's roger beep lands inside our open call (after
    # the ack's keyed window + margin, so it is genuine rx, not self). It must classify
    # no_speech, never reach the call machine, and never burn the say-again budget.
    # Timing: call closes block 53 (2650 ms) → ack window [2650,3150] + 250 margin =
    # 3400; beep loud onset 3800 ms (pre-roll 3600 > 3400 ⇒ rx). TWO beep clusters —
    # under the old behavior the second would have garble-closed the call.
    def _tone_blocks(rng: Any) -> list[np.ndarray]:
        t1 = np.arange(RATE * 180 // 1000, dtype=np.float32)
        beep = np.concatenate(
            [
                (0.3 * np.sin(2 * np.pi * 1570 * t1 / RATE)).astype(np.float32),
                (0.3 * np.sin(2 * np.pi * 1320 * t1 / RATE)).astype(np.float32),
            ]
        )
        padded = np.concatenate(
            [beep, rng.normal(0, 0.0025, 8 * BLOCK - beep.size).astype(np.float32)]
        )
        return [padded[i * BLOCK : (i + 1) * BLOCK] for i in range(8)]

    rng = np.random.default_rng(11)
    silence = _gap()  # carrier-floor gaps: keep every close on the hang path (see _gap)
    blocks: list[tuple[int, np.ndarray]] = []

    def _append(items: list[np.ndarray]) -> None:
        base = len(blocks)
        for i, b in enumerate(items):
            blocks.append(((base + i) * BLOCK_US, b))

    _append([silence] * 10)
    _append([_speech()] * 20)  # the call: "obi obi this is nono"
    _append([silence] * 46)  # ack transmits inline at 2650 ms; margin ends 3400 ms
    _append(_tone_blocks(rng))  # beep #1 at 3800-4200 ms
    _append([silence] * 30)
    _append(_tone_blocks(rng))  # beep #2 (would have garble-closed under old behavior)
    _append([silence] * 30)
    _append([_speech()] * 20)  # the real question
    _append([silence] * 940)  # then silence until the call times out

    transcripts = [_tr("obi obi this is nono"), _tr("is the mountain link up")]
    h = _harness(tmp_path, blocks, transcripts, composer=lambda *a, **k: "link is up, over")
    rows = h.rows()
    beep_rows = [r for r in rows if r.stt_gate == "no_speech"]
    assert len(beep_rows) == 2 and all(r.kind == "rx" for r in beep_rows)
    assert len(h.transcriber.heard) == 2  # whisper saw only the call and the question
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]  # NO say_again
    assert h.calls() == [("timeout", "timeout")]  # answered, then silent expiry — no garble


def test_incall_transmissions_attributed_to_the_calls_caller(tmp_path: Path) -> None:
    # Field 2026-08-20 10:02: mid-call requests carried speaker=None even though the
    # hail identified the caller — an open call's traffic is the caller's by protocol.
    # The BARE hail form ("Obi, Obi, Mira") also went unattributed: the tracker's hail
    # rule wants a roster name in hail position, but the trigger's roster-certain
    # identification (the one that personalizes the ack) is a speaker identification.
    transcripts = [_tr("obi obi nono"), _tr("is the mountain link up")]
    h = _harness(
        tmp_path,
        _CALL_THEN_QUESTION,
        transcripts,
        composer=lambda *a, **k: "link is up, over",
    )
    hail = next(r for r in h.rows() if r.trigger_fired)
    assert (hail.speaker, hail.speaker_confidence) == ("nono", "high")
    question = next(r for r in h.rows() if r.transcript == "is the mountain link up")
    assert (question.speaker, question.speaker_confidence) == ("nono", "high")


def test_speech_aliases_canonicalize_the_composer_request(tmp_path: Path) -> None:
    # Field 2026-08-20: whisper hears NOC as "knock" (its own vocab prompt says so) —
    # the composer gets the canonical spelling; the stored transcript keeps the hearing.
    transcripts = [_tr("obi obi this is nono"), _tr("is the knock gateway up")]
    seen: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        seen.append(request)
        return "it is up, over"

    h = _harness(tmp_path, _CALL_THEN_QUESTION, transcripts, composer=composer)
    assert seen == ["is the NOC gateway up"]
    assert any(r.transcript == "is the knock gateway up" for r in h.rows())


def test_rehail_over_budget_supersedes_into_a_fresh_call(tmp_path: Path) -> None:
    # Field 2026-08-20 10:03: the answer budget silently abandoned a legitimate named
    # re-hail. End-to-end: budget 1, answer consumed, then a full re-hail — the old call
    # closes superseded, a NEW call acks and answers the carried question. Ordering
    # matters: the close must land on the OLD call, not tear down the new one.
    # The tail is longer than _CALL_THEN_QUESTION's: two full TX exchanges advance the
    # harness clock, pushing the second call's 45 s timeout deadline further out.
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (30, True), (1400, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("is the mountain link up"),
        _tr("obi obi this is nono what is the uptime of the noc gateway"),
    ]
    seen: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        seen.append(request)
        return "affirmative, over"

    class AlwaysClearMonitor:
        def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
            return True

    # Always-clear monitor: the back-to-back ack→answer would otherwise starve on the
    # scripted source's monitor freshness (live capture feeds it continuously; the
    # inline harness produces no blocks during a transmission). Gate physics are
    # covered elsewhere — this test is about the supersede call bookkeeping.
    h = _harness(
        tmp_path,
        script,
        transcripts,
        composer=composer,
        max_answers_per_call=1,
        monitor_override=AlwaysClearMonitor(),
    )
    assert seen == ["is the mountain link up", "what is the uptime of the noc gateway"]
    # The superseding re-hail carried its question — no second ack (one-breath rule).
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("answer", "transmitted"),
        ("answer", "transmitted"),
    ]
    assert h.calls() == [("superseded", "superseded"), ("timeout", "timeout")]
    # The two hails ride DIFFERENT calls — the re-hail row carries the new call id.
    fired_call_ids = {r.call_id for r in h.rows() if r.trigger_fired}
    assert len(fired_call_ids) == 2 and None not in fired_call_ids


def test_recall_with_question_gets_answered(tmp_path: Path) -> None:
    # Field 2026-08-19 22:45:14: after the ack, the caller re-hailed WITH the question —
    # trigger fires (name at start), remainder longer than a handle ⇒ anonymous
    # request_tail. Before L4 this exact input was discarded busy_with_call.
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("obi what is the uptime of the noc gateway"),
    ]
    seen: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        seen.append(request)
        return "one week and five minutes, over"

    h = _harness(tmp_path, _CALL_THEN_QUESTION, transcripts, composer=composer)
    assert seen == ["what is the uptime of the noc gateway"]
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]
    assert h.calls() == [("timeout", "timeout")]


# --- the deep-quiet splitter end-to-end (field 2026-08-20) ----------------------------------


def test_split_ack_latency(tmp_path: Path) -> None:
    # Honest boundary physics: each transmission ends with a short carrier tail (the far
    # radio still keyed but silent) then TRUE silence once it unkeys. The splitter closes
    # the segment ~300 ms into the true silence — the ack keys ~800 ms after the caller's
    # last word instead of waiting out the 1200 ms hang.
    blocks: list[tuple[int, np.ndarray]] = []

    def _append(items: list[np.ndarray]) -> None:
        base = len(blocks)
        for i, b in enumerate(items):
            blocks.append(((base + i) * BLOCK_US, b))

    _append([_gap()] * 10)
    _append([_speech()] * 20)  # the call, ends 1500 ms
    _append([_gap()] * 10)  # carrier tail 1500-2000 ms
    _append([_silence()] * 46)  # true silence: split at ~2300 ms, ack keys immediately
    _append([_speech()] * 20)  # the question at 4300-5300 ms (clear of the ack margin)
    _append([_gap()] * 10)
    _append([_silence()] * 940)

    transcripts = [_tr("obi obi this is nono"), _tr("is the mountain link up")]
    h = _harness(tmp_path, blocks, transcripts, composer=lambda *a, **k: "link is up, over")
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]
    rows = h.rows()
    call_row = next(r for r in rows if r.trigger_fired)
    ack_row = next(r for r in rows if r.kind == "obi")
    # The latency win: ack on the air within 1 s of the caller's last word (was ~1.2 s+).
    assert ack_row.started_us - call_row.ended_us <= 1_000_000


def test_row84_exchange_stored_as_six_rows(tmp_path: Path) -> None:
    # THE field case (store row 84): a six-transmission IT/waterworks exchange, gaps
    # 0.4-1.1 s of true silence each ending in a key-click — previously ONE 30.8 s row.
    def _click_block() -> np.ndarray:
        block = np.zeros(BLOCK, dtype=np.float32)
        block[: RATE * 5 // 1000] = 0.98
        return block

    blocks: list[tuple[int, np.ndarray]] = []

    def _append(items: list[np.ndarray]) -> None:
        base = len(blocks)
        for i, b in enumerate(items):
            blocks.append(((base + i) * BLOCK_US, b))

    speech_runs = [40, 30, 60, 40, 50, 40]
    gaps = [12, 8, 22, 16, 10]
    _append([_silence()] * 4)
    _append([_speech()] * speech_runs[0])
    for gap, run in zip(gaps, speech_runs[1:], strict=True):
        _append([_silence()] * gap)
        _append([_click_block()])
        _append([_speech()] * run)
    _append([_silence()] * 40)

    transcripts = [
        _tr("it support it support waterworks"),
        _tr("waterworks go for it"),
        _tr("hi there we have a couple keyboards to trade out"),
        _tr("copy that come on over"),
        _tr("alright we'll swing by in the next hour thank you"),
        _tr("clear"),
    ]
    h = _harness(
        tmp_path,
        blocks,
        transcripts,
        composer=lambda *a, **k: "never",
        known_callers=["IT", "IT Support", "Waterworks"],
    )
    rows = [r for r in h.rows() if r.kind == "rx"]
    assert len(rows) == 6  # previously merged into one
    assert [r.transcript for r in rows] == [t.text for t in transcripts]
    assert h.outbound() == []  # nothing here is directed at obi
    # Speaker attribution: the hail and the answer name their speakers (high); the
    # following turns alternate between the two established parties (low).
    attributed = [(r.speaker, r.speaker_confidence) for r in rows]
    assert attributed[0] == ("waterworks", "high")
    assert attributed[1] == ("it", "high")
    assert attributed[2] == ("waterworks", "low")
    assert attributed[3] == ("it", "low")
    assert attributed[4] == ("waterworks", "low")
    assert attributed[5] == ("it", "low")


# --- feature 091: call memory — the composer sees the exchange so far -----------------------


class _AlwaysClearMonitor:
    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return True


def test_follow_up_composes_with_the_first_turn_aired_text_in_history(tmp_path: Path) -> None:
    # Two questions in one call (the 17:31 shape). The second compose must see turn one
    # — the caller's request CANONICALIZED (speech aliases, same as the live request)
    # and obi's answer AS AIRED: the cap truncated the long draft to its first
    # sentence, and history must carry that, never the draft (FR-005/SC-001 plumbing).
    from obi.radio.calls import Turn

    long_tail = "the rest of the detail nobody asked for " * 60  # one huge sentence
    draft = f"knock gateway is up, over. {long_tail}end."
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (30, True), (1400, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("is the knock gateway up"),
        _tr("obi what about the chestnut gateway"),
    ]
    seen: list[tuple[str, tuple[Turn, ...]]] = []

    def composer(request: str, *, history: tuple[Turn, ...] = (), **_kw: Any) -> str:
        seen.append((request, history))
        return draft if len(seen) == 1 else "chestnut is up too, over"

    h = _harness(
        tmp_path,
        script,
        transcripts,
        composer=composer,
        monitor_override=_AlwaysClearMonitor(),
    )
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("answer", "transmitted"),
        ("answer", "transmitted"),
    ]
    assert seen[0] == ("is the NOC gateway up", ())
    request2, history2 = seen[1]
    assert request2 == "what about the chestnut gateway"
    assert history2 == (Turn(request="is the NOC gateway up", answer="knock gateway is up, over."),)


def test_compose_reply_seeds_history_ahead_of_the_live_question(tmp_path: Path) -> None:
    from obi.radio.calls import Turn
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    compose_reply(
        runtime,
        request="and to google?",
        caller="mira",
        tier="operator",
        call_id=4,
        live_calls=3,
        recorder=None,
        history=(
            Turn(request="rtt noc to manzanita", answer="zero point eight milliseconds, over"),
            Turn(request="say again the unit", answer="milliseconds, over"),
        ),
    )
    messages = stub.calls[0]["messages"]
    assert messages[:4] == [
        {"role": "user", "content": "[mira] rtt noc to manzanita"},
        {"role": "assistant", "content": "zero point eight milliseconds, over"},
        {"role": "user", "content": "[mira] say again the unit"},
        {"role": "assistant", "content": "milliseconds, over"},
    ]
    assert messages[4]["content"] == "[mira] and to google?"


def test_compose_reply_frames_history_like_the_live_question_when_anonymous(
    tmp_path: Path,
) -> None:
    from obi.radio.calls import Turn
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    compose_reply(
        runtime,
        request="what's the latency?",
        caller=None,
        tier="safe",
        call_id=5,
        live_calls=3,
        recorder=None,
        history=(Turn(request="radio check", answer="five by five, over"),),
    )
    messages = stub.calls[0]["messages"]
    assert messages[0]["content"] == "[unidentified caller] radio check"
    assert messages[2]["content"] == "[unidentified caller] what's the latency?"


def test_stale_worker_results_cannot_touch_a_successor_call(tmp_path: Path) -> None:
    # The spec's race, interleaved by hand at the worker seam (the inline-spawn harness
    # cannot overlap operations): call 1 goes composing, the caller clears it, call 2
    # opens and goes composing — then call 1's compose worker finally runs. Without the
    # call-id guard the machine (composing again, but for call 2) would accept call 1's
    # answer as call 2's. Same shape for a stale TX worker: it must not key RF at all.
    from obi.radio.calls import ComposeEffect, OpenCallEffect, TransmitEffect

    seen: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        seen.append(request)
        return f"answer to {request}, over"

    h = _harness(
        tmp_path,
        _script((5, False)),
        [],
        composer=composer,
        monitor_override=_AlwaysClearMonitor(),
    )
    r, m = h.runner, h.runner._machine
    assert m is not None
    # run() closed the store on exit; this test drives the worker seams post-run.
    r._conn = radiostore.connect(h.settings.db_path, check_same_thread=False)

    out1 = m.on_transmission(
        0,
        fired=True,
        caller="nono",
        text="obi obi this is nono",
        intelligible=True,
        request_tail="first question",
    )
    r._open_call(next(e for e in out1.effects if isinstance(e, OpenCallEffect)), opened_us=0)
    call1 = r._call_id
    compose1 = next(e for e in out1.effects if isinstance(e, ComposeEffect))

    out2 = m.on_transmission(1 * S, fired=False, caller=None, text="nono out", intelligible=True)
    r._close_call(next(iter(out2.effects)))  # call 1 closed mid-compose

    out3 = m.on_transmission(
        2 * S,
        fired=True,
        caller="yoyo",
        text="obi obi this is yoyo",
        intelligible=True,
        request_tail="second question",
    )
    r._open_call(next(e for e in out3.effects if isinstance(e, OpenCallEffect)), opened_us=2 * S)
    call2 = r._call_id
    compose2 = next(e for e in out3.effects if isinstance(e, ComposeEffect))
    assert call1 != call2 and m.state == "composing"

    # Call 1's compose worker lands late: no composer consult, no machine touch.
    r._compose_worker(compose1, call1)
    assert seen == []
    assert m.state == "composing"  # still waiting for ITS OWN answer
    assert h.outbound() == []

    # A stale TX worker for call 1 must not key RF or store an outbound row.
    keyed_before = h.ptt.key_calls
    r._tx_worker(TransmitEffect(kind="answer", text="ghost of call one, over"), call1)
    assert h.ptt.key_calls == keyed_before
    assert h.outbound() == []
    assert m.state == "composing"

    # And the CURRENT call still works end to end.
    r._compose_worker(compose2, call2)
    assert seen == ["second question"]
    assert h.outbound() == [("answer", "transmitted")]
    assert m.transcript == [
        Turn(request="second question", answer="answer to second question, over")
    ]


def test_raising_composer_fails_its_call_and_the_next_call_starts_fresh(tmp_path: Path) -> None:
    # US3 end-to-end: turn one explodes inside the composer (belt path) — the call gets
    # the canned unavailability and closes; a NEW call then answers normally, with
    # nothing carried over from the wreck.
    calls_seen: list[tuple[str, tuple[Turn, ...]]] = []

    def composer(request: str, *, history: tuple[Turn, ...] = (), **_kw: Any) -> str:
        calls_seen.append((request, history))
        if len(calls_seen) == 1:
            raise RuntimeError("model exploded mid-turn")
        return "chestnut is up, over"

    script = _script(
        (10, False),
        (20, True),
        (60, False),
        (20, True),
        (60, False),
        (20, True),
        (60, False),
        (20, True),
        (1400, False),
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("is the noc gateway up"),
        _tr("obi obi this is nono"),
        _tr("is the chestnut gateway up"),
    ]
    h = _harness(
        tmp_path,
        script,
        transcripts,
        composer=composer,
        monitor_override=_AlwaysClearMonitor(),
    )
    assert [k for k, _d in h.outbound()] == ["ack", "unavailability", "ack", "answer"]
    assert h.calls() == [("failure", "failure"), ("timeout", "timeout")]
    assert calls_seen == [
        ("is the noc gateway up", ()),
        ("is the chestnut gateway up", ()),  # fresh call: no history from the wreck
    ]


def test_history_rides_messages_only_the_cached_prefix_stays_stable(tmp_path: Path) -> None:
    # FR-011/SC-006: history must not vary the system prompt or the toolset — the
    # cached prefix survives every turn; growth is bounded to 2×turns+1 short messages.
    from obi.radio.runner import compose_reply

    runtime, stub = _stub_runtime(tmp_path)
    compose_reply(
        runtime,
        request="q",
        caller="nono",
        tier="operator",
        call_id=6,
        live_calls=3,
        recorder=None,
    )
    runtime2, stub2 = _stub_runtime(tmp_path)
    full = tuple(Turn(request=f"q{n}", answer=f"a{n}, over") for n in range(8))
    compose_reply(
        runtime2,
        request="q9",
        caller="nono",
        tier="operator",
        call_id=7,
        live_calls=3,
        recorder=None,
        history=full,
    )
    bare, seeded = stub.calls[0], stub2.calls[0]
    assert seeded["system"] == bare["system"]
    assert [t["name"] for t in seeded["tools"]] == [t["name"] for t in bare["tools"]]
    assert len(seeded["messages"]) == 2 * len(full) + 1


def test_third_party_hail_in_window_draws_no_rf(tmp_path: Path) -> None:
    # Feature 114, field 2026-08-29 (radio.db call 118): with a window still open after
    # obi's answer, MIRA HAILED VERTIGO on the shared channel and obi answered it ("Copy
    # Mira, over."). End to end through the runner — capture, trigger, tracker, machine,
    # TX gate — the hail must key nothing at all and release the channel.
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (30, True), (940, False)
    )
    transcripts = [
        _tr("obi obi this is nono"),
        _tr("is the mountain link up"),
        _tr("vertigo vertigo vertigo this is mira"),
    ]
    composed: list[str] = []

    def composer(request: str, **_kw: Any) -> str:
        composed.append(request)
        return "link is up, over"

    h = _harness(
        tmp_path, script, transcripts, composer=composer, known_callers=["Mira", "Vertigo"]
    )
    # The caller's own question still composes; the hail to Vertigo never reaches the model.
    assert composed == ["is the mountain link up"]
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]
    assert h.calls() == [("third_party", "third_party")]
    assert [row.trigger_reason for row in h.rows()][-1] == "third_party_traffic"


def test_third_party_answer_to_a_hail_draws_no_rf(tmp_path: Path) -> None:
    # The incident's SECOND misfire (tx 3228, "Go for Vertigo."): the station whose call
    # it was, announcing itself to somebody else. obi never hails anyone, so this is
    # never for obi — even though the speaker is the call's own caller.
    script = _script(
        (10, False), (20, True), (60, False), (20, True), (60, False), (30, True), (940, False)
    )
    transcripts = [
        _tr("obi obi this is vertigo"),
        _tr("is the mountain link up"),
        _tr("go for vertigo"),
    ]

    def composer(request: str, **_kw: Any) -> str:
        return "link is up, over"

    h = _harness(
        tmp_path, script, transcripts, composer=composer, known_callers=["Mira", "Vertigo"]
    )
    assert h.outbound() == [("ack", "transmitted"), ("answer", "transmitted")]
    assert h.calls() == [("third_party", "third_party")]
