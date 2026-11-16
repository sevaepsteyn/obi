"""Integration (feature 088, US1): the RX pipeline through the runner with fake hardware.

FakeAudioSource scripted blocks → Segmenter → FakeTranscriber → radiostore rows + clips.
Covers the transcription gate mapping, hardware vanish/return mid-run (FR-034/US1-AS4),
split-brain (audio present, serial absent ⇒ RX-only), and audio-absent retry. No real
device, no sounddevice import, no clock dependence beyond injected sleep (FR-036).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from obi.core import radiostore
from obi.core.config import RadioSettings
from obi.radio.capture import BLOCK_MS
from obi.radio.devices import DevicesReport, HalfReport
from obi.radio.runner import RadioRunner
from tests.fakes import FakeTranscriber

RATE = 16_000  # capture directly at whisper rate so the resample path no-ops (no scipy)
BLOCK = RATE * BLOCK_MS // 1000
BLOCK_US = BLOCK_MS * 1000


def _silence() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.float32)


# Broadband, seeded: real speech is broadband — a pure sine would (correctly) be gated
# as a key-up beep by classify_non_speech before ever reaching the fake transcriber.
_SPEECH_BLOCK = np.random.default_rng(4242).normal(0, 0.15, RATE * BLOCK_MS // 1000).astype(
    np.float32
)


def _speech() -> np.ndarray:
    return _SPEECH_BLOCK


def _timeline(*bursts: int, start_us: int = 0) -> list[tuple[int, np.ndarray]]:
    """Alternating silence/speech runs (counts in blocks), starting silent."""

    blocks: list[np.ndarray] = []
    speaking = False
    for count in bursts:
        blocks.extend([_speech() if speaking else _silence()] * count)
        speaking = not speaking
    return [(start_us + i * BLOCK_US, b) for i, b in enumerate(blocks)]


@dataclass
class FakeAudioSource:
    """Scripted AudioSource: yields (t_us, block) items; an Exception item raises."""

    script: list[Any]
    stopped: bool = False

    def blocks(self) -> Any:
        for item in self.script:
            if isinstance(item, Exception):
                raise item
            yield item

    def stop(self) -> None:
        self.stopped = True


@dataclass
class RecordingNotifier:
    notes: list[str] = field(default_factory=list)

    def deliver_status(self, text: str) -> None:
        self.notes.append(text)


def _report(*, audio: bool = True, serial: bool = True) -> DevicesReport:
    return DevicesReport(
        audio=HalfReport(present=audio, detail="audio" if audio else "audio absent"),
        serial=HalfReport(present=serial, detail="serial" if serial else "serial absent"),
    )


def _tr(text: str, gate: str = "ok") -> Any:
    from obi.voice.stt import TranscriptionResult

    return TranscriptionResult(text=text, gate=gate, seconds=1.0, level=0.2)  # type: ignore[arg-type]


def _settings(tmp_path: Path) -> RadioSettings:
    return RadioSettings(
        sample_rate=RATE,
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
    )


def _run(
    tmp_path: Path,
    sources: list[Any],
    transcriber: FakeTranscriber,
    *,
    probe_reports: list[DevicesReport] | None = None,
    notifier: RecordingNotifier | None = None,
) -> tuple[list[radiostore.Transmission], list[float]]:
    """Drive the runner until the source factory script is exhausted; return rows + sleeps."""

    sleeps: list[float] = []
    remaining = list(sources)
    reports = list(probe_reports) if probe_reports is not None else None

    runner = RadioRunner(
        settings=_settings(tmp_path),
        source_factory=lambda: remaining.pop(0) if remaining else None,
        transcriber=transcriber,
        probe_fn=(lambda: reports.pop(0) if reports else _report())
        if reports is not None
        else _report,
        notifier=notifier,
        sleep=sleeps.append,
    )
    runner.run()
    conn = radiostore.connect_readonly(tmp_path / "radio.db")
    rows = radiostore.query_window(conn, limit=100, include_self=True)
    conn.close()
    return rows, sleeps


def test_speech_becomes_a_stored_transmission_with_clip(tmp_path: Path) -> None:
    source = FakeAudioSource(_timeline(10, 20, 40))
    transcriber = FakeTranscriber(results=[_tr("obi obi this is nono")])
    rows, _ = _run(tmp_path, [source], transcriber)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "rx"
    assert row.transcript == "obi obi this is nono"
    assert row.stt_gate == "ok"
    assert row.duration_ms >= 900
    assert row.clip_path is not None
    clip = tmp_path / "clips" / row.clip_path
    assert clip.exists() and clip.stat().st_size > 1000
    assert transcriber.heard, "the transcriber was fed the segment audio"


def test_gated_transcription_is_flagged_unintelligible(tmp_path: Path) -> None:
    source = FakeAudioSource(_timeline(4, 20, 40))
    transcriber = FakeTranscriber(results=[_tr("", gate="no_speech")])
    rows, _ = _run(tmp_path, [source], transcriber)
    assert len(rows) == 1
    assert rows[0].stt_gate == "unintelligible"
    assert rows[0].transcript == ""


def test_transcriber_exception_never_loses_the_transmission(tmp_path: Path) -> None:
    class ExplodingTranscriber:
        def transcribe(self, audio: Any) -> Any:
            raise RuntimeError("mlx blew up")

    source = FakeAudioSource(_timeline(4, 20, 40))
    sleeps: list[float] = []
    remaining: list[Any] = [source]
    runner = RadioRunner(
        settings=_settings(tmp_path),
        source_factory=lambda: remaining.pop(0) if remaining else None,
        transcriber=ExplodingTranscriber(),
        probe_fn=_report,
        notifier=None,
        sleep=sleeps.append,
    )
    runner.run()  # must not raise
    conn = radiostore.connect_readonly(tmp_path / "radio.db")
    rows = radiostore.query_window(conn, limit=10)
    conn.close()
    assert len(rows) == 1 and rows[0].stt_gate == "unintelligible"


def test_vanish_and_return_degrades_and_resumes(tmp_path: Path) -> None:
    # Source 1 delivers one transmission then dies (USB vanished); source 2 delivers another.
    dying = FakeAudioSource(_timeline(4, 20, 40) + [RuntimeError("device vanished")])
    returned = FakeAudioSource(_timeline(4, 20, 40, start_us=60 * 1_000_000))
    transcriber = FakeTranscriber(results=[_tr("first"), _tr("second")])
    notifier = RecordingNotifier()
    rows, sleeps = _run(tmp_path, [dying, returned], transcriber, notifier=notifier)
    assert [r.transcript for r in rows] == ["first", "second"]
    assert sleeps, "the runner backed off between source lifetimes instead of hot-looping"
    assert any("radio" in n.lower() or "capture" in n.lower() for n in notifier.notes)


def test_audio_absent_retries_then_captures(tmp_path: Path) -> None:
    source = FakeAudioSource(_timeline(4, 20, 40))
    transcriber = FakeTranscriber(results=[_tr("late but heard")])
    rows, sleeps = _run(
        tmp_path,
        [source],
        transcriber,
        probe_reports=[_report(audio=False), _report(), _report()],
    )
    assert [r.transcript for r in rows] == ["late but heard"]
    assert sleeps, "absent hardware waited instead of spinning"


def test_split_brain_serial_absent_still_captures_rx_only(tmp_path: Path) -> None:
    source = FakeAudioSource(_timeline(4, 20, 40))
    transcriber = FakeTranscriber(results=[_tr("rx only")])
    rows, _ = _run(
        tmp_path,
        [source],
        transcriber,
        probe_reports=[_report(serial=False), _report(serial=False)],
    )
    assert [r.transcript for r in rows] == ["rx only"]


def test_vocab_prompt_carries_stations_prowords_and_sites() -> None:
    from obi.radio.runner import build_vocab_prompt

    settings = RadioSettings(
        operator_handles=["Mira"],
        known_callers=["ZFX", "Za-zu"],
        vocab_terms=["NOC", "manzanita", "mountain", "tamarack"],
    )
    prompt = build_vocab_prompt(settings)
    for required in ("obi", "mira", "zfx", "za-zu", "radio check", "say again",
                     "noc", "manzanita", "mountain", "tamarack"):
        assert required in prompt.lower(), required
    # Dialogue-shaped (field 2026-08-20): the vocabulary appears as sample TRAFFIC in
    # spoken positions — a hail, a status question, a link report — not a term list.
    assert "Obi, Obi, this is Mira." in prompt
    assert "What's the status of the NOC?" in prompt
    assert "The manzanita to mountain link is up, over." in prompt
    # No vocab_terms ⇒ no dangling site sentences.
    bare = build_vocab_prompt(RadioSettings())
    assert "Sites and gear" not in bare and "What's the status" not in bare


def test_retained_clip_is_the_audio_as_received_not_whisper_s_meal(tmp_path: Path) -> None:
    """The clip is the channel RECORD; STT conditioning must not leak into it.

    Field 2026-08-21: replayed clips sounded distorted next to the operator's own handheld,
    because the stored audio was ``normalize_for_stt`` output — lifted toward -6 dBFS and
    hard-clipped at the top. Quiet speech is where the two diverge most, so that is what
    this drives.
    """

    import wave

    quiet = np.random.default_rng(99).normal(0, 0.03, BLOCK).astype(np.float32)
    script = [
        (i * BLOCK_US, _silence() if i < 10 else quiet) for i in range(10 + 20)
    ] + [((10 + 20 + i) * BLOCK_US, _silence()) for i in range(40)]
    rows, _ = _run(
        tmp_path, [FakeAudioSource(script)], FakeTranscriber(results=[_tr("quiet caller")])
    )
    row = next(r for r in rows if r.kind == "rx")
    assert row.clip_path

    with wave.open(str(tmp_path / "clips" / row.clip_path), "rb") as reader:
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    clip_rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64))))) / 32768.0

    # As received (~0.03), NOT lifted toward the -6 dBFS (0.5) STT target.
    assert clip_rms < 0.06, f"clip was normalized for STT: rms {clip_rms:.3f}"
    assert clip_rms > 0.015, f"clip lost the signal: rms {clip_rms:.3f}"
    # And nothing in it is pinned at full scale by our own processing.
    assert np.max(np.abs(pcm)) < 32000


def test_vocab_prompt_stays_inside_the_whisper_prompt_window() -> None:
    # Whisper keeps only the last ~223 tokens of initial_prompt and drops the rest from the
    # FRONT — where the hail-shaped priming lives. A 61-name roster (the IT Slack roster,
    # 2026-08-21) overflowed it, so the builder fits to a budget instead.
    from obi.radio.runner import _VOCAB_PROMPT_BUDGET_CHARS, build_vocab_prompt

    settings = RadioSettings(
        operator_handles=["Mira"],
        known_callers=[f"Station{n:02d}" for n in range(61)],
        vocab_terms=["NOC", "manzanita", "mountain", *[f"site{n:02d}" for n in range(30)]],
    )
    prompt = build_vocab_prompt(settings)
    assert len(prompt) <= _VOCAB_PROMPT_BUDGET_CHARS
    # The head — the part Whisper would have dropped — survives intact.
    assert prompt.startswith("Obi, Obi, this is Mira.")
    assert "radio check" in prompt.lower()


def test_vocab_prompt_never_lets_the_roster_crowd_out_the_site_terms() -> None:
    # Greedy in-order filling gave the roster everything and the site vocabulary nothing —
    # trading the callsign mis-hearings for the "NOC" → North ones. Both lists get a share.
    from obi.radio.runner import build_vocab_prompt

    settings = RadioSettings(
        operator_handles=["Mira"],
        known_callers=[f"VeryLongStationName{n:02d}" for n in range(60)],
        vocab_terms=["NOC", "manzanita", "mountain", "tamarack", "chestnut", "gunter"],
    )
    prompt = build_vocab_prompt(settings)
    assert "Stations on channel:" in prompt
    assert "Sites and gear:" in prompt
    for term in ("tamarack", "chestnut", "gunter"):
        assert term in prompt, term


def test_vocab_prompt_keeps_short_lists_whole() -> None:
    from obi.radio.runner import build_vocab_prompt

    settings = RadioSettings(
        operator_handles=["Mira"],
        known_callers=["ZFX", "Za-zu", "Tabe"],
        vocab_terms=["NOC", "manzanita", "mountain", "tamarack", "chestnut"],
    )
    prompt = build_vocab_prompt(settings)
    for name in ("ZFX", "Za-zu", "Tabe", "tamarack", "chestnut"):
        assert name in prompt, name


def test_normalize_for_stt_lifts_quiet_speech_click_robustly() -> None:
    import numpy as np

    from obi.radio.runner import normalize_for_stt

    # Quiet speech (5% full scale) with a 5 ms near-full-scale PTT click at the front —
    # the click is <0.5% of the samples, so the percentile reference ignores it.
    speech = (0.05 * np.sin(np.linspace(0, 700 * np.pi, RATE * 2))).astype(np.float32)
    speech[: RATE * 5 // 1000] = 0.98
    lifted = normalize_for_stt(speech)
    body = lifted[RATE // 2 :]  # away from the click
    assert 0.3 < float(np.abs(body).max()) <= 1.0  # speech lifted toward -6 dBFS
    assert float(np.abs(lifted[:10]).max()) == 1.0  # the click clips, not the speech

    # Healthy audio stays essentially untouched (never attenuated, at most a hair of
    # lift — a sine's 99.5th percentile sits just under its peak), silence stays silence.
    healthy = (0.5 * np.sin(np.linspace(0, 700 * np.pi, RATE))).astype(np.float32)
    out = normalize_for_stt(healthy)
    assert float(np.abs(out).max()) < 0.52
    silence = np.zeros(RATE, dtype=np.float32)
    assert normalize_for_stt(silence) is silence


def test_repetition_hallucination_is_gated_unintelligible(tmp_path: Path) -> None:
    from obi.radio.runner import is_repetition_hallucination

    assert is_repetition_hallucination("asc asc asc asc asc")
    assert is_repetition_hallucination("The The the")
    assert not is_repetition_hallucination("radio check")
    assert not is_repetition_hallucination("obi obi mira")  # two distinct tokens
    assert not is_repetition_hallucination("clear")  # short real traffic

    source = FakeAudioSource(_timeline(4, 20, 40))
    transcriber = FakeTranscriber(results=[_tr("asc asc asc asc asc asc")])
    rows, _ = _run(tmp_path, [source], transcriber)
    assert len(rows) == 1
    assert rows[0].stt_gate == "unintelligible"  # stored, flagged, never a trigger/request


def test_classify_non_speech_gates_clicks_and_beeps_but_not_words() -> None:
    import numpy as np

    from obi.radio.runner import classify_non_speech

    rng = np.random.default_rng(88)
    quiet = (rng.normal(0, 0.001, RATE) if False else np.zeros(RATE)).astype(np.float32)

    # Bare PTT key: 5 ms click + keyed ambient (-52 dBFS-ish noise) for a second.
    click = (rng.normal(0, 0.0025, RATE)).astype(np.float32)  # ambient floor
    click[: RATE * 5 // 1000] = 0.98
    assert classify_non_speech(click) == "carrier_only"

    # Key-up with the roger beep: click + 300 ms pure 1.5 kHz tone.
    beep = (rng.normal(0, 0.0025, RATE)).astype(np.float32)
    beep[: RATE * 5 // 1000] = 0.98
    t = np.arange(RATE * 300 // 1000, dtype=np.float32)
    beep[RATE // 10 : RATE // 10 + t.size] = (0.3 * np.sin(2 * np.pi * 1500 * t / RATE)).astype(
        np.float32
    )
    assert classify_non_speech(beep) == "key_beep"

    # A single short word ("clear"): 400 ms of BROADBAND voiced-ish energy passes.
    word = (rng.normal(0, 0.0025, RATE)).astype(np.float32)
    burst = rng.normal(0, 0.15, RATE * 400 // 1000).astype(np.float32)
    word[RATE // 4 : RATE // 4 + burst.size] = burst
    assert classify_non_speech(word) is None

    # Real speech shape: several broadband syllable bursts across two seconds.
    speech = (rng.normal(0, 0.0025, RATE * 2)).astype(np.float32)
    for start_ms in (200, 600, 1000, 1500):
        seg = rng.normal(0, 0.12, RATE * 180 // 1000).astype(np.float32)
        start = RATE * start_ms // 1000
        speech[start : start + seg.size] = seg
    assert classify_non_speech(speech) is None

    assert classify_non_speech(quiet) == "carrier_only"  # squelch crash, nothing voiced


def test_non_speech_segments_skip_stt_entirely(tmp_path: Path) -> None:
    # A keyed-but-silent transmission long enough to segment: a click block plus keyed
    # ambient (peaks above squelch to hold the segment open, frame rms below the voiced
    # threshold — the criticalrf keyed-room-ambient shape). STT must never be consulted.
    rng = np.random.default_rng(7)
    click_block = rng.normal(0, 0.005, BLOCK).astype(np.float32)
    click_block[: RATE * 5 // 1000] = 0.98
    quiet_carrier = rng.normal(0, 0.005, BLOCK).astype(np.float32)
    blocks: list[tuple[int, np.ndarray]] = []
    silence = np.zeros(BLOCK, dtype=np.float32)
    for i in range(4):
        blocks.append((i * BLOCK_US, silence))
    blocks.append((4 * BLOCK_US, click_block))
    for i in range(5, 13):
        blocks.append((i * BLOCK_US, quiet_carrier))
    for i in range(13, 53):
        blocks.append((i * BLOCK_US, silence))

    source = FakeAudioSource(blocks)
    transcriber = FakeTranscriber(results=[_tr("should never be consulted")])
    rows, _ = _run(tmp_path, [source], transcriber)
    assert len(rows) == 1
    assert rows[0].stt_gate == "no_speech"
    assert rows[0].transcript == ""
    assert transcriber.heard == []  # whisper never ran


# --- L2: the two-tone roger beep (measured 2026-08-19: 1570 Hz + 1050/1320 Hz notes) --------


def _tone(freq: float, ms: int, amp: float = 0.3) -> np.ndarray:
    t = np.arange(RATE * ms // 1000, dtype=np.float32)
    return (amp * np.sin(2 * np.pi * freq * t / RATE)).astype(np.float32)


def test_classify_two_tone_roger_beep_sequential() -> None:
    # The field beep: two ~180 ms notes back to back (voiced ≈ 360 ms, measured 260-490).
    # Single-band energy ≈ 0.5 per note — the OLD single-band test alone passed this.
    from obi.radio.runner import classify_non_speech

    rng = np.random.default_rng(3)
    audio = rng.normal(0, 0.0025, RATE).astype(np.float32)  # keyed ambient floor
    beep = np.concatenate([_tone(1570, 180), _tone(1320, 180)])
    audio[RATE // 10 : RATE // 10 + beep.size] = beep
    assert classify_non_speech(audio) == "key_beep"


def test_classify_two_tone_roger_beep_simultaneous() -> None:
    from obi.radio.runner import classify_non_speech

    rng = np.random.default_rng(3)
    audio = rng.normal(0, 0.0025, RATE).astype(np.float32)
    dual = (_tone(1570, 300) + _tone(1050, 300)) / 2
    audio[RATE // 10 : RATE // 10 + dual.size] = dual
    assert classify_non_speech(audio) == "key_beep"


def test_long_alternating_tones_are_not_gated() -> None:
    # The voiced-duration guard: anything tonal LONGER than any real key beep passes to
    # whisper rather than being silently dropped (fail toward transcribing).
    from obi.radio.runner import classify_non_speech

    rng = np.random.default_rng(3)
    audio = rng.normal(0, 0.0025, RATE * 2).astype(np.float32)
    long_tones = np.concatenate([_tone(1570, 300), _tone(1320, 300)] * 2)  # 1.2 s voiced
    audio[RATE // 10 : RATE // 10 + long_tones.size] = long_tones
    assert classify_non_speech(audio) is None


def test_synthetic_short_word_is_not_gated() -> None:
    # A terse real word (~400 ms voiced). Real speech is never spectrally stationary —
    # pitch drifts, formants move, consonants add noise (measured real clips: top2
    # 0.17-0.35). NOTE deliberately excluded: a perfectly STEADY harmonic stack is
    # physically a multi-tone chord and correctly gates as a beep; only realistic,
    # time-varying speech must pass.
    from obi.radio.runner import classify_non_speech

    rng = np.random.default_rng(3)
    audio = rng.normal(0, 0.0025, RATE).astype(np.float32)
    n = RATE * 400 // 1000
    t = np.arange(n, dtype=np.float32) / RATE
    pitch = 120.0 + 60.0 * t / t[-1]  # pitch glides 120 -> 180 Hz across the word
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    word = np.zeros(n, dtype=np.float32)
    for k in range(1, 17):
        f_k = 150.0 * k  # mid-glide harmonic frequency
        # Two vocal-tract formants (F1~500, F2~1500) — THIS is what spreads speech
        # energy across many bands; a formant-less 1/k stack is physically a chord.
        gain = 1.0 / (1 + ((f_k - 500) / 300) ** 2) + 1.0 / (1 + ((f_k - 1500) / 400) ** 2)
        word += (0.15 * gain) * np.sin(k * phase).astype(np.float32)
    word += rng.normal(0, 0.03, n).astype(np.float32)  # breath/consonant noise
    word *= np.hanning(n).astype(np.float32)  # amplitude envelope
    audio[RATE // 4 : RATE // 4 + n] = word
    assert classify_non_speech(audio) is None


def test_neighbor_two_tone_beep_skips_stt(tmp_path: Path) -> None:
    # Runner-level: ANOTHER station's roger beep arrives as genuine rx — classified
    # no_speech, whisper never consulted.
    rng = np.random.default_rng(9)
    beep = np.concatenate([_tone(1570, 180), _tone(1320, 180)])
    blocks: list[tuple[int, np.ndarray]] = []
    silence = np.zeros(BLOCK, dtype=np.float32)
    for i in range(4):
        blocks.append((i * BLOCK_US, silence))
    beep_padded = np.concatenate([beep, rng.normal(0, 0.0025, 10 * BLOCK - beep.size)]).astype(
        np.float32
    )
    for i in range(10):
        blocks.append(((4 + i) * BLOCK_US, beep_padded[i * BLOCK : (i + 1) * BLOCK]))
    for i in range(14, 54):
        blocks.append((i * BLOCK_US, silence))
    source = FakeAudioSource(blocks)
    transcriber = FakeTranscriber(results=[_tr("should never be consulted")])
    rows, _ = _run(tmp_path, [source], transcriber)
    assert len(rows) == 1
    assert rows[0].stt_gate == "no_speech"
    assert transcriber.heard == []


def test_runner_attaches_the_gate_monitor_to_the_capture_source(tmp_path: Path) -> None:
    # Half-duplex hardening (field 2026-08-20: obi keyed over the tail of a caller's
    # transmission). The clear-channel gate's monitor must be fed by the CAPTURE side in
    # real time — never by the consumer thread, which lags behind wall clock whenever
    # whisper is running and can show the gate a stale picture of the channel. The runner
    # attaches its monitor to any source that supports it, and the Segmenter keeps a
    # PRIVATE monitor so the shared one is capture-fed only.
    attached: list[Any] = []

    @dataclass
    class AttachableSource(FakeAudioSource):
        def attach_monitor(self, monitor: Any) -> None:
            attached.append(monitor)

    source = AttachableSource(_timeline(4, 20, 40))
    transcriber = FakeTranscriber(results=[_tr("hello")])
    sleeps: list[float] = []
    remaining: list[Any] = [source]
    runner = RadioRunner(
        settings=_settings(tmp_path),
        source_factory=lambda: remaining.pop(0) if remaining else None,
        transcriber=transcriber,
        probe_fn=_report,
        notifier=None,
        sleep=sleeps.append,
    )
    runner.run()
    assert attached == [runner.monitor]
    assert runner._segmenter.monitor is not runner.monitor  # gate data ≠ consumer data
