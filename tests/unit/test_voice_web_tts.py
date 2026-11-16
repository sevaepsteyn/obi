"""Unit tests: server-side answer rendering (feature 024, T003 — research R3).

``render`` shells out to macOS ``say`` writing a WAV file (verified live: ``say -o x.wav
--data-format=LEI16@22050`` emits RIFF/WAVE), reads the bytes, and deletes the temp file on
every path. The runner is injected so no test invokes the real ``say``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.voice.tts import render


def _fake_runner(payload: bytes = b"RIFFfake-wav"):  # noqa: ANN202 - test helper
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> None:
        calls.append(list(argv))
        Path(argv[argv.index("-o") + 1]).write_bytes(payload)

    return run, calls


def test_render_builds_the_verified_argv_and_returns_the_bytes() -> None:
    run, calls = _fake_runner(b"RIFFxxxxWAVE")
    audio = render("all links are up", voice="Samantha", rate=190, runner=run)
    assert audio == b"RIFFxxxxWAVE"
    (argv,) = calls
    assert argv[0] == "say"
    assert "--data-format=LEI16@22050" in argv
    assert argv[argv.index("-v") + 1] == "Samantha"
    assert argv[argv.index("-r") + 1] == "190"
    assert argv[-1] == "all links are up"
    out = Path(argv[argv.index("-o") + 1])
    assert out.suffix == ".wav"
    assert not out.exists()  # temp file removed after the read


def test_render_omits_voice_and_rate_when_unset() -> None:
    run, calls = _fake_runner()
    render("hello", voice=None, rate=None, runner=run)
    (argv,) = calls
    assert "-v" not in argv
    assert "-r" not in argv


def test_render_sample_rate_parameter_selects_the_data_format() -> None:
    # feature 088: the radio TX path renders at the codec's native rate; the default stays
    # 22050 so the web voice surface is unchanged.
    run, calls = _fake_runner()
    render("hello", voice=None, rate=None, sample_rate=48_000, runner=run)
    (argv,) = calls
    assert "--data-format=LEI16@48000" in argv


def test_render_empty_text_never_invokes_say() -> None:
    run, calls = _fake_runner()
    assert render("   ", voice=None, rate=None, runner=run) == b""
    assert calls == []


def test_render_strips_markdown_before_say() -> None:
    # feature 082: markdown the model may emit must not reach `say` as literal punctuation.
    run, calls = _fake_runner()
    render("**core-1** is *down* — see `logs`", voice=None, rate=None, runner=run)
    (argv,) = calls
    spoken = argv[-1]
    assert spoken == "core-1 is down — see logs"
    for symbol in ("**", "*", "`"):
        assert symbol not in spoken


def test_render_cleans_up_the_temp_file_when_say_fails() -> None:
    seen: list[Path] = []

    def failing(argv: list[str], **kwargs: object) -> None:
        seen.append(Path(argv[argv.index("-o") + 1]))
        raise OSError("say exploded")

    with pytest.raises(OSError, match="exploded"):
        render("boom", voice=None, rate=None, runner=failing)
    (out,) = seen
    assert not out.exists()
