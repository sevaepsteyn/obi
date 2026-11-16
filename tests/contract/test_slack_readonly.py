"""The Slack interface reuses exactly the chat runtime's operator toolset.

Feature 012 adds
``snmp_metrics``, 013 adds ``logs_query``, 020 adds ``siklu_status``, 026 adds ``config_query``,
028 adds ``routeros_query``/``routeros_ping``/``routeros_traceroute``, 034 adds ``uisp_links``,
and 090 adds ``metrics_history`` to the shared toolset; the Slack adapter exposes them too
(it adds no tool of its own). Feature 105 removes ``unifi_status`` (registry-wide)
and ``audit_query`` (MCP-only now) from the conversational set Slack shares. Feature 103
changes this surface's contract DELIBERATELY (constitution 2.0.0): the conversational
runtime additionally carries the decorative-lighting write tools — the ONLY mutating tools
anywhere, statically ``low`` risk, allowlist-scoped, snapshot-backed — plus the read-only
``wled_status``.
"""

from __future__ import annotations

from pathlib import Path

from tests.fakes import chat_config, chat_runtime, text_response

_EXPECTED_TOOLS = {
    "inventory_list",
    "inventory_summary",
    "routeros_status",
    "routeros_neighbors",
    "uisp_status",
    "uisp_links",
    "snmp_metrics",
    "metrics_history",
    "query_health",
    # feature 118: the 095 board — the primary overall-status answer, local read.
    "dash_status",
    "weather_forecast",
    "gate_traffic",
    "logs_query",
    "siklu_status",
    "config_query",
    "routeros_query",
    "routeros_ping",
    "routeros_traceroute",
    # feature 103: the lighting read joins the registry; the two writes are the operator delta.
    "wled_status",
    "wled_set",
    "wled_revert",
    # feature 108: ADMIN's VictoriaMetrics store (second-tier, read-only, remote HTTP).
    "backbone_rf_health",
    "victoria_query",
    "victoria_discover",
    "weather_observed",
    # feature 111: mesh-node location (read-only; the position query is rate-limited).
    "mesh_locate",
    # feature 127: camp/art location from the local city datasets — contacts nothing.
    "playa_locate",
}


def test_slack_runtime_exposes_exactly_the_readonly_tools(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("noop")])

    # The Slack adapter answers through this same runtime/toolset — it adds no tool of its own.
    assert {tool.name for tool in runtime.tools} == _EXPECTED_TOOLS
    assert len(runtime.tools) == len(_EXPECTED_TOOLS)
    # feature 103: the wled writes are the ONLY mutating tools on this surface; everything
    # else stays read-only. (test_registry_readonly / test_operator_tools pin the boundary.)
    mutating = {tool.name for tool in runtime.tools if tool.mutating}
    assert mutating == {"wled_set", "wled_revert"}
    assert all(
        tool.risk_class == "read_only"
        for tool in runtime.tools
        if tool.name not in mutating
    )


def test_no_egress_tool_reachable_from_slack(tmp_path: Path) -> None:
    """Feature 104 FR-014: widened authorization widens WHO may ask, never WHAT obi can do.

    Egress actions — posting into Slack on request, radio transmission, paging — are CLI
    commands on the operator's machine (``obi slack say``, ``obi radio say``), never registry
    tools, so no Slack conversation (DM or channel, any authorization path) can reach them.
    (The 103 lighting writes are a deliberate, constitution-2.0.0 capability, not egress —
    they are covered by the mutating-set pin above.)
    """

    config = chat_config(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("noop")])

    for tool in runtime.tools:
        assert not any(word in tool.name for word in ("say", "page", "transmit", "post"))
