"""SaySpeaker strips markdown before handing text to macOS `say` (feature 082)."""

from __future__ import annotations

from typing import Any

import obi.voice.tts as tts
from obi.voice.tts import SaySpeaker


class _FakeProc:
    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:  # pragma: no cover - not reached in these tests
        pass


def test_speak_strips_markdown(monkeypatch: Any) -> None:
    seen: list[list[str]] = []

    def fake_popen(args: list[str], *a: Any, **k: Any) -> _FakeProc:
        seen.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(tts.subprocess, "Popen", fake_popen)
    SaySpeaker().speak("**tamarack-gw** is up for *7 weeks*")

    (argv,) = seen
    spoken = argv[-1]
    assert spoken == "tamarack-gw is up for 7 weeks"
    assert "*" not in spoken


def test_speak_skips_when_only_markup_or_blank(monkeypatch: Any) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        tts.subprocess, "Popen", lambda *a, **k: (seen.append(a), _FakeProc())[1]
    )
    SaySpeaker().speak("   ")  # nothing to say → `say` never spawned
    assert seen == []
