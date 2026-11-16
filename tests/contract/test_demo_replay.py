"""The chat-preview replay drives the real chat control (feature 136, T034); needs node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "demo_replay.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def out() -> dict[str, Any]:
    result = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "voice.js"),
         str(STATIC_DIR / "demo" / "replay.js"), str(STATIC_DIR / "widgets" / "talk.js")],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_recording_plays_the_live_event_sequence(out: dict[str, Any]) -> None:
    states = [s for s, _ in out["firstPlay"]]
    assert states == [
        "reset",
        "submitting", "thinking", "thinking", "thinking", "speaking", "answered",
        "submitting", "thinking", "speaking", "answered",
    ]  # fmt: skip
    details = [d for _, d in out["firstPlay"]]
    assert details[2] == "is anything down?"  # the transcript echoes the question
    assert details[3:5] == ["consulting uisp_links…", "consulting dash_status…"]
    assert details[5] == "All **20** links are up."


def test_pacing_is_capped(out: dict[str, Any]) -> None:
    progress = out["firstSlept"][:2]
    assert progress == [out["caps"]["progress"], 300]
    assert out["revealLong"] == out["caps"]["reveal"] == 4000
    assert out["revealTotal"] <= 4000


def test_visitor_question_gets_the_demo_notice_without_network(out: dict[str, Any]) -> None:
    assert out["visitor"][-1] == ["error", out["demoMessage"]]
    assert out["fetches"] == []


def test_next_and_replay_move_across_recordings(out: dict[str, Any]) -> None:
    assert out["changes"][0] == 0
    assert (out["afterNext"], out["afterWrap"], out["afterReplay"]) == (1, 0, 0)


def test_report_and_reveal_render(out: dict[str, Any]) -> None:
    assert "uisp_links" in out["reportHtml"]
    assert out["revealed"] == "All 20 links are up."
    lengths = [len(f) for f in out["frames"]]
    assert lengths == sorted(lengths) and lengths[0] < len(out["revealed"])
