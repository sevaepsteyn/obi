"""Chat session orchestration: REPL + one-shot, preflight, and the kill switch (feature 005).

Builds the runtime from config, runs an interactive or one-shot conversation, and degrades
cleanly (a clear message + a non-zero exit) when the model is unreachable or unauthenticated —
the constitution v1.3.0 carve-out (Principle VI). The offline core (``discover``/``status``) is
never touched here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from obi.chat.agent import ConversationResult, OnTool, run_conversation
from obi.chat.context import load_context
from obi.core.audit import AuditTrail, record_event
from obi.core.config import ChatSettings, Config
from obi.core.drivers.mikrotik import MikrotikDriver
from obi.core.drivers.netbox import NetboxSource
from obi.core.drivers.siklu import SikluDriver
from obi.core.drivers.snmp import SnmpMetricsSource
from obi.core.drivers.sqlite_logs import SqliteLogSource
from obi.core.drivers.sqlite_metrics import SqliteMetricsHistorySource
from obi.core.drivers.uisp import UispController
from obi.core.drivers.unifi_network import UnifiNetworkController
from obi.core.drivers.victoria import VictoriaMetricsSource
from obi.core.llm import AnthropicLLMClient, AuditedLLMClient, ChatUnavailable, LLMClient
from obi.core.prompt import system_prompt
from obi.core.toolset import (
    ConfigBackupSource,
    MeshSource,
    ToolContext,
    ToolDefinition,
    WledSource,
    build_chat_tools,
    build_dispatch,
)

logger = logging.getLogger("obi")

_TRANSPARENCY = (
    "obi chat uses a cloud reasoning model. Your questions and the read-only results obi looks "
    "up are sent to it; your credentials are not."
)
# Appended to the transparency banner when an operator network-context file is loaded (FR-007).
_CONTEXT_NOTE = " Your configured network context is also included in what is sent to the model."
_EXIT_NOTE = " Type 'exit' to quit."


def resolve_api_key(chat: ChatSettings) -> str | None:
    """The chat API key from config, falling back to ``ANTHROPIC_API_KEY``; ``None`` if neither."""

    if chat.api_key is not None:
        return chat.api_key.reveal()
    return os.environ.get("ANTHROPIC_API_KEY")


@dataclass
class ChatRuntime:
    """Everything one chat session needs. Tests construct this directly with a stub client."""

    client: LLMClient
    ctx: ToolContext
    tools: list[ToolDefinition]
    model: str
    max_tokens: int
    # feature 008: the composed system prompt (built once; static for the session so it stays in
    # the cached prefix, FR-012), plus the context-load outcome for the operator-facing notices.
    system: str
    context_warning: str | None = None
    context_loaded: bool = False


def build_tool_context(config: Config) -> ToolContext:
    """Construct the read-only :class:`ToolContext` (driver + optional sources + audit) from config.

    Factored out of :func:`build_runtime` (feature 048) so a **non-LLM** caller can build the exact
    same tool wiring the chat agent uses **without an API key**. ``build_runtime`` additionally
    requires the key and a source preflight (chat needs a reasoning model and at least one source);
    a caller that only runs read-only tools — the web tool bridge (:mod:`obi.web.tool_api`) — needs
    neither. No source constructor does I/O on ``__init__`` (each just stores its settings), so this
    is cheap and safe to call fresh per request (thread-safe: no shared mutable ctx).

    Chat supports any source combination (features 006/010/013): build each accessor only when that
    source is configured; the Mikrotik/UISP/NetBox/logs handlers each gate on their accessor, so a
    ``None`` here is never dereferenced.
    """

    driver = MikrotikDriver(config.routeros) if config.routeros is not None else None
    uisp_source = UispController(config.uisp) if config.uisp is not None else None
    netbox_source = NetboxSource(config.netbox) if config.netbox is not None else None
    unifi_network_source = (
        UnifiNetworkController(config.unifi_network) if config.unifi_network is not None else None
    )
    # feature 012: SNMP is an added capability over the rosters above (not a source on its own), so
    # it never satisfies the preflight; it is wired only when [snmp] is configured.
    metrics_source = SnmpMetricsSource(config.snmp) if config.snmp is not None else None
    # feature 013: device syslog is a first-class source — a logs-only chat is valid.
    log_source = SqliteLogSource(config.logs) if config.logs is not None else None
    # feature 020: Siklu EtherHaul is a device-per-address source — a Siklu-only chat is valid.
    siklu_driver = SikluDriver(config.siklu) if config.siklu is not None else None
    # feature 026: the read-only config-backup store, wired only when [configs] is set. Like snmp,
    # it is a capability over the roster (not a standalone source) — never counts for preflight.
    config_source = ConfigBackupSource(config.configs) if config.configs is not None else None
    # feature 090: the stored time-series store, wired only when [snmp.history] opts in. A local
    # read that contacts nothing — so, like configs, it is a capability over the roster and never
    # counts for preflight. This one line is what puts throughput history in front of terminal
    # chat, Slack, voice, the web chat window, and the web tool bridge at once.
    metrics_history_source = (
        SqliteMetricsHistorySource(config.snmp.history)
        if config.snmp is not None and config.snmp.history is not None
        else None
    )
    # feature 103: the lighting allowlist, wired only when [wled] names devices. Like configs,
    # a capability (never counts for preflight). Which TOOLS see it is decided by the builder:
    # every surface gets the read-only wled_status; only build_operator_tools carries the writes.
    wled_source = WledSource(config.wled) if config.wled is not None else None
    # feature 111: the mesh-locate opt-in, wired only when [meshtastic] exists. A capability
    # (never counts for preflight); the handler owns freshness/rate-limit policy, this is
    # just the settings' presence.
    mesh_source = MeshSource(config.meshtastic) if config.meshtastic is not None else None
    # feature 108: the external VictoriaMetrics store, wired only when [victoria] is configured. A
    # capability over the roster (never counts for preflight); the second-tier remote metrics
    # source behind obi's own stores.
    victoria_source = (
        VictoriaMetricsSource(config.victoria) if config.victoria is not None else None
    )
    return ToolContext(
        config=config,
        driver=driver,
        uisp_source=uisp_source,
        netbox_source=netbox_source,
        unifi_network_source=unifi_network_source,
        metrics_source=metrics_source,
        log_source=log_source,
        metrics_history_source=metrics_history_source,
        siklu_driver=siklu_driver,
        config_source=config_source,
        wled_source=wled_source,
        mesh_source=mesh_source,
        victoria_source=victoria_source,
        audit=AuditTrail(config.audit_path),
    )


def build_runtime(config: Config) -> ChatRuntime:
    """Construct the runtime. Raises :class:`ChatUnavailable` when no API key is available."""

    api_key = resolve_api_key(config.chat)
    if not api_key:
        raise ChatUnavailable(
            "obi chat needs an API key for the reasoning model — set ANTHROPIC_API_KEY or "
            "[chat].api_key in your config. Your offline commands still work: "
            "`obi routeros discover`, `obi status`."
        )
    ctx = build_tool_context(config)
    # `load_config` already guarantees at least one source; the guard below is belt-and-suspenders
    # for a directly-constructed Config.
    if (
        ctx.driver is None
        and ctx.uisp_source is None
        and ctx.netbox_source is None
        and ctx.unifi_network_source is None
        and ctx.log_source is None
        and ctx.siklu_driver is None
    ):
        raise ChatUnavailable(
            "obi chat needs at least one configured source — add a [routeros], [uisp], [netbox], "
            "[unifi-network], [logs], and/or [siklu] section to your config. Your offline commands "
            "still work: `obi routeros discover`, `obi status`."
        )
    # Load the optional operator network context once and compose the static system prompt. A
    # missing/bad/oversized file degrades to no context + a warning (never raises), so chat
    # always starts (FR-005/006).
    context = load_context(config.chat)
    if context.source is not None:
        logger.info(
            "chat context",
            extra={
                "path": str(context.source),
                "size_bytes": context.size_bytes,
                "used": context.text is not None,
            },
        )
    return ChatRuntime(
        # feature 042: the audited decorator records every model call (full prompt/response +
        # token usage) through the ambient audit scope — a pass-through when capture is off.
        client=AuditedLLMClient(AnthropicLLMClient(api_key)),
        ctx=ctx,
        # feature 105 + 103: the conversational set — the operator toolset (registry plus
        # the wled write tools, constitution 2.0.0) minus CHAT_EXCLUDED_TOOL_NAMES
        # (audit_query is MCP-only). One seam covers chat, Slack, voice, web, and XR.
        tools=build_chat_tools(config),
        model=config.chat.model,
        max_tokens=config.chat.max_tokens,
        system=system_prompt(context.text, config=config),
        context_warning=context.warning,
        context_loaded=context.text is not None,
    )


def ask(
    runtime: ChatRuntime,
    messages: list[dict[str, Any]],
    question: str,
    *,
    on_tool: OnTool | None = None,
) -> ConversationResult:
    """Ask one question, appending the turns to ``messages`` and returning the answer."""

    runtime.ctx.question = question
    messages.append({"role": "user", "content": question})
    # feature 042: the shared human-communication seam — every surface (chat CLI, voice,
    # Slack) funnels through ask(), so one in/out pair here captures the whole turn. The
    # event's `source` (chat|voice|slack) comes from the surface's ambient audit scope.
    record_event(
        "human_message",
        summary=_first_line(question),
        payload={"direction": "in", "content": question, "peer": runtime.ctx.actor},
    )
    result = run_conversation(
        client=runtime.client,
        system=runtime.system,
        tools=[tool.schema() for tool in runtime.tools],
        messages=messages,
        model=runtime.model,
        max_tokens=runtime.max_tokens,
        dispatch=build_dispatch(runtime.ctx, runtime.tools),
        on_tool=on_tool,
    )
    record_event(
        "human_message",
        summary=_first_line(result.answer),
        payload={
            "direction": "out",
            "content": result.answer,
            "peer": runtime.ctx.actor,
            "tools_used": result.tools_used,
            # feature 089: the turn's summed token accounting, for the AUD list views.
            "input_tokens": result.usage.total_input(),
            "output_tokens": result.usage.output_tokens,
            "model_calls": result.usage.model_calls,
        },
    )
    return result


def _first_line(text: str, limit: int = 120) -> str:
    """A one-line summary of a turn for list views."""

    line = text.strip().splitlines()[0] if text.strip() else "(empty)"
    return line[:limit] + ("…" if len(line) > limit else "")


def run_once(runtime: ChatRuntime, question: str) -> ConversationResult:
    """Answer a single question (one-shot mode)."""

    return ask(runtime, [], question)


def print_answer(console: Console, answer: str) -> None:
    """Render an obi answer as markdown to the terminal (feature 082).

    Uses a rich ``Markdown`` renderable rather than interpolating the answer into a console-markup
    string, so its emphasis renders (``**bold**`` shows as bold, not literal asterisks) and any
    incidental ``[bracketed]`` text in the answer is shown verbatim — never parsed as rich markup,
    which previously could corrupt or crash the print.
    """

    console.print(Markdown(answer))


def run_interactive(runtime: ChatRuntime, console: Console, err: Console) -> int:
    """Run the interactive REPL. Returns the process exit code."""

    banner = _TRANSPARENCY + (_CONTEXT_NOTE if runtime.context_loaded else "") + _EXIT_NOTE
    console.print(f"[dim]{banner}[/dim]")
    messages: list[dict[str, Any]] = []

    def _notify(name: str, _input: dict[str, Any]) -> None:
        err.print(f"[dim]· {name}…[/dim]")

    while True:
        try:
            line = console.input("[bold cyan]you ›[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        text = line.strip()
        if not text:
            continue
        if text.lower() in {"exit", "quit"}:
            return 0
        try:
            result = ask(runtime, messages, text, on_tool=_notify)
        except ChatUnavailable as exc:
            err.print(f"[red]chat unavailable:[/red] {exc}")
            return 3
        except KeyboardInterrupt:
            console.print("\n[dim](interrupted)[/dim]")
            return 0
        console.print("[bold green]obi ›[/bold green]")
        print_answer(console, result.answer)
