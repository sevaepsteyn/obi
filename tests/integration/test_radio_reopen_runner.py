"""Integration (feature 122): the preventive capture reopen through the runner.

Clones the 113 watchdog harness with two additions the reopen gate needs: the fake
source feeds each block's levels into the RUNNER's monitor (mimicking the real capture
callback — the quiet gate reads it) and advances a shared injectable clock (the due
check and clear_for compare against now). No real device, no sounddevice import.

Contract references R1–R10: specs/122-capture-reopen/contracts/capture-reopen.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from obi.core import auditstore, radiostore
from obi.core.audit import AuditRecorder
from obi.core.config import AuditSettings, RadioSettings
from obi.radio.capture import BLOCK_MS, dbfs
from obi.radio.devices import DevicesReport, HalfReport
from obi.radio.runner import RadioRunner
from tests.fakes import FakeTranscriber

RATE = 16_000
BLOCK = RATE * BLOCK_MS // 1000
BLOCK_US = BLOCK_MS * 1000
S = 1_000_000

_RNG = np.random.default_rng(122)
_NOISE = _RNG.normal(0, 1e-4, BLOCK).astype(np.float32)  # ≈ −80 dBFS: healthy quiet
_SPEECH = _RNG.normal(0, 0.15, BLOCK).astype(np.float32)  # peak ≈ −4 dBFS
_DIGITAL_SILENCE = np.zeros(BLOCK, dtype=np.float32)  # −120 dBFS: zombie stream


def _line(*runs: tuple[np.ndarray, int], start_us: int = 0) -> list[Any]:
    """Concatenate (block, count) runs into (t_us, block) items from ``start_us``."""

    items: list[Any] = []
    t = start_us
    for block, count in runs:
        for _ in range(count):
            items.append((t, block))
            t += BLOCK_US
    return items


def _burst(*, start_us: int, lead: int = 4, talk: int = 20, tail: int = 90) -> list[Any]:
    """noise, speech, long noise tail — one transmission plus the quiet gap the gate
    needs (2 s quiet + the ≤1 Hz gate throttle ⇒ the reopen lands ~2.3 s after the
    speech ends, a few blocks into the tail)."""

    return _line((_NOISE, lead), (_SPEECH, talk), (_NOISE, tail), start_us=start_us)


@dataclass
class ReopenSource:
    """Scripted AudioSource that feeds the runner monitor + clock like real capture.

    Items are (t_us, block), an Exception (raised), or a callable (run between blocks).
    """

    script: list[Any]
    clock: dict[str, int]
    name: str = "src"
    events: list[str] | None = None
    stopped: bool = False
    _monitor: Any = None

    def attach_monitor(self, monitor: Any) -> None:
        self._monitor = monitor

    def blocks(self) -> Any:
        if self.events is not None:
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
                if self._monitor is not None:  # the real callback feeds in real time
                    peak, rms = dbfs(block)
                    self._monitor.add(t_us, peak_dbfs=peak, rms_dbfs=rms)
                self.clock["now"] = t_us + BLOCK_US  # "now" = end of the newest block
                yield item
        finally:
            if self.events is not None:
                self.events.append(f"closed {self.name}")

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
    # watchdog=False is the DEPLOYED state; the reopen must work without the detectors
    # (R10). floor_s=10 keeps fake timelines short (config validation floor).
    defaults: dict[str, Any] = dict(
        sample_rate=RATE,
        db_path=tmp_path / "radio.db",
        clips_dir=tmp_path / "clips",
        watchdog=False,
        capture_reopen_floor_s=10,
        capture_reopen_max_uptime_s=0,
    )
    defaults.update(overrides)
    return RadioSettings(**defaults)


@dataclass
class Harness:
    runner: RadioRunner
    notifier: RecordingNotifier
    logs: list[str]
    audit_db: Path
    tmp_path: Path
    clock: dict[str, int]

    def rows(self) -> list[radiostore.Transmission]:
        conn = radiostore.connect_readonly(self.tmp_path / "radio.db")
        try:
            return radiostore.query_window(conn, limit=100, include_self=True)
        finally:
            conn.close()

    def audit_events(self) -> list[dict[str, Any]]:
        if not self.audit_db.exists():
            return []
        conn = auditstore.connect_readonly(self.audit_db)
        try:
            events, _total = auditstore.query_events(
                conn, sources=["radio"], limit=50, include_payload=True
            )
        finally:
            conn.close()
        return [e for e in events if e["session_id"] == "radio-watchdog"]

    def preventive_events(self) -> list[dict[str, Any]]:
        return [e for e in self.audit_events() if "preventive" in e["summary"]]


def _harness(tmp_path: Path, scripts: list[list[Any]], settings: RadioSettings) -> Harness:
    clock = {"now": 0}
    notifier = RecordingNotifier()
    logs: list[str] = []
    audit_db = tmp_path / "audit.db"
    events: list[str] = []
    sources = [
        ReopenSource(script, clock, name=str(i + 1), events=events)
        for i, script in enumerate(scripts)
    ]
    runner = RadioRunner(
        settings=settings,
        source_factory=lambda: sources.pop(0) if sources else None,
        transcriber=FakeTranscriber(results=[_tr(f"heard {i}") for i in range(6)]),
        probe_fn=_report,
        notifier=notifier,
        sleep=lambda _s: None,
        log=logs.append,
        recorder=AuditRecorder(AuditSettings(db_path=audit_db)),
        now_us=lambda: clock["now"],
    )
    h = Harness(runner, notifier, logs, audit_db, tmp_path, clock)
    h.source_events = events  # type: ignore[attr-defined]
    h.sources_all = list(sources)  # type: ignore[attr-defined]
    return h


# -- US1: reopen after each transmission --------------------------------------------------


def test_reopen_after_foreign_segment_and_quiet_gap(tmp_path: Path) -> None:
    # A transmission closes, ~2 s of quiet follow ⇒ source 1 is stopped mid-tail,
    # source 2 is consumed, and the station still hears on the fresh stream. R5:
    # exactly one audit action, NO Slack note. Second reopen stays inside the floor.
    h = _harness(
        tmp_path,
        [_burst(start_us=0), _burst(start_us=10 * S, tail=40)],
        _settings(tmp_path),
    )
    s1, s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert s1.stopped, "the preventive reopen stopped source 1"
    assert not s2.stopped, "the floor deferred any second reopen"
    assert [r.transcript for r in h.rows() if r.kind == "rx"] == ["heard 0", "heard 1"]
    events = h.preventive_events()
    assert len(events) == 1
    assert events[0]["summary"] == "capture reopen: preventive"
    assert events[0]["payload"]["trigger"] == "after_tx"
    assert events[0]["payload"]["gap_ms"] is None  # first reopen: nothing to measure yet
    assert h.notifier.notes == []  # maintenance never alerts
    assert any("preventive reopen: closing capture" in line for line in h.logs)
    assert h.runner.watchdog_exit_requested is False


def test_reopen_after_own_tx_and_carried_self_window_marks_bleed(tmp_path: Path) -> None:
    # R6: obi unkeys (note_self_tx on a TX worker) ⇒ a reopen is armed; the fresh
    # source's first traffic lands INSIDE the carried exclusion window ⇒ stored as
    # kind="self", never reaching STT — even though the segmenter was rebuilt.
    h_ref: dict[str, RadioRunner] = {}
    script1 = _line((_NOISE, 45), start_us=0)
    script1.insert(44, lambda: h_ref["runner"].note_self_tx(1_500_000, 2_500_000))
    script2 = _burst(start_us=2_400_000, lead=2, tail=45)
    h = _harness(tmp_path, [script1, script2], _settings(tmp_path))
    h_ref["runner"] = h.runner
    s1, _s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert s1.stopped, "obi's own TX armed the reopen"
    rows = h.rows()
    assert rows and all(r.kind == "self" for r in rows)
    assert len(h.preventive_events()) == 1
    assert h.preventive_events()[0]["payload"]["trigger"] == "after_tx"


def test_reopen_defers_while_call_open(tmp_path: Path) -> None:
    # R3: an open exchange blocks execution for the whole script ⇒ the source runs to
    # its scripted end (never stopped) and nothing is audited.
    h = _harness(tmp_path, [_burst(start_us=0)], _settings(tmp_path))
    (s1,) = h.sources_all  # type: ignore[attr-defined]
    h.runner._call_id = 7  # an exchange is open the whole time
    h.runner.run()

    assert not s1.stopped
    assert h.preventive_events() == []
    assert any("waiting for a quiet gap" in line for line in h.logs)


def test_reopen_defers_while_tx_busy(tmp_path: Path) -> None:
    h = _harness(tmp_path, [_burst(start_us=0)], _settings(tmp_path))
    (s1,) = h.sources_all  # type: ignore[attr-defined]
    h.runner._tx = SimpleNamespace(busy=lambda: True)  # type: ignore[assignment]
    h.runner.run()

    assert not s1.stopped
    assert h.preventive_events() == []


def test_reopen_defers_while_channel_not_quiet(tmp_path: Path) -> None:
    # A segment closes (deep-quiet split) but another station keys up right after and
    # talks to the end of the script — clear_for never holds, the reopen never runs.
    script = _line((_NOISE, 4), (_SPEECH, 20), (_NOISE, 8), (_SPEECH, 80), start_us=0)
    h = _harness(tmp_path, [script], _settings(tmp_path))
    (s1,) = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert not s1.stopped
    assert h.preventive_events() == []


def test_floor_allows_only_one_reopen_for_close_transmissions(tmp_path: Path) -> None:
    # R4: two transmissions ~10 s apart under a 60 s floor ⇒ exactly one reopen.
    h = _harness(
        tmp_path,
        [_burst(start_us=0), _burst(start_us=10 * S)],
        _settings(tmp_path, capture_reopen_floor_s=60),
    )
    s1, s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert s1.stopped and not s2.stopped
    assert len(h.preventive_events()) == 1
    assert sum("waiting for a quiet gap" in line for line in h.logs) == 2  # once per due period


def test_generator_closed_before_next_source_opens(tmp_path: Path) -> None:
    # R9: the old InputStream generator is closed deterministically, before run()
    # builds the next source — never left to GC.
    h = _harness(
        tmp_path,
        [_burst(start_us=0), _line((_NOISE, 5), start_us=10 * S)],
        _settings(tmp_path),
    )
    h.runner.run()

    events = h.source_events  # type: ignore[attr-defined]
    assert events.index("closed 1") < events.index("opened 2")
    assert events == ["opened 1", "closed 1", "opened 2", "closed 2"]


def test_monitor_reset_and_gain_relog_on_reopened_source(tmp_path: Path) -> None:
    # R7: the fresh source starts with an EMPTY monitor (no pre-gap evidence for the
    # TX gate). R5: the host-gain verdict is re-logged after the reopen.
    seen: dict[str, int] = {}
    h = _harness(
        tmp_path,
        [_burst(start_us=0), _line((_NOISE, 5), start_us=10 * S)],
        _settings(tmp_path),
    )
    script2 = h.sources_all[1].script  # type: ignore[attr-defined]
    script2.insert(0, lambda: seen.update(blocks=len(h.runner.monitor.snapshot())))
    h.runner.run()

    assert seen["blocks"] == 0
    assert sum("host gain" in line for line in h.logs) >= 2  # startup + post-reopen


def test_watchdog_reopen_arms_the_shared_floor(tmp_path: Path) -> None:
    # R4 both directions: a flatline (detector) reopen anchors the floor, so a
    # preventive reopen due right after is deferred for the whole second source.
    settings = _settings(
        tmp_path,
        watchdog=True,
        watchdog_flatline_s=1,
        watchdog_floor_s=0,
        capture_reopen_floor_s=60,
    )
    h = _harness(
        tmp_path,
        [_line((_DIGITAL_SILENCE, 30), start_us=0), _burst(start_us=2 * S)],
        settings,
    )
    s1, s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert s1.stopped, "the watchdog stopped the zombie source"
    assert not s2.stopped, "the shared floor deferred the preventive reopen"
    assert h.preventive_events() == []
    assert any("flatline" in e["summary"] for e in h.audit_events())


def test_preventive_reopens_never_escalate_to_exit(tmp_path: Path) -> None:
    # R4: three preventive reopens inside the escalation window — far past
    # watchdog_max_reopens=2 — never touch the supervisor's fire history.
    settings = _settings(
        tmp_path,
        watchdog=True,
        watchdog_max_reopens=2,
        watchdog_floor_s=0,
    )
    h = _harness(
        tmp_path,
        [
            _burst(start_us=0),
            _burst(start_us=20 * S),
            _burst(start_us=40 * S),
            _line((_NOISE, 5), start_us=60 * S),
        ],
        settings,
    )
    h.runner.run()

    assert len(h.preventive_events()) == 3
    assert h.runner.watchdog_exit_requested is False
    assert not any("exit" in e["summary"] for e in h.audit_events())
    assert h.notifier.notes == []


def test_off_state_is_pre_122_behavior(tmp_path: Path) -> None:
    # R10: after_tx off + backstop 0 wires no trigger — the source runs to its
    # scripted end untouched, nothing audited, and the off state is visible once.
    settings = _settings(tmp_path, capture_reopen_after_tx=False)
    h = _harness(tmp_path, [_burst(start_us=0)], settings)
    (s1,) = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert not s1.stopped
    assert h.audit_events() == []
    assert h.notifier.notes == []
    assert any("preventive capture reopen disabled" in line for line in h.logs)
    assert [r.transcript for r in h.rows() if r.kind == "rx"] == ["heard 0"]


def test_startup_line_states_the_policy(tmp_path: Path) -> None:
    h = _harness(tmp_path, [], _settings(tmp_path, capture_reopen_max_uptime_s=7200))
    h.runner.run()
    lines = [line for line in h.logs if "preventive capture reopen" in line]
    assert lines == [
        "preventive capture reopen: after-tx on, floor 10s, quiet 2000ms, backstop 7200s"
    ]


# -- US2: max-uptime backstop -------------------------------------------------------------


def test_backstop_reopens_a_silent_channel(tmp_path: Path) -> None:
    # No traffic at all: past max_uptime the stream is still reopened at a quiet
    # moment, with the backstop trigger named in log and audit.
    settings = _settings(tmp_path, capture_reopen_max_uptime_s=60)
    h = _harness(
        tmp_path,
        [_line((_NOISE, 80), start_us=61 * S), _line((_NOISE, 5), start_us=70 * S)],
        settings,
    )
    s1, _s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert s1.stopped
    events = h.preventive_events()
    assert len(events) == 1
    assert events[0]["payload"]["trigger"] == "max_uptime"
    assert any("max_uptime" in line for line in h.logs if "closing capture" in line)


def test_backstop_zero_never_reopens_a_silent_channel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # max_uptime_s=0 (harness default)
    h = _harness(tmp_path, [_line((_NOISE, 80), start_us=61 * S)], settings)
    (s1,) = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert not s1.stopped
    assert h.preventive_events() == []


def test_backstop_works_with_after_tx_off(tmp_path: Path) -> None:
    # The operator's rollback configuration (research.md D1): per-TX off, backstop on.
    # A transmission arms nothing; only uptime triggers the reopen.
    settings = _settings(
        tmp_path, capture_reopen_after_tx=False, capture_reopen_max_uptime_s=60
    )
    h = _harness(
        tmp_path,
        [
            _burst(start_us=0, tail=40) + _line((_NOISE, 60), start_us=61 * S),
            _line((_NOISE, 5), start_us=70 * S),
        ],
        settings,
    )
    s1, _s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    assert s1.stopped
    events = h.preventive_events()
    assert len(events) == 1
    assert events[0]["payload"]["trigger"] == "max_uptime"


def test_recovery_churn_restarts_the_uptime_clock(tmp_path: Path) -> None:
    # The uptime anchor is per SOURCE lifetime: after a degraded-path retry the fresh
    # source starts young, so preventive reopens never pile onto recovery churn.
    settings = _settings(tmp_path, capture_reopen_max_uptime_s=60)
    script1 = _line((_NOISE, 20), start_us=58 * S)
    script1.append(RuntimeError("device vanished"))
    h = _harness(
        tmp_path,
        [script1, _line((_NOISE, 80), start_us=59 * S + 100_000)],
        settings,
    )
    _s1, s2 = h.sources_all  # type: ignore[attr-defined]
    h.runner.run()

    # Wall clock passes 60 s during source 2, but its OWN uptime never does.
    assert not s2.stopped
    assert h.preventive_events() == []


# -- US3: observability — the churn theory is testable ------------------------------------


def test_second_reopen_carries_the_measured_gap(tmp_path: Path) -> None:
    # The deaf window of reopen 1 (source-1 last block → source-2 first block) is
    # logged when hearing resumes and rides in reopen 2's audit payload.
    h = _harness(
        tmp_path,
        [
            _burst(start_us=0),
            _burst(start_us=10 * S),
            _line((_NOISE, 5), start_us=60 * S),
        ],
        _settings(tmp_path),
    )
    h.runner.run()

    events = h.preventive_events()
    assert len(events) == 2
    second, first = events  # query_events returns newest first
    assert first["payload"]["gap_ms"] is None
    gap_ms = second["payload"]["gap_ms"]
    assert isinstance(gap_ms, int) and gap_ms > 0
    assert any(f"capture reopened: {gap_ms}ms deaf" in line for line in h.logs)
    # The measured value is the scripted source-1 stop → source-2 start distance:
    # source 2 begins at 10 s and source 1 was stopped a few seconds in.
    assert 5_000 <= gap_ms <= 9_000
    assert second["payload"]["uptime_s"] >= 1
    assert second["payload"]["quiet_ms"] == 2000


def test_gap_is_also_measured_after_a_watchdog_reopen(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        watchdog=True,
        watchdog_flatline_s=1,
        watchdog_floor_s=0,
        capture_reopen_after_tx=False,
    )
    h = _harness(
        tmp_path,
        [_line((_DIGITAL_SILENCE, 30), start_us=0), _line((_NOISE, 10), start_us=5 * S)],
        settings,
    )
    h.runner.run()

    assert any("capture reopened:" in line and "ms deaf" in line for line in h.logs)
