"""The stdio MCP server: obi's operator toolset served to local MCP clients.

The served list is DERIVED from ``core.toolset.build_operator_tools()`` (constitution 2.0.0:
MCP clients are operator-run processes on obi's machine, chat's trust class — the registry
plus the decorative-lighting write tools, feature 103): a tool added there appears here with
zero surface work, and excluding one would take explicit
code in :func:`served_tools` — there is deliberately no name list in this module. Dispatch
reuses chat's exact wiring (``build_tool_context`` + ``build_dispatch``: never-raise handlers,
``tool_call`` audit events), so a tool failure returns the tool's own message and the session
keeps serving. One server process serves one client connection, with one ambient audit scope
(``source="mcp"``) for its lifetime; ``live_budget`` stays ``None`` — this surface is
operator-trusted like chat/voice, only per-call tool bounds apply.

stdout is the protocol channel: nothing in this module prints, and obi's logging is
stderr-only by construction (``core.logging``).
"""

from __future__ import annotations

import importlib.metadata
import uuid
from typing import Any

import anyio
import anyio.to_thread
from mcp.server import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared._stream_protocols import ReadStream, WriteStream
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from obi.core.audit import audit_scope, recorder_from_config
from obi.core.config import Config
from obi.core.toolset import build_dispatch, build_operator_tools

SERVER_NAME = "obi"

_INSTRUCTIONS_BASE = (
    "Tools over obi's network: device inventory and status (RouterOS, UISP, UniFi, Siklu), "
    "stored metrics/logs/config/audit history, and live read-only diagnostics (status, query, "
    "ping, traceroute). Network devices are strictly read-only."
)
_INSTRUCTIONS_LIGHTING = (
    " The ONE exception (feature 103): wled_set/wled_revert change the decorative WLED "
    "lighting ({devices}) — allowlisted devices only, the overwritten look is captured first and "
    "wled_revert restores it, dry_run previews, every write is audited."
)


def build_instructions(config: Config | None = None) -> str:
    """The MCP server instructions; lighting text from ``[wled]`` (feature 137)."""

    from obi.core.prompt import _lighting_devices

    if config is not None and config.wled is None:
        return _INSTRUCTIONS_BASE
    devices, _mapping = _lighting_devices(config)
    return _INSTRUCTIONS_BASE + _INSTRUCTIONS_LIGHTING.format(devices=devices)


#: The registry-view instructions (no config).
_INSTRUCTIONS = build_instructions()


def served_tools(config: Config | None = None) -> list[Tool]:
    """The operator toolset, verbatim: names/descriptions/schemas from ``build_operator_tools()``.

    This is the derivation seam the constitution's rule points at (1.14.0, retargeted by
    2.0.0) — any future exclusion must be an explicit, visible filter HERE, and the parity
    test pins equality. Feature 137's one filter: given ``config``, a tool whose required
    sections are unconfigured is dropped (``ToolDefinition.requires`` via ``available_tools``)
    — it only ever narrows, exactly as every other surface does.
    """

    return [
        Tool(name=t.name, description=t.description, input_schema=t.input_schema)
        for t in build_operator_tools(config)
    ]


def _version() -> str:
    try:
        return importlib.metadata.version("obi")
    except importlib.metadata.PackageNotFoundError:  # not installed (e.g. odd test env)
        return ""


def create_server(config: Config) -> Server[Any]:
    """Wire the low-level MCP server over chat's exact tool context and dispatch."""

    # Local import: pulls the chat session module (rich, LLM client types) only when a server
    # is actually built, keeping `import obi.mcp.server` light for the parity test.
    from obi.chat.session import build_tool_context

    ctx = build_tool_context(config)
    dispatch = build_dispatch(ctx, build_operator_tools(config))
    # One recorder + session id per server process = one audited session per client
    # connection. The scope is entered around EACH dispatched call (in its worker thread)
    # rather than around server.run: the SDK's context streams run request handlers in the
    # context captured at the transport, so a scope opened around the serve loop would be
    # invisible to them.
    recorder = recorder_from_config(config)
    session_id = f"mcp-{uuid.uuid4().hex[:12]}"

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=served_tools(config))

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        run = dispatch.get(params.name)
        if run is None:
            raise MCPError(INVALID_PARAMS, f"unknown tool: {params.name}")
        arguments = params.arguments or {}

        def scoped_run() -> str:
            with audit_scope(recorder, "mcp", session_id):
                return run(arguments)

        # Handlers are sync and may block on a device for seconds — run off the protocol
        # loop so pings and a second call keep being serviced.
        result = await anyio.to_thread.run_sync(scoped_run)
        # Same error convention build_dispatch itself uses for the audit record.
        return CallToolResult(
            content=[TextContent(type="text", text=result)],
            is_error=result.startswith("error"),
        )

    return Server(
        name=SERVER_NAME,
        version=_version(),
        instructions=build_instructions(config),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def run_server(
    config: Config,
    read_stream: ReadStream[SessionMessage | Exception],
    write_stream: WriteStream[SessionMessage],
) -> None:
    """Serve one client connection on the given streams.

    Split from :func:`serve` so the contract test can drive the real server over the SDK's
    in-memory transport.
    """

    server = create_server(config)
    await server.run(read_stream, write_stream, server.create_initialization_options())


def serve(config: Config) -> None:
    """Run the stdio server until the client closes stdin (the ``obi mcp serve`` entrypoint)."""

    async def _main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await run_server(config, read_stream, write_stream)

    anyio.run(_main)
