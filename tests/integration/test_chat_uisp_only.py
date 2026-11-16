"""Integration (US3): chat answers on a UISP-only config (no Mikrotik driver), and a
hallucinated `rescan` tool call gets a graceful unknown-tool error (discovery is admin/CLI-only
via `obi routeros discover`; the agent has no rescan tool)."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.models import Device, DeviceStatus, Reachability
from tests.fakes import (
    FakeSource,
    chat_config,
    chat_runtime,
    text_response,
    tool_response,
    uisp_settings,
)


def test_uisp_only_answers_uisp_question(tmp_path: Path) -> None:
    config = chat_config(tmp_path, routeros=False, uisp=uisp_settings())
    up = DeviceStatus(
        Device("ridge-ap", "10.9.0.5"),
        Reachability.REACHABLE,
        uptime_seconds=10,
        cpu_load_percent=2.0,
        free_memory_bytes=1_000_000,
    )
    runtime = chat_runtime(
        config,
        responses=[tool_response("uisp_status", {}), text_response("UISP ridge-ap is up.")],
        uisp_source=FakeSource([up]),
        with_driver=False,
    )

    result = ask(runtime, [], "is ridge-ap up?")

    assert result.tools_used == ["uisp_status"]
    assert "ridge-ap" in result.answer


def test_rescan_tool_is_gone_and_fails_gracefully(tmp_path: Path) -> None:
    config = chat_config(tmp_path, routeros=False, uisp=uisp_settings())
    runtime = chat_runtime(
        config,
        responses=[tool_response("rescan", {}), text_response("obi cannot rescan; read-only.")],
        uisp_source=FakeSource([]),
        with_driver=False,
    )

    ask(runtime, [], "refresh the inventory")

    # Discovery is admin/CLI-only (`obi routeros discover`): the agent has no rescan tool, and a
    # hallucinated call gets an unknown-tool error fed back instead of crashing the loop.
    assert all(tool.name != "rescan" for tool in runtime.tools)
    fed_back = str(runtime.client.calls[-1]["messages"])
    assert "unknown tool" in fed_back
