"""Unit tests: the voice-web turn engine + conversation store (feature 024, T006/T013).

The engine is exercised with the offline doubles the 023 tests use (scripted LLM, fake
transcriber) plus a fake renderer — every event the contract documents is asserted here
without a server, a socket, or any hardware.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from obi.core.llm import ChatUnavailable, Usage
from obi.voice.stt import TranscriptionResult
from obi.voice.web.turns import (
    BusyError,
    ConversationStore,
    VoiceWebApp,
    valid_conversation_id,
)
from tests.fakes import (
    FakeTranscriber,
    chat_config,
    chat_runtime,
    pcm_speech,
    save_inventory,
    text_response,
    tool_response,
)

CID = "11112222-aaaa-bbbb-cccc-333344445555"


def heard(text: str, seconds: float = 1.2) -> TranscriptionResult:
    return TranscriptionResult(text=text, gate="ok", seconds=seconds, level=0.2)


def make_app(
    tmp_path: Path,
    *,
    responses: list[Any] | None = None,
    transcriber: Any | None = None,
    client: Any | None = None,
    idle_minutes: int = 30,
    clock: Any | None = None,
) -> VoiceWebApp:
    config = chat_config(tmp_path)
    runtime = chat_runtime(config, responses=responses or [text_response("all good.")])
    if client is not None:
        runtime = dataclasses.replace(runtime, client=client)
    store = ConversationStore(idle_minutes=idle_minutes, **({"clock": clock} if clock else {}))
    return VoiceWebApp(
        runtime=runtime,
        settings=config.voice,
        transcriber=transcriber or FakeTranscriber(results=[heard("is the link up?")]),
        render=lambda text: b"RIFF" + text.encode()[:8],
        store=store,
    )


def run_turn(app: VoiceWebApp, cid: str = CID, audio: Any | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    app.turn(cid, audio if audio is not None else pcm_speech(), events.append)
    return events


def audit_lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- US1: one turn end to end (T006) ---------------------------------------------------------


def test_successful_turn_streams_transcript_progress_answer(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        responses=[
            tool_response("routeros_status", {"address": "172.25.110.1"}),
            text_response("Yes — the link is up."),
        ],
    )
    events = run_turn(app)
    assert [e["event"] for e in events] == ["transcript", "progress", "answer"]
    assert events[0]["text"] == "is the link up?"
    assert events[0]["seconds"] == pytest.approx(1.2)
    assert events[1]["tool"] == "routeros_status"
    answer = events[2]
    assert answer["text"] == "Yes — the link is up."
    assert base64.b64decode(answer["audio_wav_b64"]).startswith(b"RIFF")
    assert answer["context_reset"] is True  # created by this request (page ignores 1st turn)


def test_second_turn_in_same_conversation_is_not_reset(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        responses=[text_response("one."), text_response("two.")],
        transcriber=FakeTranscriber(results=[heard("first"), heard("second")]),
    )
    first = run_turn(app)
    second = run_turn(app)
    assert first[-1]["context_reset"] is True
    assert second[-1]["context_reset"] is False


def test_gated_no_speech_never_reaches_the_model(tmp_path: Path) -> None:
    quiet = TranscriptionResult(text="", gate="no_speech", seconds=2.1, level=0.003)
    app = make_app(tmp_path, transcriber=FakeTranscriber(results=[quiet]))
    events = run_turn(app)
    assert [e["event"] for e in events] == ["gated"]
    assert events[0]["reason"] == "no_speech"
    assert events[0]["seconds"] == pytest.approx(2.1)
    assert events[0]["level"] == pytest.approx(0.003)
    assert app.runtime.client.calls == []  # type: ignore[attr-defined]


def test_gated_mic_silent_maps_to_silent_reason(tmp_path: Path) -> None:
    silent = TranscriptionResult(text="", gate="mic_silent", seconds=0.5, level=0.0)
    app = make_app(tmp_path, transcriber=FakeTranscriber(results=[silent]))
    events = run_turn(app)
    assert [e["event"] for e in events] == ["gated"]
    assert events[0]["reason"] == "silent"


class _UnavailableClient:
    def create_message(self, **kwargs: Any) -> Any:
        raise ChatUnavailable("the model is unreachable — check your API key / connectivity")


def test_model_unavailable_is_a_degraded_error_event_and_audited(tmp_path: Path) -> None:
    app = make_app(tmp_path, client=_UnavailableClient())
    events = run_turn(app)
    assert [e["event"] for e in events] == ["transcript", "error"]
    assert events[1]["reason"] == "model_unavailable"
    assert "unreachable" in events[1]["message"]
    records = audit_lines(tmp_path)
    assert any(
        r["action"] == "voice:error" and r["actor"] == f"voice-web:{CID[:8]}" for r in records
    )


class _ExplodingClient:
    def create_message(self, **kwargs: Any) -> Any:
        raise RuntimeError("kaboom")


def test_unexpected_failure_rolls_back_and_keeps_the_conversation_usable(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        transcriber=FakeTranscriber(results=[heard("q1"), heard("q2")]),
    )
    broken = dataclasses.replace(app.runtime, client=_ExplodingClient())
    good = app.runtime
    app.runtime = broken
    events = run_turn(app)
    assert events[-1]["event"] == "error"
    assert events[-1]["reason"] == "turn_failed"
    conversation = app.store.peek(CID)
    assert conversation is not None and conversation.messages == []  # rolled back
    app.runtime = good
    events = run_turn(app)  # the same conversation answers normally afterwards
    assert events[-1]["event"] == "answer"


def test_tool_audit_records_carry_the_web_actor(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        responses=[
            tool_response("routeros_status", {"target": "ghost-device"}),
            text_response("that device is unknown."),
        ],
    )
    save_inventory(app.runtime.ctx.config.inventory_path, [])  # audit path needs an inventory
    run_turn(app)
    records = audit_lines(tmp_path)
    tool_records = [r for r in records if r["action"] != "voice:error"]
    assert tool_records, "expected at least one tool audit record"
    assert all(r["actor"] == f"voice-web:{CID[:8]}" for r in tool_records)


def test_conversation_id_validation() -> None:
    assert valid_conversation_id(CID)
    assert valid_conversation_id("scripted-client-1")
    assert not valid_conversation_id("short")
    assert not valid_conversation_id("x" * 65)
    assert not valid_conversation_id("has spaces here")
    assert not valid_conversation_id("../../etc/passwd-etc-etc")


# --- US2: context, expiry, busy (T013) --------------------------------------------------------


def test_context_carries_within_a_conversation(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        responses=[text_response("the mountain link."), text_response("its other end is up.")],
        transcriber=FakeTranscriber(results=[heard("which link?"), heard("and the other end?")]),
    )
    run_turn(app)
    run_turn(app)
    client = app.runtime.client
    second_call_messages = client.calls[1]["messages"]  # type: ignore[attr-defined]
    texts = json.dumps(second_call_messages)
    assert "which link?" in texts  # the first turn's history reached the second call


def test_conversations_are_independent(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        responses=[text_response("a."), text_response("b.")],
        transcriber=FakeTranscriber(results=[heard("alpha"), heard("beta")]),
    )
    run_turn(app, cid="aaaaaaaa-1111-2222-3333-444444444444")
    events = run_turn(app, cid="bbbbbbbb-1111-2222-3333-444444444444")
    client = app.runtime.client
    second_call_messages = json.dumps(client.calls[1]["messages"])  # type: ignore[attr-defined]
    assert "alpha" not in second_call_messages  # no cross-conversation leakage
    assert events[-1]["context_reset"] is True  # its own fresh conversation


def test_idle_conversations_expire_and_reset_is_flagged(tmp_path: Path) -> None:
    now = [0.0]
    app = make_app(
        tmp_path,
        responses=[text_response("a."), text_response("b.")],
        transcriber=FakeTranscriber(results=[heard("one"), heard("two")]),
        idle_minutes=30,
        clock=lambda: now[0],
    )
    first = run_turn(app)
    assert first[-1]["context_reset"] is True
    now[0] += 31 * 60  # idle past the bound
    second = run_turn(app)
    assert second[-1]["context_reset"] is True  # swept + re-created
    client = app.runtime.client
    second_messages = json.dumps(client.calls[1]["messages"])  # type: ignore[attr-defined]
    assert "one" not in second_messages  # fresh context, no stale history


def test_racing_turn_on_one_conversation_is_busy(tmp_path: Path) -> None:
    store = ConversationStore(idle_minutes=30)
    conversation, created = store.begin_turn(CID)
    assert created is True
    with pytest.raises(BusyError):
        store.begin_turn(CID)
    store.end_turn(conversation)
    conversation2, created2 = store.begin_turn(CID)  # released → usable again
    assert created2 is False
    store.end_turn(conversation2)


def test_busy_turn_emits_the_error_event(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    conversation, _created = app.store.begin_turn(CID)  # hold the lock like an in-flight turn
    try:
        events = run_turn(app)
    finally:
        app.store.end_turn(conversation)
    assert [e["event"] for e in events] == ["error"]
    assert events[0]["reason"] == "busy"


# --- 056: context folding + conditional TTS (T002/T003) --------------------------------------


def _render_tracker() -> tuple[Callable[[str], bytes], list[str]]:
    """A render callable that records every answer it synthesizes (proves TTS ran / didn't)."""

    seen: list[str] = []

    def render(text: str) -> bytes:
        seen.append(text)
        return b"RIFF" + text.encode()[:8]

    return render, seen


def test_typed_turn_with_context_folds_into_the_prompt(tmp_path: Path) -> None:
    """A right-click-seeded typed turn: the model sees question + fenced context; the transcript
    event echoes ONLY the question (never the attached context) — data-model, contract R6."""

    app = make_app(tmp_path, responses=[text_response("looks healthy.")])
    events: list[dict[str, Any]] = []
    context = (
        "[window] ROS — RouterOS fleet; params: address=172.18.84.1\n"
        "[element]\nidentity: core-gw-1"
    )
    app.turn_text(CID, "is this healthy?", events.append, context=context)

    transcript = next(e for e in events if e["event"] == "transcript")
    assert transcript["text"] == "is this healthy?"
    assert "172.18.84.1" not in json.dumps(transcript)  # context never leaks into the echo

    prompt = json.dumps(app.runtime.client.calls[-1]["messages"])  # type: ignore[attr-defined]
    assert "is this healthy?" in prompt
    assert "operator's screen context" in prompt  # the fenced block header (R6)
    assert "172.18.84.1" in prompt and "core-gw-1" in prompt  # the full context reached the model


def test_spoken_turn_accepts_context_the_same_way(tmp_path: Path) -> None:
    """A seeded chat can be asked by MIC: the spoken turn folds the same context block; the
    transcript still echoes only the spoken question."""

    app = make_app(
        tmp_path,
        responses=[text_response("that is an error line.")],
        transcriber=FakeTranscriber(results=[heard("what does this mean?")]),
    )
    events: list[dict[str, Any]] = []
    app.turn(CID, pcm_speech(), events.append, context="[element]\nerror on eth3 down")

    prompt = json.dumps(app.runtime.client.calls[-1]["messages"])  # type: ignore[attr-defined]
    assert "what does this mean?" in prompt
    assert "operator's screen context" in prompt
    assert "error on eth3 down" in prompt
    transcript = next(e for e in events if e["event"] == "transcript")
    assert transcript["text"] == "what does this mean?"
    assert "eth3" not in json.dumps(transcript)


def test_typed_turn_answer_has_no_audio_and_skips_tts(tmp_path: Path) -> None:
    """056 voice policy (R5): a typed turn is answered TEXT ONLY — the answer event omits
    ``audio_wav_b64`` AND the TTS render callable is never invoked (no wasted synthesis)."""

    app = make_app(tmp_path, responses=[text_response("text only.")])
    render, seen = _render_tracker()
    app.render = render
    events: list[dict[str, Any]] = []
    app.turn_text(CID, "hello?", events.append)

    answer = events[-1]
    assert answer["event"] == "answer"
    assert "audio_wav_b64" not in answer
    assert answer["text"] == "text only."
    assert seen == []  # synthesis never ran for a typed turn


def test_spoken_turn_answer_still_carries_audio(tmp_path: Path) -> None:
    """A spoken turn is unchanged: the answer carries rendered speech, synthesized exactly once."""

    app = make_app(tmp_path, responses=[text_response("spoken answer.")])
    render, seen = _render_tracker()
    app.render = render
    events = run_turn(app)

    answer = events[-1]
    assert answer["event"] == "answer"
    assert base64.b64decode(answer["audio_wav_b64"]).startswith(b"RIFF")
    assert seen == ["spoken answer."]  # rendered once, for the spoken turn


def test_empty_context_is_treated_as_absent(tmp_path: Path) -> None:
    """Whitespace-only context ⇒ no fenced block (the chip was removed / never composed)."""

    app = make_app(tmp_path, responses=[text_response("ok.")])
    events: list[dict[str, Any]] = []
    app.turn_text(CID, "plain question", events.append, context="   \n  ")
    prompt = json.dumps(app.runtime.client.calls[-1]["messages"])  # type: ignore[attr-defined]
    assert "operator's screen context" not in prompt
    assert "plain question" in prompt


# --- 089: the per-turn report on the answer event (contracts/turn-report.md) ------------------


def test_answer_event_carries_the_turn_report(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        responses=[
            tool_response(
                "routeros_status",
                {"address": "172.25.110.1"},
                usage=Usage(input_tokens=100, output_tokens=20, cache_read_input_tokens=9000),
            ),
            text_response("Yes — the link is up.", usage=Usage(input_tokens=150, output_tokens=30)),
        ],
    )
    events = run_turn(app)
    report = events[-1]["report"]
    assert report["model"] == app.runtime.model
    (tool,) = report["tools"]
    assert tool["name"] == "routeros_status"
    assert tool["arguments"] == {"address": "172.25.110.1"}
    assert isinstance(tool["result"], str) and tool["result"]
    assert report["usage"]["input_tokens"] == 250
    assert report["usage"]["output_tokens"] == 50
    assert report["usage"]["cache_read_input_tokens"] == 9000
    assert report["usage"]["model_calls"] == 2
    assert report["cost_usd"] is not None  # the configured test model is priced
    json.dumps(events[-1])  # the enriched event must survive the NDJSON encoder


def test_tool_free_turn_reports_empty_tools(tmp_path: Path) -> None:
    app = make_app(tmp_path, responses=[text_response("just an answer.")])
    events = run_turn(app)
    report = events[-1]["report"]
    assert report["tools"] == []
    assert report["usage"]["model_calls"] == 1


def test_degraded_turns_emit_no_report(tmp_path: Path) -> None:
    app = make_app(tmp_path, client=_UnavailableClient())
    events = run_turn(app)
    assert events[-1]["event"] == "error"
    assert "report" not in events[-1]


def test_report_assembly_failure_never_blocks_the_answer(tmp_path: Path, monkeypatch: Any) -> None:
    """FR-013: a broken report drops the key, the answer still delivers."""

    import obi.voice.web.turns as turns_module

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("report kaboom")

    monkeypatch.setattr(turns_module, "web_report", broken)
    app = make_app(tmp_path, responses=[text_response("still answered.")])
    events = run_turn(app)
    assert events[-1]["event"] == "answer"
    assert events[-1]["text"] == "still answered."
    assert "report" not in events[-1]


def test_spoken_answer_audio_is_synthesized_from_text_only(tmp_path: Path) -> None:
    """FR-014: TTS renders the answer text — never any part of the report."""

    app = make_app(
        tmp_path,
        responses=[
            tool_response("routeros_status", {"address": "172.25.110.1"}),
            text_response("spoken answer."),
        ],
    )
    render, seen = _render_tracker()
    app.render = render
    events = run_turn(app)
    assert "report" in events[-1]
    assert seen == ["spoken answer."]  # the report contributed nothing to synthesis


def test_store_serializes_across_threads(tmp_path: Path) -> None:
    store = ConversationStore(idle_minutes=30)
    outcomes: list[str] = []
    conversation, _ = store.begin_turn(CID)

    def racer() -> None:
        try:
            store.begin_turn(CID)
            outcomes.append("acquired")
        except BusyError:
            outcomes.append("busy")

    thread = threading.Thread(target=racer)
    thread.start()
    thread.join()
    store.end_turn(conversation)
    assert outcomes == ["busy"]
