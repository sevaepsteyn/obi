"""The agent toolset is EXACTLY the read-only tools (read-only by construction).

Feature 006 adds ``uisp_status``; 012 adds ``snmp_metrics`` (live SNMP); 013 adds ``logs_query``
(device syslog); 020 adds ``siklu_status`` (Siklu EtherHaul radios/links); 026 adds
``config_query`` (stored RouterOS config backup); 028 adds
``routeros_query``/``routeros_ping``/``routeros_traceroute`` (live read-only RouterOS access);
034 adds ``uisp_links`` (UISP wireless link RF health); 090 adds ``metrics_history`` (stored
SNMP time series, as rates). 105 REMOVES ``unifi_status`` (feature 011) registry-wide — playa
wifi is another team's system — and splits the conversational set from the registry:
``build_chat_tools()`` is the conversational set (``audit_query`` is MCP-only). 103 makes
its base the OPERATOR toolset, adding the two wled write tools (constitution 2.0.0) — the
REGISTRY stays entirely read-only / non-mutating (test_registry_readonly pins that).
"""

from __future__ import annotations

from obi.core.toolset import (
    CHAT_EXCLUDED_TOOL_NAMES,
    READ_ONLY,
    build_chat_tools,
    build_tools,
)

_EXPECTED = {
    "inventory_list",
    "inventory_summary",
    "routeros_status",
    "routeros_neighbors",
    "uisp_status",
    "uisp_links",
    "snmp_metrics",
    "metrics_history",
    "query_health",
    # feature 118: the 095 status-dashboard board — a contacts-nothing local read, the
    # primary overall-status answer.
    "dash_status",
    # feature 110: BRC weather from the local snapshot store — a contacts-nothing read.
    "weather_forecast",
    # feature 112: gate travel times + gate status from the local traffic store — same class.
    "gate_traffic",
    "logs_query",
    "siklu_status",
    "config_query",
    # feature 101: obi's own audit trail, summaries only — a local store read. Registered,
    # but since 105 served ONLY via MCP (see the chat-set test below).
    "audit_query",
    "routeros_query",
    "routeros_ping",
    "routeros_traceroute",
    # feature 103: the read-only lighting status lives in the registry (the writes do NOT).
    "wled_status",
    # feature 108: ADMIN's VictoriaMetrics store (second-tier, read-only, remote HTTP).
    "backbone_rf_health",
    "victoria_query",
    "victoria_discover",
    "weather_observed",
    # feature 111: mesh-node location — read-only (its one possible transmission is a
    # rate-limited position QUERY, the ping/traceroute class; never reachable autonomously).
    "mesh_locate",
    # feature 127: camp/art location from the local city datasets — contacts nothing.
    "playa_locate",
}


def test_exactly_the_read_only_toolset() -> None:
    tools = build_tools()

    assert {tool.name for tool in tools} == _EXPECTED
    for tool in tools:
        assert tool.risk_class == READ_ONLY
        assert tool.mutating is False
        schema = tool.schema()
        assert schema["name"] == tool.name
        assert schema["input_schema"]["type"] == "object"


def test_unifi_status_stays_disabled() -> None:
    # Feature 105 (operator decision 2026-08-26): no live-UniFi tool on ANY surface for now.
    # Re-enabling is a deliberate act: re-register in build_tools() and update this pin.
    assert "unifi_status" not in {tool.name for tool in build_tools()}


def test_chat_set_is_the_registry_minus_the_mcp_only_tools() -> None:
    # Feature 105: the conversational surfaces (chat/Slack/voice/web/XR share one runtime)
    # get the registry minus the operator-only tools: audit_query (105, operator-only
    # disclosure) and radio_call (124, outbound calls originate from CLI+MCP only —
    # operator decision 2026-09-02). Both stay reachable via MCP (Claude Desktop/Code).
    assert set(CHAT_EXCLUDED_TOOL_NAMES) == {"audit_query", "radio_call"}
    # 103/2.0.0: the chat set's base is the operator toolset, so the wled writes ride in.
    assert {tool.name for tool in build_chat_tools()} == (
        _EXPECTED - set(CHAT_EXCLUDED_TOOL_NAMES)
    ) | {"wled_set", "wled_revert"}
