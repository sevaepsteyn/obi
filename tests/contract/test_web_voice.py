"""Contract tests: voice on the unified web origin (features 024/032/033).

Merged from ``test_ar_voice.py`` + ``test_voice_web_api.py`` + ``test_voice_web_second_client.py``
(033 T002): every contract sentence those suites asserted is asserted here, against the ONE
server that now hosts the turn protocol. The real TLS server runs in-process on an ephemeral
port; only the pipeline pieces behind ``/api/turn`` are offline doubles.
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from obi.voice.stt import TranscriptionResult
from obi.voice.web.turns import ConversationStore, VoiceWebApp
from obi.web.server import STATIC_DIR, create_server
from tests.fakes import (
    FakeTranscriber,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    tool_response,
)

CID = "ar-cafef00d-1111-2222-3333-444455556666"


def make_wav(seconds: float = 1.0, *, rate: int = 16000) -> bytes:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    pcm = (np.sin(2 * np.pi * 220.0 * t) * 0.2 * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())
    return buf.getvalue()


def _make_app(tmp_path: Path, *, transcriber: FakeTranscriber | None = None) -> VoiceWebApp:
    config = chat_config(tmp_path)
    save_inventory(config.inventory_path, [])  # the audited tool path needs an inventory
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("routeros_status", {"target": "ghost-device"}),
            text_response("Yes — the link is up."),
            text_response("Still up."),
        ],
    )
    return VoiceWebApp(
        runtime=runtime,
        settings=config.voice,
        transcriber=transcriber
        or FakeTranscriber(
            results=[
                TranscriptionResult(text="is the link up?", gate="ok", seconds=1.0, level=0.2),
                TranscriptionResult(text="and now?", gate="ok", seconds=0.8, level=0.2),
            ]
        ),
        render=lambda text: b"RIFFfakewav" + text.encode()[:4],
        store=ConversationStore(idle_minutes=30),
    )


def _serve(
    tmp_path: Path, *, voice_app: VoiceWebApp | None
) -> Iterator[tuple[str, Path]]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        voice_app=voice_app,
        voice_max_seconds=90,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def voiced(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    yield from _serve(tmp_path, voice_app=_make_app(tmp_path))


@pytest.fixture()
def voiceless(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    yield from _serve(tmp_path, voice_app=None)


def _get(base: str, path: str) -> httpx.Response:
    return httpx.get(f"{base}{path}", verify=False, timeout=5.0)


def post_turn(
    base: str,
    *,
    cid: str = CID,
    audio: bytes | None = None,
    name: str = "q.wav",
    context: str | None = None,
) -> httpx.Response:
    data = {"conversation": cid}
    if context is not None:
        data["context"] = context  # 056: the optional right-click context part
    return httpx.post(
        f"{base}/api/turn",
        files={"audio": (name, audio if audio is not None else make_wav(), "audio/wav")},
        data=data,
        verify=False,
        timeout=10.0,
    )


def post_text(
    base: str, text: str, *, cid: str = CID, context: str | None = None
) -> httpx.Response:
    data = {"conversation": cid, "text": text}
    if context is not None:
        data["context"] = context  # 056: the optional right-click context part
    return httpx.post(
        f"{base}/api/turn",
        data=data,
        files={"_": ("", b"")},  # force multipart encoding without an audio part
        verify=False,
        timeout=10.0,
    )


def events_of(response: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


# --- the turn protocol on the unified origin (032 contract §2, 024 contract) ----------------


def test_full_turn_streams_the_documented_events(voiced: tuple[str, Path]) -> None:
    base, tmp = voiced
    response = post_turn(base)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = events_of(response)
    assert [e["event"] for e in events] == ["transcript", "progress", "answer"]
    assert events[0]["text"] == "is the link up?"
    assert events[1]["tool"] == "routeros_status"
    assert base64.b64decode(events[2]["audio_wav_b64"]).startswith(b"RIFF")
    assert events[2]["context_reset"] is True
    # The conversation id flows into the audit actor unchanged; per-tool records fire.
    records = [
        json.loads(line) for line in (tmp / "audit.jsonl").read_text().splitlines() if line
    ]
    assert any(r.get("actor") == f"voice-web:{CID[:8]}" for r in records)


def test_same_conversation_carries_context(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    first = post_turn(base)
    second = post_turn(base)
    assert events_of(first)[-1]["context_reset"] is True
    assert events_of(second)[-1]["context_reset"] is False


def test_malformed_wav_is_400(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    response = post_turn(base, audio=b"definitely not a wav")
    assert response.status_code == 400
    assert "WAV" in response.json()["error"]


def test_wrong_rate_wav_is_400(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    response = post_turn(base, audio=make_wav(rate=44100))
    assert response.status_code == 400
    assert "16000" in response.json()["error"]


def test_missing_parts_are_400(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    no_audio = httpx.post(
        f"{base}/api/turn", data={"conversation": CID}, verify=False, timeout=5.0
    )
    assert no_audio.status_code == 400


def test_bad_conversation_id_is_400(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    assert post_turn(base, cid="nope").status_code == 400
    assert post_turn(base, cid="bad id with spaces!").status_code == 400


def test_overlong_recording_is_413(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    over = make_wav(seconds=91.0)  # web_max_seconds default is 90
    assert post_turn(base, audio=over).status_code == 413


def test_oversized_body_is_413_before_reading_it(voiced: tuple[str, Path]) -> None:
    """A raw request declaring an 8 MB body is refused up front (Content-Length gate)."""

    import socket
    import ssl

    base, _tmp = voiced
    port = int(base.rsplit(":", 1)[1])
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw,
        context.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
    ):
        tls.sendall(
            b"POST /api/turn HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: multipart/form-data; boundary=x\r\n"
            b"Content-Length: 8388608\r\n\r\n"
        )
        status = tls.recv(4096).decode(errors="replace").splitlines()[0]
    assert " 413 " in status


def test_expiry_over_the_api_returns_context_reset(tmp_path: Path) -> None:
    """024 T014: a conversation idled past the bound is re-created; the API flags the reset."""

    now = [0.0]
    config = chat_config(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a."), text_response("b.")])
    app = VoiceWebApp(
        runtime=runtime,
        settings=config.voice,
        transcriber=FakeTranscriber(
            results=[
                TranscriptionResult(text="one", gate="ok", seconds=1.0, level=0.2),
                TranscriptionResult(text="two", gate="ok", seconds=1.0, level=0.2),
            ]
        ),
        render=lambda text: b"RIFF",
        store=ConversationStore(idle_minutes=30, clock=lambda: now[0]),
    )
    for base, _tmp in _serve(tmp_path, voice_app=app):
        first = post_turn(base)
        now[0] += 31 * 60
        second = post_turn(base)
    assert events_of(first)[-1]["context_reset"] is True
    assert events_of(second)[-1]["context_reset"] is True  # swept + re-created over the API


def test_gated_recording_emits_the_terminal_gated_event(tmp_path: Path) -> None:
    """024 US4: a too-quiet capture is gated — one terminal event, nothing reaches the model."""

    app = _make_app(
        tmp_path,
        transcriber=FakeTranscriber(
            results=[TranscriptionResult(text="", gate="mic_silent", seconds=1.0, level=0.0)]
        ),
    )
    for base, _tmp in _serve(tmp_path, voice_app=app):
        events = events_of(post_turn(base))
        assert [e["event"] for e in events] == ["gated"]
        assert events[0]["reason"] == "silent"


def test_busy_conversation_emits_the_error_event(tmp_path: Path) -> None:
    """024 US4: a second turn while one is in flight → the documented busy error event."""

    started = threading.Event()
    release = threading.Event()

    class _SlowTranscriber(FakeTranscriber):
        def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
            started.set()
            release.wait(timeout=10.0)
            return super().transcribe(audio)

    app = _make_app(
        tmp_path,
        transcriber=_SlowTranscriber(
            results=[
                TranscriptionResult(text="is the link up?", gate="ok", seconds=1.0, level=0.2)
            ]
        ),
    )
    for base, _tmp in _serve(tmp_path, voice_app=app):
        first: dict[str, httpx.Response] = {}
        thread = threading.Thread(
            target=lambda first=first, base=base: first.update(r=post_turn(base))
        )
        thread.start()
        try:
            assert started.wait(timeout=10.0)
            second = post_turn(base)  # same conversation, while the first is mid-STT
        finally:
            release.set()
            thread.join(timeout=15.0)
        events = events_of(second)
        assert events[-1]["event"] == "error"
        assert events[-1]["reason"] == "busy"
        assert [e["event"] for e in events_of(first["r"])][-1] == "answer"  # not corrupted


# --- typed turns (033: text alongside push-to-talk) ------------------------------------------


def test_typed_turn_streams_the_same_protocol_without_stt(tmp_path: Path) -> None:
    """A `text` part runs the same conversation/tools/answer path; the transcript event
    echoes the typed text flagged `typed`, and STT is never touched (no results staged —
    the fake would fail if consumed). 056 R5: a typed answer is TEXT ONLY — no
    ``audio_wav_b64`` (the server skips TTS synthesis for typed turns)."""

    app = _make_app(tmp_path, transcriber=FakeTranscriber(results=[]))
    for base, _tmp in _serve(tmp_path, voice_app=app):
        response = post_text(base, "is the link up?")
        assert response.status_code == 200
        events = events_of(response)
        assert [e["event"] for e in events] == ["transcript", "progress", "answer"]
        assert events[0] == {"event": "transcript", "text": "is the link up?", "typed": True}
        assert "audio_wav_b64" not in events[2]  # typed turns synthesize no speech (R5)
        assert events[2]["context_reset"] is True


def test_typed_and_spoken_turns_share_one_conversation(tmp_path: Path) -> None:
    """Mode is per-turn, not per-conversation: a spoken turn then a typed follow-up under
    the same id carries context (no reset)."""

    for base, _tmp in _serve(tmp_path, voice_app=_make_app(tmp_path)):
        first = post_turn(base)  # spoken
        second = post_text(base, "and now?")  # typed follow-up, same conversation
        assert events_of(first)[-1]["context_reset"] is True
        assert events_of(second)[-1]["context_reset"] is False


def test_audio_and_text_together_is_400(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    response = httpx.post(
        f"{base}/api/turn",
        files={"audio": ("q.wav", make_wav(), "audio/wav")},
        data={"conversation": CID, "text": "also this"},
        verify=False,
        timeout=10.0,
    )
    assert response.status_code == 400
    assert "exactly one" in response.json()["error"]


def test_empty_text_is_400(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    assert post_text(base, "   ").status_code == 400


def test_overlong_text_is_413(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    assert post_text(base, "x" * 2001).status_code == 413


# --- 056: the context part + conditional TTS over HTTP (T004/T026) ----------------------------


def _app_with_client(
    tmp_path: Path, *, responses: list[Any], transcriber: FakeTranscriber | None = None
) -> VoiceWebApp:
    """An app whose scripted client records every call — lets a served request assert the
    composed prompt the model actually saw (the ``voiced`` fixture hides its app)."""

    config = chat_config(tmp_path)
    save_inventory(config.inventory_path, [])
    runtime = chat_runtime(config, responses=responses)
    return VoiceWebApp(
        runtime=runtime,
        settings=config.voice,
        transcriber=transcriber
        or FakeTranscriber(
            results=[TranscriptionResult(text="what is this?", gate="ok", seconds=1.0, level=0.2)]
        ),
        render=lambda text: b"RIFFfakewav" + text.encode()[:4],
        store=ConversationStore(idle_minutes=30),
    )


def test_context_part_folds_into_the_typed_prompt_over_http(tmp_path: Path) -> None:
    app = _app_with_client(tmp_path, responses=[text_response("healthy.")])
    for base, _tmp in _serve(tmp_path, voice_app=app):
        response = post_text(
            base, "is this healthy?", context="[window] ROS\n[element]\nidentity: core-gw-1"
        )
        assert response.status_code == 200
        events = events_of(response)
        assert events[0] == {"event": "transcript", "text": "is this healthy?", "typed": True}
    prompt = json.dumps(app.runtime.client.calls[-1]["messages"])  # type: ignore[attr-defined]
    assert "is this healthy?" in prompt
    assert "operator's screen context" in prompt  # the fenced block header (R6)
    assert "core-gw-1" in prompt  # the whole attachment reached the model (FR-022)


def test_context_part_rides_a_spoken_turn_over_http(tmp_path: Path) -> None:
    app = _app_with_client(tmp_path, responses=[text_response("an error.")])
    for base, _tmp in _serve(tmp_path, voice_app=app):
        assert post_turn(base, context="[element]\nerror line on eth3").status_code == 200
    prompt = json.dumps(app.runtime.client.calls[-1]["messages"])  # type: ignore[attr-defined]
    assert "operator's screen context" in prompt
    assert "error line on eth3" in prompt


def test_stripped_empty_context_is_ignored(tmp_path: Path) -> None:
    app = _app_with_client(tmp_path, responses=[text_response("ok.")])
    for base, _tmp in _serve(tmp_path, voice_app=app):
        assert post_text(base, "plain question", context="   \n  ").status_code == 200
    prompt = json.dumps(app.runtime.client.calls[-1]["messages"])  # type: ignore[attr-defined]
    assert "operator's screen context" not in prompt
    assert "plain question" in prompt


def test_oversized_context_is_413_with_a_readable_error(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    response = post_text(base, "q", context="x" * 4001)
    assert response.status_code == 413
    assert "context too large" in response.json()["error"]


def test_absent_context_is_a_normal_turn(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    assert post_text(base, "is the link up?").status_code == 200


def test_answer_event_shape_differs_by_input_mode(tmp_path: Path) -> None:
    """R5 over the wire: a typed answer omits ``audio_wav_b64`` (no synthesis); a spoken
    answer carries it. Pins the server contract against a client-side regression."""

    app = _app_with_client(
        tmp_path, responses=[text_response("typed reply."), text_response("spoken reply.")]
    )
    for base, _tmp in _serve(tmp_path, voice_app=app):
        typed = events_of(post_text(base, "typed q", cid="typed-1111-2222-3333-444455556666"))
        spoken = events_of(post_turn(base, cid="spoken-1111-2222-3333-444455556666"))
    assert typed[-1]["event"] == "answer" and "audio_wav_b64" not in typed[-1]
    assert spoken[-1]["event"] == "answer"
    assert base64.b64decode(spoken[-1]["audio_wav_b64"]).startswith(b"RIFF")


# --- a second, independent client (024 SC-006) ----------------------------------------------


def test_scripted_client_from_the_contract_doc_alone(voiced: tuple[str, Path]) -> None:
    """The contract doc's client sketch, verbatim in shape — no obi client code involved."""

    base, _tmp = voiced
    pcm16 = (np.sin(np.linspace(0, 200, 16000)) * 0.2 * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm16)

    events = []
    with httpx.stream(
        "POST",
        f"{base}/api/turn",
        verify=False,
        timeout=10.0,
        files={"audio": ("q.wav", buf.getvalue(), "audio/wav")},
        data={"conversation": "scripted-client-1"},
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.strip():
                events.append(json.loads(line))

    kinds = [e["event"] for e in events]
    assert kinds[0] == "transcript" and kinds[-1] == "answer"
    assert events[0]["text"] == "is the link up?"
    assert base64.b64decode(events[-1]["audio_wav_b64"]).startswith(b"RIFF")
    assert events[-1]["context_reset"] is True  # first turn of a fresh id
    assert all(
        e["event"] in {"transcript", "progress", "answer", "gated", "error"} for e in events
    )


# --- the capability probe (032 contract §1) --------------------------------------------------


def test_voice_json_reports_enabled_with_the_cap(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    response = _get(base, "/voice.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"enabled": True, "max_seconds": 90}
    assert _get(base, "/voice.json?x=1").json()["enabled"] is True  # query strings ignored


def test_voice_json_reports_disabled_on_the_static_server(voiceless: tuple[str, Path]) -> None:
    base, _tmp = voiceless
    response = _get(base, "/voice.json")
    assert response.status_code == 200
    assert response.json()["enabled"] is False


# --- voice-off = the untouched static server (032 contract §2/§4) ----------------------------


def test_disabled_turn_is_404_and_statics_still_serve(voiceless: tuple[str, Path]) -> None:
    base, _tmp = voiceless
    assert post_turn(base).status_code == 404
    for path in ("/", "/index.html", "/viewer.js", "/links.json"):
        assert _get(base, path).status_code == 200, path


def test_post_does_not_widen_the_allowlist(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    assert httpx.post(f"{base}/", verify=False, timeout=5.0).status_code == 405
    assert httpx.post(f"{base}/links.json", verify=False, timeout=5.0).status_code == 405
    assert httpx.post(f"{base}/voice.json", verify=False, timeout=5.0).status_code == 405
    assert httpx.post(f"{base}/audit.jsonl", verify=False, timeout=5.0).status_code == 404


def test_unknown_path_is_404(voiced: tuple[str, Path]) -> None:
    base, _tmp = voiced
    assert _get(base, "/nope").status_code == 404
    assert _get(base, "/api/turn").status_code == 404  # POST-only route, not in the GET map


# --- the viewer's voice module (032 contract §3) ---------------------------------------------


def test_voice_js_is_served_from_the_allowlist(voiceless: tuple[str, Path]) -> None:
    """/voice.js is a static allowlist entry (voice on or off — /voice.json gates use)."""

    base, _tmp = voiceless
    response = _get(base, "/voice.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "export" in response.text  # it is an ES module


def test_zero_internet_guard_covers_the_whole_bundle() -> None:
    """The static bundle — shell, window manager, widgets, voice.js, theme included —
    contains no absolute http(s) reference at all (the 022 playa-proof rule; the vendored
    three.js license header stays the only exemption). The W3C SVG namespace URI is exempt too:
    it is an XML namespace IDENTIFIER (never fetched), required by createElementNS for the 075
    inline-SVG faceplate — not an external dependency."""

    for asset in STATIC_DIR.glob("**/*"):
        if not asset.is_file() or asset.suffix not in (".html", ".js", ".css"):
            continue
        if "vendor" in asset.parts:
            continue
        source = asset.read_text(encoding="utf-8").replace("http://www.w3.org/2000/svg", "")
        assert not re.search(r"https?://", source), f"{asset.name} references an external URL"


# --- voice in the TALK widget (033 US3 / FR-007, re-homed by 037 research R5/R6) --------------

TALK_SOURCE = (STATIC_DIR / "widgets" / "talk.js").read_text(encoding="utf-8")
VIEWER3D_SOURCE = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
VOICE_SOURCE = (STATIC_DIR / "voice.js").read_text(encoding="utf-8")


def test_talk_widget_drives_the_shared_voice_module() -> None:
    """The TALK widget reuses voice.js — capture, turn POST, playback — via the same
    state machine the 3D viewer uses. One engine, one client module, zero drift."""

    assert re.search(r"""from\s+['"]\.\./voice\.js['"]""", TALK_SOURCE)
    for symbol in ("fetchVoiceStatus", "createTalkControl", "ensureCapture"):
        assert symbol in TALK_SOURCE, symbol


def test_talk_widget_is_tap_or_hold() -> None:
    """056 US3: the mic is tap-toggle OR press-and-hold. talk.js drives the tap-or-hold state
    machine via press/release; the raw holdStart/holdEnd push-to-talk primitives stay in
    voice.js for the XR squeeze/pinch (immersive surfaces untouched)."""

    assert "control.press()" in TALK_SOURCE
    assert "control.release()" in TALK_SOURCE
    assert "holdStart" in VOICE_SOURCE and "holdEnd" in VOICE_SOURCE


def test_talk_affordance_is_a_mic_icon_on_both_surfaces() -> None:
    """Operator ask (2026-07-08): the affordance is a microphone icon, not the word 'talk' —
    an emoji glyph in the TALK widget, a vector glyph on the in-scene 3D button."""

    assert "🎤" in TALK_SOURCE
    assert "drawMicGlyph" in VIEWER3D_SOURCE
    assert "◉ talk" not in VIEWER3D_SOURCE


def test_talk_widget_is_clearly_inactive_when_voice_is_off() -> None:
    """Server voice off ⇒ the turn engine is absent entirely (/api/turn 404s): the widget
    renders an explicit unavailable state naming the fix — never broken, never a mystery
    (US3 AS-3 lineage; 037 FR-019)."""

    gate = re.search(
        r"if\s*\(!\s*voiceStatus\.enabled\s*\)\s*\{(.*?)\n  \}", TALK_SOURCE, re.S
    )
    assert gate, "expected an explicit voice-off branch"
    off_branch = gate.group(1)
    assert "off" in off_branch and "return" in off_branch
    # 048: voice is on by default now; the hint names the command + the opt-out that disables it.
    assert "obi web serve" in off_branch
    assert "[web] voice = false" in off_branch


def test_typed_mode_submits_through_the_same_turn_machinery() -> None:
    """Operator ask (2026-07-08): a text box works alongside the mic — Enter submits a
    typed turn through the SAME createTalkControl (same conversation, same in-flight
    lock, same event states); no microphone required. Operator ask (2026-08-19): an
    on-screen send button beside the box submits the same way — phone keyboards don't
    always offer a usable Enter."""

    assert "submitText" in TALK_SOURCE
    assert "talk-ask" in TALK_SOURCE
    assert re.search(r"""key\s*===\s*['"]Enter['"]""", TALK_SOURCE)
    # both entry points funnel through one submit path
    assert "talk-send" in TALK_SOURCE
    assert TALK_SOURCE.count("submitTyped()") >= 2
    # the shared module carries the typed path: one POST helper per input mode
    assert "postTextTurn" in VOICE_SOURCE
    assert re.search(r"""append\(\s*["']text["']""", VOICE_SOURCE)
    assert "submitText" in VOICE_SOURCE


def test_talk_widget_renders_a_transcript() -> None:
    """037 US1: the widget upgrades the single status line to a capped scrollback —
    operator lines, obi answers, muted status/progress lines."""

    assert "talk-log" in TALK_SOURCE
    assert "MAX_ENTRIES" in TALK_SOURCE
    for role in ("'operator'", "'obi'", "'status'"):
        assert role in TALK_SOURCE, role


# --- the standalone talk server is GONE (033) ------------------------------------------------


def test_the_standalone_talk_server_module_is_retired() -> None:
    """033: the 8444 talk server merged into the unified origin — the module must not
    quietly return (its job lives in obi.web.server now)."""

    with pytest.raises(ModuleNotFoundError):
        import obi.voice.web.server  # noqa: F401


def test_static_bundle_uses_only_allowlisted_routes() -> None:
    """Every fetch() target in the served bundle is an allowlisted route or /api/turn —
    no privileged interface (the 024 talk-page discipline, now on the unified bundle)."""

    allowed = {
        "/",
        "/index.html",
        "/viewer.js",
        "/voice.js",
        "/links.json",
        "links.json",
        "/terrain.json",
        "terrain.json",
        # 098: the BRC city-grid artifact — one-shot intake, terrain.json-style
        "brc.json",
        "/voice.json",
        "voice.json",
        "/api/turn",
        # the local-data API routes (038/040/041) — read-only, explicitly dispatched
        "/api/logs.json",
        "/api/watch.json",
        "/api/inventory.json",
        "/api/metrics.json",
        "/api/configs.json",
        # 047: the shell resolves `/ROS <name|ip>` to a canonical address via the status
        # snapshot (a served, device-blind local file). The ROS/router widgets fetch through
        # the board driver's variable URL (/api/router.json), so only this literal appears here.
        "/snapshots/status.json",
        # 052: the OSPF map + drill-down + the shell's `/OSP <token>` resolver read the ospf
        # snapshot (a served, device-blind local file); no live route.
        "/snapshots/ospf.json",
    }
    for source_file in STATIC_DIR.glob("**/*.js"):
        if "vendor" in source_file.parts:
            continue
        source = source_file.read_text(encoding="utf-8")
        targets = re.findall(r"""fetch\(\s*["']([^"']+)["']""", source)
        assert set(targets) <= allowed, f"{source_file.name}: {targets}"
        assert "XMLHttpRequest" not in source
        assert "WebSocket" not in source
        assert "EventSource" not in source
