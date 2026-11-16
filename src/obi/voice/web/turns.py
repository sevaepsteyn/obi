"""The voice-web turn engine + conversation registry (feature 024, research R1/R6).

One web turn = the 023 pipeline over a browser recording: gate → transcribe → the same
``run_voice_turn`` chat seam the terminal uses → server-rendered answer audio — emitted as
the contract's NDJSON events (contracts/voice-turn-api.md). Conversations are in-memory and
page-scoped: page-minted ids, per-conversation turn serialization, idle expiry
(clarifications 2026-07-02). No business logic lives here — transport and lifecycle only.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from obi.chat.report import web_report
from obi.chat.session import ChatRuntime
from obi.core.audit import AuditRecorder, audit_scope
from obi.core.config import VoiceSettings
from obi.core.llm import ChatUnavailable
from obi.core.models import AuditOutcome, AuditRecord, _utcnow
from obi.voice.session import run_voice_turn
from obi.voice.stt import Transcriber

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("obi")

Emit = Callable[[dict[str, Any]], None]

_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

# 056 R6: a right-click-seeded turn carries an operator-visible context block (the window
# identity + the clicked row/log line). It is folded into the question text as a fenced block
# rather than a new message role — the chat seam (``run_voice_turn`` → ``ask``) takes a plain
# string, so history/rollback semantics stay untouched and the composed prompt is what audit
# records (FR-022, exactly how 045 seeds Slack thread context). What the chip shows IS what the
# model sees.
_CONTEXT_HEADER = "[operator's screen context — attached from the web workspace]"


def _compose_prompt(question: str, context: str | None) -> str:
    """Fold an optional context block under the question (R6). Empty/whitespace ⇒ absent."""

    trimmed = (context or "").strip()
    if not trimmed:
        return question
    return f"{question}\n\n{_CONTEXT_HEADER}\n{trimmed}"


def valid_conversation_id(conversation_id: str) -> bool:
    """Contract sanity for the client-minted opaque id (never parsed beyond this)."""

    return bool(_CONVERSATION_ID.match(conversation_id))


class BusyError(Exception):
    """A turn is already in flight for this conversation (FR-008: never queue, say so)."""


@dataclass
class Conversation:
    """One browser conversation: the chat message history plus its serialization lock."""

    id: str
    actor: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_used: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ConversationStore:
    """In-memory registry of live conversations (research R6).

    ``begin_turn`` gets-or-creates the entry, sweeps idle neighbors, and takes the
    conversation's turn lock (non-blocking — a losing racer gets :class:`BusyError`, never a
    queue). ``end_turn`` refreshes ``last_used`` and releases. The injected ``clock`` keeps
    expiry testable (monotonic seconds).
    """

    def __init__(
        self, *, idle_minutes: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._idle_seconds = idle_minutes * 60.0
        self._clock = clock
        self._entries: dict[str, Conversation] = {}
        self._registry_lock = threading.Lock()

    def begin_turn(self, conversation_id: str) -> tuple[Conversation, bool]:
        """Acquire the conversation for one turn. Returns ``(conversation, created)``."""

        now = self._clock()
        with self._registry_lock:
            self._sweep(now)
            conversation = self._entries.get(conversation_id)
            created = conversation is None
            if conversation is None:
                conversation = Conversation(
                    id=conversation_id,
                    actor=f"voice-web:{conversation_id[:8]}",
                    last_used=now,
                )
                self._entries[conversation_id] = conversation
            if not conversation.lock.acquire(blocking=False):
                raise BusyError(
                    "a turn is already in flight for this conversation — wait for the answer"
                )
            conversation.last_used = now
            return conversation, created

    def end_turn(self, conversation: Conversation) -> None:
        conversation.last_used = self._clock()
        conversation.lock.release()

    def peek(self, conversation_id: str) -> Conversation | None:
        """The live entry, if any (tests + diagnostics; takes no lock on the entry)."""

        with self._registry_lock:
            return self._entries.get(conversation_id)

    def _sweep(self, now: float) -> None:
        """Drop idle conversations (lazy, on each request). In-flight turns are never swept."""

        for conversation_id, conversation in list(self._entries.items()):
            if now - conversation.last_used <= self._idle_seconds:
                continue
            if conversation.lock.locked():
                continue  # a turn is running right now; last_used refreshes on end_turn
            del self._entries[conversation_id]
            logger.info(
                "voice-web conversation expired",
                extra={"conversation": conversation_id[:8]},
            )


@dataclass
class VoiceWebApp:
    """Everything one voice-web service run holds: the runtime and the pipeline pieces.

    ``runtime`` arrives with the spoken-style addendum already applied (exactly once, at
    service start — the system prompt stays a stable cached prefix, 023 research R5).
    ``render`` is pre-bound to the configured obi voice. The single ``stt_lock`` serializes
    transcription — one MLX model instance on one GPU (research R6); LLM phases of different
    conversations may overlap freely.
    """

    runtime: ChatRuntime
    settings: VoiceSettings
    transcriber: Transcriber
    render: Callable[[str], bytes]
    store: ConversationStore
    stt_lock: threading.Lock = field(default_factory=threading.Lock)
    # feature 042: the service-lifetime audit recorder; each turn opens a per-conversation
    # capture scope over it inside the serving thread (contextvars are per-thread).
    recorder: AuditRecorder | None = None

    def turn(
        self,
        conversation_id: str,
        audio: np.ndarray,
        emit: Emit,
        *,
        context: str | None = None,
    ) -> None:
        """Run one voice turn, emitting contract events as they happen (FR-005/007/009/010).

        Exactly one terminal event (``answer`` | ``gated`` | ``error``) ends every call; no
        exception escapes (one bad turn must never kill the service). ``context`` (056) is an
        optional operator-attached screen-context block folded into the question (R6); a
        spoken turn is answered with rendered speech (``speak=True``).
        """

        try:
            conversation, created = self.store.begin_turn(conversation_id)
        except BusyError as exc:
            emit({"event": "error", "reason": "busy", "message": str(exc)})
            return
        try:
            with self.stt_lock:
                result = self.transcriber.transcribe(audio)
            del audio  # the capture ends here — nothing beyond this line sees it (FR-004)
            if result.gate != "ok" or not result.text:
                emit(
                    {
                        "event": "gated",
                        "reason": "silent" if result.gate == "mic_silent" else "no_speech",
                        "seconds": round(result.seconds, 2),
                        "level": round(result.level, 4),
                    }
                )
                return
            emit(
                {"event": "transcript", "text": result.text, "seconds": round(result.seconds, 2)}
            )
            self._respond(conversation, created, result.text, emit, context=context, speak=True)
        finally:
            self.store.end_turn(conversation)

    def turn_text(
        self,
        conversation_id: str,
        text: str,
        emit: Emit,
        *,
        context: str | None = None,
    ) -> None:
        """Run one TYPED turn (033): same conversation, same tools, same event protocol.

        The transcript event echoes the typed text (flagged ``typed``) so every client renders
        the two input modes identically; STT never runs and no audio ever existed. 056 voice
        policy (R5): a typed turn is answered as TEXT ONLY — ``speak=False`` skips TTS synthesis
        entirely, so the answer event carries no ``audio_wav_b64`` (and typed turns get faster).
        ``context`` folds an operator-attached screen-context block into the question (R6).
        """

        try:
            conversation, created = self.store.begin_turn(conversation_id)
        except BusyError as exc:
            emit({"event": "error", "reason": "busy", "message": str(exc)})
            return
        try:
            emit({"event": "transcript", "text": text, "typed": True})
            self._respond(conversation, created, text, emit, context=context, speak=False)
        finally:
            self.store.end_turn(conversation)

    def _respond(
        self,
        conversation: Conversation,
        created: bool,
        question: str,
        emit: Emit,
        *,
        context: str | None = None,
        speak: bool = True,
    ) -> None:
        """The shared reasoning+answer path behind both input modes (spoken and typed).

        ``context`` is folded into the prompt the model sees (R6); the ``transcript`` event
        (emitted by the caller) still echoes only the question. ``speak`` gates TTS synthesis:
        the ``answer`` event carries ``audio_wav_b64`` iff the turn was spoken (056 R5).
        """

        runtime = replace(self.runtime, ctx=replace(self.runtime.ctx, actor=conversation.actor))
        prompt = _compose_prompt(question, context)

        def _notify(name: str, _input: dict[str, Any]) -> None:
            emit({"event": "progress", "tool": name})

        try:
            with audit_scope(self.recorder, "voice", f"chat-web-{conversation.id}"):
                outcome = run_voice_turn(
                    runtime, conversation.messages, prompt, on_tool=_notify
                )
        except ChatUnavailable as exc:
            self._degraded(conversation, "model_unavailable", str(exc), prompt, emit)
            return
        except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the service
            logger.exception("voice-web turn failed")
            self._degraded(
                conversation, "turn_failed", f"that didn't work: {exc}", prompt, emit
            )
            return
        answer: dict[str, object] = {
            "event": "answer",
            "text": outcome.answer,
            "context_reset": created,
        }
        # 089: the per-turn report (tools + tokens + cost, contracts/turn-report.md). Assembly
        # failure omits the key — the answer always wins (FR-013). TTS below renders only
        # ``outcome.answer``, so the report is never spoken (FR-014).
        try:
            answer["report"] = web_report(outcome, self.runtime.model)
        except Exception:  # noqa: BLE001 - report chrome must never block the reply
            logger.exception("voice-web turn report assembly failed")
        if speak:
            answer["audio_wav_b64"] = base64.b64encode(self.render(outcome.answer)).decode()
        emit(answer)

    def _degraded(
        self,
        conversation: Conversation,
        reason: str,
        message: str,
        transcript: str,
        emit: Emit,
    ) -> None:
        """Degraded turn (FR-013): a clear error event, and the event is audited (FR-016)."""

        emit({"event": "error", "reason": reason, "message": message})
        self.runtime.ctx.audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="voice:error",
                target="voice-web",
                reason=transcript,
                outcome=AuditOutcome.ERROR,
                detail=message,
                actor=conversation.actor,
            )
        )
