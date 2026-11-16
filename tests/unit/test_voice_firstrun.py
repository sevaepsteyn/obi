"""Unit tests (T013): the first-run model-download disclosure flow (FR-013)."""

from __future__ import annotations

import io

from rich.console import Console

from obi.core.config import VoiceSettings
from tests.fakes import scripted_input


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=200)


def test_cached_model_asks_nothing(tmp_path) -> None:
    from obi.voice.session import ensure_model_available

    console = _console()

    def _never(_prompt: str = "") -> str:
        raise AssertionError("must not prompt when the model is already cached")

    assert ensure_model_available(
        VoiceSettings(), console, _never, is_cached=lambda model: True
    )
    assert console.file.getvalue() == ""


def test_uncached_model_disclosed_then_downloaded_eagerly_on_consent() -> None:
    from obi.voice.session import ensure_model_available

    console = _console()
    fetched: list[str] = []
    assert ensure_model_available(
        VoiceSettings(),
        console,
        scripted_input(["y"]),
        is_cached=lambda model: False,
        download=fetched.append,
    )
    out = console.file.getvalue()
    assert "mlx-community/whisper-large-v3-turbo" in out  # what will be fetched…
    assert "1.6 GB" in out  # …and how big it is (FR-013)
    # Consent means the fetch happens NOW, visibly — not silently inside the first turn.
    assert fetched == ["mlx-community/whisper-large-v3-turbo"]
    assert "download" in out.lower()  # the operator sees the download begin


def test_declining_the_download_fetches_nothing_and_bows_out() -> None:
    from obi.voice.session import ensure_model_available

    console = _console()
    fetched: list[str] = []
    assert not ensure_model_available(
        VoiceSettings(),
        console,
        scripted_input(["n"]),
        is_cached=lambda model: False,
        download=fetched.append,
    )
    assert fetched == []


def test_no_input_device_is_a_clear_voice_unavailable(monkeypatch) -> None:
    import pytest as _pytest

    from obi.voice import audio
    from obi.voice.errors import VoiceUnavailable

    class _NoMic:
        @staticmethod
        def query_devices(kind: str) -> dict:
            return {"max_input_channels": 0}

    monkeypatch.setattr(audio, "_sounddevice", lambda: _NoMic)
    with _pytest.raises(VoiceUnavailable, match="Privacy & Security"):
        audio.ensure_input_device()
