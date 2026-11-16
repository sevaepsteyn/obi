"""Contract: the tap-or-hold talk-control state machine actually resolves each gesture (056).

The mic interaction is core logic (constitution II) and DOM-free by design (research R4/R10):
capture + transport are injectable, so createTalkControl runs in plain node with a stubbed
recorder and a scripted turn stream. This drives every gesture path through the REAL voice.js
module (the ``ospf_headless.mjs`` idiom) and asserts the emitted state sequences — chiefly that
no path ends silently (SC-003: the old short-hold silent-discard is gone).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "talk_control.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run() -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "voice.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _sequence(events: list[dict]) -> list[str]:
    return [event["state"] for event in events]


def test_tap_toggles_then_a_second_tap_submits() -> None:
    events = _run()["tapToggle"]
    assert events[0]["state"] == "listening" and events[0]["detail"]["mode"] == "hold"
    # released before the threshold ⇒ toggle mode ("tap to send"), still listening
    assert events[1]["state"] == "listening" and events[1]["detail"]["mode"] == "toggle"
    sequence = _sequence(events)
    assert "submitting" in sequence  # the second tap sent
    assert sequence[-1] == "answered"


def test_tap_then_escape_discards_visibly_without_submitting() -> None:
    events = _run()["tapCancel"]
    sequence = _sequence(events)
    # toggle listening, then a VISIBLE discard (a hint) — never a silent idle (FR-017/SC-003)
    assert sequence == ["listening", "listening", "hint"]
    assert "discarded" in events[-1]["detail"]["message"]
    assert "submitting" not in sequence  # nothing was sent


def test_hold_past_the_threshold_submits_on_release() -> None:
    events = _run()["hold"]
    sequence = _sequence(events)
    assert events[0]["detail"]["mode"] == "hold"  # push-to-talk while held
    assert "submitting" in sequence
    assert sequence[-1] == "answered"


def test_cap_timer_auto_submits_with_the_capped_flag() -> None:
    events = _run()["cap"]
    submitting = next(event for event in events if event["state"] == "submitting")
    assert submitting["detail"]["capped"] is True
    assert _sequence(events)[-1] == "answered"


def test_a_second_window_is_told_the_mic_is_busy_and_by_whom() -> None:
    busy = _run()["busy"]
    # window A owns the recorder…
    assert _sequence(busy["a"]) == ["listening"]
    # …and window B is refused, naming A (FR-018) — it never starts recording.
    assert _sequence(busy["b"]) == ["busy-elsewhere"]
    assert busy["b"][0]["detail"]["owner"] == "window A"


def test_no_gesture_path_ends_silently() -> None:
    """SC-003: every driven path emits at least one visible state and ends in a rendered
    terminal (answered / hint / busy-elsewhere) — the silent-discard path is eliminated."""

    result = _run()
    terminals = {"answered", "hint", "busy-elsewhere", "error"}
    for name in ("tapToggle", "tapCancel", "hold", "cap"):
        sequence = _sequence(result[name])
        assert sequence, name
        assert sequence[-1] in terminals, (name, sequence)
    assert _sequence(result["busy"]["b"])[-1] in terminals
