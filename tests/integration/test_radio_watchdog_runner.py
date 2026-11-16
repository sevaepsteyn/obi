"""Integration (feature 113): the capture watchdog through the runner with fake hardware.

FakeAudioSource scripted blocks → detectors → supervisor → reopen / exit-requested,
with W4 assertions (log + audit + Slack note on every fire) and the W5 disabled
contract. No real device, no sounddevice import, no clock dependence beyond the wall
clock the supervisor is handed (tests zero the spacing floor).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import numpy as np

from obi.core import auditstore, radiostore
from obi.core.audit import AuditRecorder
from obi.core.config import AuditSettings, RadioSettings
from obi.radio.capture import BLOCK_MS
from obi.radio.devices import DevicesReport, HalfReport
from obi.radio.runner import RadioRunner
from tests.fakes import FakeTranscriber

RATE = 16_000  # capture at whisper rate so the resample path no-ops (no scipy)
BLOCK = RATE * BLOCK_MS // 1000
BLOCK_US = BLOCK_MS * 1000


def _digital_silence() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.float32)  # −120 dBFS: the zombie-stream signature


_RNG = np.random.default_rng(1313)
_NOISE_FLOOR_BLOCK = _RNG.normal(0, 1e-4, BLOCK).astype(np.float32)  # ≈ −80 dBFS rms
_SPEECH_BLOCK = _RNG.normal(0, 0.15, BLOCK).astype(np.float32)  # broadband, peak ≈ −4 dBFS


def _blocks(kind: np.ndarray, count: int, *, start_us: int = 0) -> list[Any]:
    return [(start_us + i * BLOCK_US, kind) for i in range(count)]


def _burst(
    *, speech: np.ndarray, start_us: int, lead: int = 4, talk: int = 20, tail: int = 40
) -> list[Any]:
    """silence, speech, silence — one transmission the Segmenter will close and emit.

    The quiet is a live codec's NOISE FLOOR (≈ −80 dBFS), not digital zeros — real
    silence never flatlines, and these bursts must never look like a zombie stream."""

    line = [_NOISE_FLOOR_BLOCK] * lead + [speech] * talk + [_NOISE_FLOOR_BLOCK] * tail
    return [(start_us + i * BLOCK_US, b) for i, b in enumerate(line)]


@dataclass
class FakeAudioSource:
    """Scripted AudioSource; items are (t_us, block), an Exception, or a callable."""

    script: list[Any]
    stopped: bool = False

    def blocks(self) -> Any:
        for item in self.script:
            if self.stopped:
                return
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item()
                continue
            yield item

    def stop(self) -> None:
        self.stopped = True


@dataclass
class RecordingNotifier:
    notes: list[str] = dc_field(default_factory=list)

    def deliver_status(self, text: str) -> None:
        self.notes.append(text)


def _report() -> DevicesReport:
    return DevicesReport(
        audio=HalfReport(present=True, detail="audio"),
        serial=HalfReport(present=True, detail="serial"),
    )


def _tr(text: str) -> Any:
    from obi.voice.stt import TranscriptionResult

    return TranscriptionResult(text=text, gate="ok", seconds=1.0, level=0.2)  # type: ignore[arg-type]


def _settings(tmp_path: Path, **overrides: Any) -> RadioSettings:
    return RadioSettings(
        sample_rate=RATE,
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
        **overrides,
    )


@dataclass
class Harness:
    runner: RadioRunner
    notifier: RecordingNotifier
    logs: list[str]
    audit_db: Path
    tmp_path: Path

    def rows(self) -> list[radiostore.Transmission]:
        conn = radiostore.connect_readonly(self.tmp_path / "radio.db")
        try:
            return radiostore.query_window(conn, limit=100, include_self=True)
        finally:
            conn.close()

    def watchdog_audit_events(self) -> list[dict[str, Any]]:
        if not self.audit_db.exists():
            return []  # no event was ever recorded — the store is created lazily
        conn = auditstore.connect_readonly(self.audit_db)
        try:
            events, _total = auditstore.query_events(
                conn, sources=["radio"], limit=50, include_payload=True
            )
        finally:
            conn.close()
        return [e for e in events if e["session_id"] == "radio-watchdog"]


def _harness(
    tmp_path: Path, sources: list[Any], settings: RadioSettings, *, results: int = 4
) -> Harness:
    notifier = RecordingNotifier()
    logs: list[str] = []
    audit_db = tmp_path / "audit.db"
    remaining = list(sources)
    runner = RadioRunner(
        settings=settings,
        source_factory=lambda: remaining.pop(0) if remaining else None,
        transcriber=FakeTranscriber(results=[_tr(f"heard {i}") for i in range(results)]),
        probe_fn=_report,
        notifier=notifier,
        sleep=lambda _s: None,
        log=logs.append,
        recorder=AuditRecorder(AuditSettings(db_path=audit_db)),
    )
    return Harness(runner, notifier, logs, audit_db, tmp_path)


# -- US1: dead stream (flatline) --------------------------------------------------------


def test_flatline_fires_reopens_and_hearing_resumes(tmp_path: Path) -> None:
    # Source 1 is a zombie stream: pure digital silence past the flatline window.
    # The watchdog must stop it; source 2 (a live stream with one transmission) proves
    # the reopened path hears again. W4: exactly one note, one audit event, a log line.
    settings = _settings(tmp_path, watchdog_flatline_s=1, watchdog_floor_s=0)
    zombie = FakeAudioSource(_blocks(_digital_silence(), 30))
    live = FakeAudioSource(_burst(speech=_SPEECH_BLOCK, start_us=60 * 1_000_000))
    h = _harness(tmp_path, [zombie, live], settings)
    h.runner.run()

    assert zombie.stopped, "the zombie source was stopped by the watchdog"
    assert [r.transcript for r in h.rows() if r.kind == "rx"] == ["heard 0"]
    assert h.runner.watchdog_exit_requested is False
    flatline_notes = [n for n in h.notifier.notes if "flatline" in n]
    assert len(flatline_notes) == 1, h.notifier.notes
    assert "reopen" in flatline_notes[0].lower()
    assert any("watchdog flatline" in line for line in h.logs)
    events = h.watchdog_audit_events()
    assert len(events) == 1
    assert "flatline" in events[0]["summary"] and "reopen" in events[0]["summary"]


def test_repeated_flatline_escalates_to_exit_for_relaunch(tmp_path: Path) -> None:
    # Three zombie sources in one escalation window: reopen, reopen, then EXIT (W6).
    settings = _settings(
        tmp_path,
        watchdog_flatline_s=1,
        watchdog_floor_s=0,
        watchdog_max_reopens=2,
        watchdog_escalation_s=3600,
    )
    zombies = [FakeAudioSource(_blocks(_digital_silence(), 30)) for _ in range(3)]
    sentinel = FakeAudioSource(_blocks(_digital_silence(), 5))
    h = _harness(tmp_path, [*zombies, sentinel], settings)
    h.runner.run()

    assert h.runner.watchdog_exit_requested is True
    assert not sentinel.stopped and h.runner is not None
    # The sentinel was never consumed: run() ended at the exit verdict.
    assert all(z.stopped for z in zombies)
    assert len([n for n in h.notifier.notes if "flatline" in n]) == 3
    assert any("relaunch" in n for n in h.notifier.notes)
    summaries = [e["summary"] for e in h.watchdog_audit_events()]
    assert sum("exit" in s for s in summaries) == 1


def test_quiet_but_real_noise_floor_never_fires(tmp_path: Path) -> None:
    # −80 dBFS is a healthy codec's quiet night (US1-AS3): hours of it must not fire.
    settings = _settings(tmp_path, watchdog_flatline_s=1, watchdog_floor_s=0)
    quiet = FakeAudioSource(_blocks(_NOISE_FLOOR_BLOCK, 60))
    h = _harness(tmp_path, [quiet], settings)
    h.runner.run()

    assert h.notifier.notes == []
    assert h.watchdog_audit_events() == []
    assert h.runner.watchdog_exit_requested is False


def test_disabled_watchdog_is_pre_113_behavior(tmp_path: Path) -> None:
    # W5: watchdog=false wires nothing — the zombie stream runs to script end
    # untouched, and the disabled state is visible at startup.
    settings = _settings(tmp_path, watchdog=False, watchdog_flatline_s=1)
    zombie = FakeAudioSource(_blocks(_digital_silence(), 30))
    live = FakeAudioSource(_burst(speech=_SPEECH_BLOCK, start_us=60 * 1_000_000))
    h = _harness(tmp_path, [zombie, live], settings)
    h.runner.run()

    assert not zombie.stopped
    assert h.notifier.notes == []
    assert h.watchdog_audit_events() == []
    assert any("watchdog disabled" in line for line in h.logs)
    # Both sources still consumed in order — the ordinary loop untouched.
    assert [r.transcript for r in h.rows() if r.kind == "rx"] == ["heard 0"]


def test_armed_watchdog_logs_its_thresholds_at_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    h = _harness(tmp_path, [], settings)
    h.runner.run()
    armed = [line for line in h.logs if "watchdog armed" in line]
    assert len(armed) == 1
    assert "120s" in armed[0] and "-10.0" in armed[0]


# -- US2: capture fade ------------------------------------------------------------------


def _faded_speech() -> np.ndarray:
    # Attenuated broadband speech: above squelch (−45), peak far below −10 dBFS —
    # the shape of the Aug 24–26 degraded sessions.
    return (_SPEECH_BLOCK * 0.02).astype(np.float32)  # peak ≈ −38, rms ≈ −40 dBFS


def _fade_settings(tmp_path: Path, **overrides: Any) -> RadioSettings:
    return _settings(
        tmp_path,
        watchdog_fade_window=4,
        watchdog_fade_min=3,
        watchdog_floor_s=0,
        **overrides,
    )


def test_fade_fires_after_min_segments_and_reopens(tmp_path: Path) -> None:
    script: list[Any] = []
    for i in range(3):
        script += _burst(speech=_faded_speech(), start_us=i * 10 * 1_000_000)
    faded = FakeAudioSource(script)
    live = FakeAudioSource(_burst(speech=_SPEECH_BLOCK, start_us=600 * 1_000_000))
    h = _harness(tmp_path, [faded, live], _fade_settings(tmp_path))
    h.runner.run()

    assert faded.stopped, "the faded source was reopened"
    fade_notes = [n for n in h.notifier.notes if "fade" in n]
    assert len(fade_notes) == 1 and "reopen" in fade_notes[0].lower()
    events = h.watchdog_audit_events()
    assert len(events) >= 1 and "fade" in events[0]["summary"]


def test_healthy_amplitude_speech_never_fires_fade(tmp_path: Path) -> None:
    script: list[Any] = []
    for i in range(4):
        script += _burst(speech=_SPEECH_BLOCK, start_us=i * 10 * 1_000_000)
    h = _harness(tmp_path, [FakeAudioSource(script)], _fade_settings(tmp_path))
    h.runner.run()
    assert [n for n in h.notifier.notes if "fade" in n] == []
    assert h.watchdog_audit_events() == []


def test_self_tx_bleed_never_votes_fade(tmp_path: Path) -> None:
    # W9: three faded bursts, every one inside a self-TX window ⇒ stored as kind=self,
    # the fade detector sees nothing, no fire even though fade_min would be met.
    h_ref: dict[str, RadioRunner] = {}

    def mark_self(start_us: int, end_us: int) -> Any:
        return lambda: h_ref["runner"].note_self_tx(start_us, end_us)

    script: list[Any] = []
    for i in range(3):
        base = i * 10 * 1_000_000
        script.append(mark_self(base, base + 70 * BLOCK_US))
        script += _burst(speech=_faded_speech(), start_us=base)
    h = _harness(tmp_path, [FakeAudioSource(script)], _fade_settings(tmp_path))
    h_ref["runner"] = h.runner
    h.runner.run()

    rows = h.rows()
    assert rows and all(r.kind == "self" for r in rows)
    assert h.notifier.notes == []
    assert h.watchdog_audit_events() == []


def test_fade_recovery_after_reopen_is_reported_once(tmp_path: Path) -> None:
    faded_script: list[Any] = []
    for i in range(3):
        faded_script += _burst(speech=_faded_speech(), start_us=i * 10 * 1_000_000)
    healthy_script: list[Any] = []
    for i in range(3):
        healthy_script += _burst(
            speech=_SPEECH_BLOCK, start_us=(100 + i * 10) * 1_000_000
        )
    h = _harness(
        tmp_path,
        [FakeAudioSource(faded_script), FakeAudioSource(healthy_script)],
        _fade_settings(tmp_path),
    )
    h.runner.run()

    recovered_logs = [line for line in h.logs if "fade" in line and "recovered" in line]
    assert len(recovered_logs) == 1, h.logs
    events = h.watchdog_audit_events()
    assert any("recovered" in e["summary"] for e in events)
