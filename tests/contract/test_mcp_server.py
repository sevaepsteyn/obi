"""T003/T016 (US1/US4): the MCP surface end-to-end over the SDK's in-memory transport.

Drives a real client↔server pair (initialize / tools/list / tools/call) with no subprocess,
no network, and no device: the served list must be the registry BY DERIVATION, a tool failure
must come back as a text result (never a protocol error) with the session still usable
(SC-007), and every call must land in the audit event store tagged ``source="mcp"`` under one
per-process ``mcp-<12hex>`` session id (SC-004).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import InitializeResult, TextContent

from obi.core import auditstore
from obi.core.config import Config
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from obi.core.toolset import build_operator_tools
from obi.mcp import server as mcp_server
from tests.fakes import chat_config, save_inventory


def _config(tmp_path: Path) -> Config:
    """A driver-less config (no [routeros]) so every test runs fully offline."""

    return chat_config(tmp_path, routeros=False)


def _session_run(
    config: Config,
    fn: Callable[[ClientSession, InitializeResult], Awaitable[Any]],
) -> Any:
    """Run ``fn`` against a live in-memory client session backed by obi's real server."""

    async def _main() -> Any:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tg:

                async def _serve() -> None:
                    await mcp_server.run_server(config, server_streams[0], server_streams[1])

                tg.start_soon(_serve)
                async with ClientSession(client_streams[0], client_streams[1]) as session:
                    init = await session.initialize()
                    result = await fn(session, init)
                tg.cancel_scope.cancel()
            return result

    return anyio.run(_main)


def _text(result: Any) -> str:
    """The single text block of a tools/call result."""

    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_initialize_and_served_list_is_the_operator_toolset(tmp_path: Path) -> None:
    """tools/list == build_operator_tools(config) — the registry plus the wled writes (2.0.0),
    minus tools whose config sections are unset (137) — names/descriptions/schemas verbatim
    (FR-002 retargeted)."""

    config = _config(tmp_path)

    async def check(session: ClientSession, init: InitializeResult) -> None:
        assert init.server_info.name == "obi"
        assert init.capabilities.tools is not None
        listed = (await session.list_tools()).tools
        registry = {t.name: t for t in build_operator_tools(config)}
        assert {t.name for t in listed} == set(registry)
        for tool in listed:
            assert tool.description == registry[tool.name].description
            assert tool.input_schema == registry[tool.name].input_schema

    _session_run(config, check)


def test_call_tool_round_trip_reads_local_store(tmp_path: Path) -> None:
    """A store-backed tool answers with real data through the protocol (US1 scenario 2)."""

    config = _config(tmp_path)
    save_inventory(
        config.inventory_path,
        [
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED),
            InventoryEntry("172.25.110.2", "ap", DiscoveryState.UNREACHABLE),
        ],
    )

    async def check(session: ClientSession, _init: InitializeResult) -> None:
        result = await session.call_tool("inventory_list", {"discovery_state": "any"})
        assert not result.is_error
        text = _text(result)
        assert "gw" in text and "ap" in text

    _session_run(config, check)


def test_tool_failure_is_a_text_result_and_session_survives(tmp_path: Path) -> None:
    """An unusable tool degrades to its own message, never a protocol error (SC-007)."""

    config = _config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )

    async def check(session: ClientSession, _init: InitializeResult) -> None:
        # No [routeros] configured: the tool must answer in words, not crash the session.
        result = await session.call_tool("routeros_status", {"device": "gw"})
        text = _text(result)
        assert text  # the tool's own not-configured/not-found message
        # The session keeps serving after the failure.
        follow_up = await session.call_tool("inventory_list", {})
        assert "gw" in _text(follow_up)

    _session_run(config, check)


def test_unknown_tool_is_a_protocol_error(tmp_path: Path) -> None:
    async def check(session: ClientSession, _init: InitializeResult) -> None:
        with pytest.raises(MCPError):
            await session.call_tool("definitely_not_a_tool", {})

    _session_run(_config(tmp_path), check)


def test_audit_query_reached_this_surface_by_derivation(tmp_path: Path) -> None:
    """T016 (US4): the new registry tool is served and round-trips with ZERO server changes —
    the feature's built-in proof of the derivation rule (SC-003)."""

    from datetime import UTC, datetime

    config = _config(tmp_path)
    conn = auditstore.connect(config.audit.db_path)
    try:
        auditstore.insert_event(
            conn,
            ts=datetime.now(UTC),
            kind="report",
            source="watch",
            session_id="watch-abc",
            target="birch-gw",
            summary="alert about birch-gw",
            payload={},
            duration_ms=None,
            outcome="ok",
            error=None,
            truncated=False,
            original_bytes=None,
        )
    finally:
        conn.close()

    async def check(session: ClientSession, _init: InitializeResult) -> None:
        listed = {t.name for t in (await session.list_tools()).tools}
        assert "audit_query" in listed
        result = await session.call_tool("audit_query", {"kind": "report"})
        text = _text(result)
        assert "alert about birch-gw" in text
        assert "shown 1 of" in text

    _session_run(config, check)


def test_every_call_is_audited_as_source_mcp(tmp_path: Path) -> None:
    """tool_call events land with source="mcp" and one shared session id (SC-004)."""

    config = _config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )

    async def check(session: ClientSession, _init: InitializeResult) -> None:
        await session.call_tool("inventory_list", {})
        await session.call_tool("inventory_summary", {})

    _session_run(config, check)

    conn = auditstore.connect_readonly(config.audit.db_path)
    try:
        events, total = auditstore.query_events(conn, kinds=["tool_call"], include_payload=True)
    finally:
        conn.close()
    assert total == 2
    assert {e["source"] for e in events} == {"mcp"}
    session_ids = {e["session_id"] for e in events}
    assert len(session_ids) == 1
    (session_id,) = session_ids
    assert session_id.startswith("mcp-") and len(session_id) == len("mcp-") + 12
    assert {e["payload"]["tool"] for e in events} == {"inventory_list", "inventory_summary"}
