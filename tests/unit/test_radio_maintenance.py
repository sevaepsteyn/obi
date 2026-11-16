"""Unit (feature 135, US2): the radio station's operator DMs honor maintenance.

``radio`` in ``[maintenance] signals`` ⇒ the outage and rate-backstop Slack DMs are suppressed,
logged and audited; the check runs at send time (no restart) and fails toward alarming (M7,
M8). Receive/transmit are not touched by this hook at all (FR-020).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obi.core.config import RadioSettings
from obi.radio.devices import DevicesReport, HalfReport
from obi.radio.runner import RadioRunner
from tests.fakes import FakeTranscriber


@dataclass
class _Notifier:
    notes: list[str] = field(default_factory=list)

    def deliver_status(self, text: str) -> None:
        self.notes.append(text)


@dataclass
class _Recorder:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def record(self, kind: str, **kwargs: Any) -> None:
        self.events.append((kind, kwargs))


def _report() -> DevicesReport:
    return DevicesReport(
        audio=HalfReport(present=True, detail="audio"),
        serial=HalfReport(present=True, detail="serial"),
    )


def _runner(tmp_path: Path, hook: Any) -> tuple[RadioRunner, _Notifier, _Recorder, list[str]]:
    notifier = _Notifier()
    recorder = _Recorder()
    logs: list[str] = []
    runner = RadioRunner(
        settings=RadioSettings(
            sample_rate=16_000, db_path=tmp_path / "radio.db", clips_dir=tmp_path / "clips"
        ),
        source_factory=lambda: None,
        transcriber=FakeTranscriber(results=[]),
        probe_fn=_report,
        notifier=notifier,
        sleep=lambda s: None,
        log=logs.append,
        recorder=recorder,  # type: ignore[arg-type]
        in_maintenance=hook,
    )
    return runner, notifier, recorder, logs


def test_outage_dm_suppressed_in_maintenance(tmp_path: Path) -> None:
    runner, notifier, recorder, logs = _runner(tmp_path, lambda: "config: teardown")
    runner._note_outage("radio capture unavailable: audio device absent")
    assert notifier.notes == []
    assert any("outage DM suppressed (maintenance: config: teardown)" in line for line in logs)
    ((kind, event),) = recorder.events
    assert kind == "report" and event["outcome"] == "suppressed"
    assert event["payload"]["kind"] == "outage" and event["payload"]["reason"] == "config: teardown"
    # still one note per streak: a second call in the same streak does nothing new
    runner._note_outage("radio capture unavailable: audio device absent")
    assert notifier.notes == [] and len(recorder.events) == 1


def test_outage_dm_sent_when_not_in_maintenance(tmp_path: Path) -> None:
    runner, notifier, recorder, _ = _runner(tmp_path, lambda: None)
    runner._note_outage("radio capture unavailable")
    assert notifier.notes == ["radio capture unavailable"]
    assert recorder.events == []


def test_outage_dm_sent_when_the_check_fails(tmp_path: Path) -> None:
    def boom() -> str | None:
        raise OSError("config unreadable")

    runner, notifier, _, _ = _runner(tmp_path, boom)
    runner._note_outage("radio capture unavailable")
    assert notifier.notes == ["radio capture unavailable"]  # M7: fail toward alarming


def test_outage_dm_sent_without_a_hook(tmp_path: Path) -> None:
    runner, notifier, _, _ = _runner(tmp_path, None)
    runner._note_outage("radio capture unavailable")
    assert notifier.notes == ["radio capture unavailable"]


def test_rate_backstop_uses_the_same_gate(tmp_path: Path) -> None:
    runner, _, recorder, _ = _runner(tmp_path, lambda: "config")
    assert runner._dm_suppressed("rate_backstop", "radio rate backstop tripped") is True
    assert recorder.events[0][1]["payload"]["kind"] == "rate_backstop"
    runner2, _, _, _ = _runner(tmp_path / "b", lambda: None)
    assert runner2._dm_suppressed("rate_backstop", "x") is False


def test_hook_is_consulted_at_send_time(tmp_path: Path) -> None:
    state = {"label": None}
    runner, notifier, _, _ = _runner(tmp_path, lambda: state["label"])
    runner._note_outage("first outage")
    assert notifier.notes == ["first outage"]
    runner._outage_noted = False  # a new streak
    state["label"] = "config"  # operator edits config — no restart
    runner._note_outage("second outage")
    assert notifier.notes == ["first outage"]


def test_backstop_call_site_is_gated() -> None:
    # The backstop DM sits deep inside the TX worker; pin that it routes through the gate.
    import inspect

    from obi.radio import runner as runner_mod

    source = inspect.getsource(runner_mod.RadioRunner)
    backstop = source[source.index('disposition == "suppressed_rate"') :][:600]
    assert '_dm_suppressed("rate_backstop"' in backstop
