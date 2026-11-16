"""The push-to-talk voice session (feature 023): the terminal consumer of the voice pipeline.

One session = one conversation. The chat brain is reused byte-for-byte via the same seam Slack
uses (``build_runtime`` + ``ask``); this module adds only the spoken transport: push-to-talk
capture, on-device transcription, spoken-style delivery, and the operator-control state machine
(interrupts, gating, degraded modes).

The turn primitive :func:`run_voice_turn` is surface-agnostic (FR-015): the planned web/AR
surfaces call it with their own transcript and callback, no terminal required.
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from rich.console import Console

from obi.chat.agent import ConversationResult, OnTool
from obi.chat.session import ChatRuntime, ask
from obi.core.config import VoiceSettings
from obi.core.llm import ChatUnavailable
from obi.core.models import AuditOutcome, AuditRecord, _utcnow
from obi.core.prompt import VOICE_SYSTEM_ADDENDUM
from obi.voice.audio import Recorder
from obi.voice.stt import Transcriber, model_is_cached
from obi.voice.tts import Speaker

logger = logging.getLogger("obi")

_TRANSPARENCY = (
    "obi voice uses a cloud reasoning model. Your transcribed questions and the read-only "
    "results obi looks up are sent to it; your credentials are not. What you say stays on "
    "this Mac — speech is captured, transcribed, and synthesized locally, and only the "
    "transcribed text is sent."
)
_CONTEXT_NOTE = " Your configured network context is also included in what is sent to the model."
IDLE_PROMPT = "[bold cyan]Enter[/bold cyan] to talk · [bold cyan]q[/bold cyan] to quit › "
RECORDING_NOTE = "● recording… press Enter to stop"
_NO_SPEECH = "didn't catch any speech — press Enter and try again"
_MIC_SILENT = (
    "the microphone delivered pure silence — if this keeps happening, check System Settings → "
    "Privacy & Security → Microphone and allow your terminal"
)


def voice_actor() -> str:
    """The audit-attribution id for this voice operator (Slack's user-id analogue)."""

    return f"voice:{getpass.getuser()}"


def ensure_model_available(
    settings: VoiceSettings,
    console: Console,
    input_fn: Callable[[str], str],
    *,
    is_cached: Callable[[str], bool] = model_is_cached,
    download: Callable[[str], None] | None = None,
) -> bool:
    """The FR-013 disclosure + eager fetch: consent means the download happens NOW, visibly.

    Returns ``False`` when the operator declines (the caller exits 0). When the model is
    already in the local HuggingFace cache there is nothing to disclose. On consent the
    model is fetched immediately (tqdm progress on the terminal) — never silently deferred
    into the first transcription.
    """

    from obi.voice.stt import approximate_download_size, download_model

    if is_cached(settings.stt_model):
        return True
    console.print(
        f"first run: the local speech model [bold]{settings.stt_model}[/bold] "
        f"({approximate_download_size(settings.stt_model)}) will be downloaded once and cached; "
        "after that, speech recognition works offline."
    )
    answer = input_fn("download now? [y/N] › ").strip().lower()
    if answer not in {"y", "yes"}:
        return False
    console.print(f"downloading {settings.stt_model}…")
    (download or download_model)(settings.stt_model)
    console.print("[green]done[/green] — the model is cached; this never happens again.")
    return True


def run_voice_turn(
    runtime: ChatRuntime,
    messages: list[dict[str, Any]],
    transcript: str,
    *,
    on_tool: OnTool | None = None,
) -> ConversationResult:
    """One voice turn against the chat engine, with the rollback the seam requires.

    ``ask()`` appends the user message before the LLM call and the tool loop appends
    mid-turn (chat/session.py:152, chat/agent.py:63/78); on ANY failure or interrupt the
    partial turn is rolled back — a dangling ``tool_use`` would poison every later call in
    the session (contracts/voice-pipeline.md item 4).
    """

    snapshot = len(messages)
    try:
        return ask(runtime, messages, transcript, on_tool=on_tool)
    except BaseException:
        del messages[snapshot:]
        raise


@dataclass
class VoiceSession:
    """One `obi voice` run: owns the conversation history and the operator-control loop."""

    runtime: ChatRuntime
    settings: VoiceSettings
    recorder: Recorder
    transcriber: Transcriber
    speaker: Speaker
    console: Console
    err: Console
    input_fn: Callable[[str], str]
    actor: str = field(default_factory=voice_actor)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The spoken-style addendum and the actor are applied exactly once, here — not per
        # turn — so the system prompt stays a stable cached prefix (research R5) and every
        # audited tool access is attributed (Principle V).
        self.runtime = replace(
            self.runtime,
            system=self.runtime.system + VOICE_SYSTEM_ADDENDUM,
            ctx=replace(self.runtime.ctx, actor=self.actor),
        )

    # --- the loop -----------------------------------------------------------------------------

    def run(self) -> int:
        """The push-to-talk loop. Returns the process exit code."""

        banner = _TRANSPARENCY + (_CONTEXT_NOTE if self.runtime.context_loaded else "")
        self.console.print(f"[dim]{banner}[/dim]")
        try:
            while True:
                try:
                    line = self.input_fn(IDLE_PROMPT)
                except (EOFError, KeyboardInterrupt):
                    self.console.print()
                    return 0
                if line.strip().lower() in {"q", "quit", "exit"}:
                    return 0
                self._turn()
        finally:
            self.speaker.stop()  # never leave an utterance playing on any exit path

    def _turn(self) -> None:
        """One push-to-talk cycle: record → gate → transcribe → think → print + speak."""

        self.console.print(f"[red]{RECORDING_NOTE}[/red]")
        try:
            audio = self.recorder.record()
        except (KeyboardInterrupt, EOFError):
            # Ctrl-C or a closed stdin mid-recording: discard the capture and fall back to
            # idle (a closed stdin will then EOF the idle prompt too → clean exit).
            self.console.print("[dim](recording discarded)[/dim]")
            return
        result = self.transcriber.transcribe(audio)
        del audio  # the capture ends here — nothing beyond this line ever sees it (FR-003)
        if result.gate == "mic_silent":
            self.console.print(f"[yellow]{_MIC_SILENT}[/yellow]")
            return
        if result.gate == "no_speech" or not result.text:
            detail = ""
            if result.seconds:
                detail = (
                    f" (heard {result.seconds:.1f}s, level {result.level:.4f} vs threshold "
                    f"{self.settings.rms_threshold} — speak up, or lower [voice] rms_threshold)"
                )
            self.console.print(f"[dim]{_NO_SPEECH}{detail}[/dim]")
            return
        self.console.print(f"[bold cyan]you ›[/bold cyan] {result.text}")

        def _notify(name: str, _input: dict[str, Any]) -> None:
            self.err.print(f"[dim]· {name}…[/dim]")

        try:
            conversation = run_voice_turn(
                self.runtime, self.messages, result.text, on_tool=_notify
            )
        except KeyboardInterrupt:
            self.console.print("\n[dim](interrupted — that question was abandoned)[/dim]")
            return
        except ChatUnavailable as exc:
            self._degraded(str(exc), result.text)
            return
        except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the session
            logger.exception("voice turn failed")
            self._degraded(f"that didn't work: {exc}", result.text)
            return
        self.console.print(f"[bold green]obi ›[/bold green] {conversation.answer}")
        try:
            self.speaker.speak(conversation.answer)
        except KeyboardInterrupt:
            self.speaker.stop()
            self.console.print("[dim](speech stopped)[/dim]")

    # --- degraded mode (FR-012) -----------------------------------------------------------------

    def _degraded(self, message: str, transcript: str) -> None:
        """Model unreachable / turn failed: say so out loud too, and audit the event."""

        self.console.print(f"[red]voice degraded:[/red] {message}")
        self.runtime.ctx.audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="voice:error",
                target="voice-session",
                reason=transcript,
                outcome=AuditOutcome.ERROR,
                detail=message,
                actor=self.actor,
            )
        )
        try:
            self.speaker.speak(f"Sorry — {message}")
        except KeyboardInterrupt:
            self.speaker.stop()
