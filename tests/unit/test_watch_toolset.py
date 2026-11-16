"""US1 — the watcher's investigation toolset is read-only by construction (feature 014, T020).

Feature 036 extends the set with the per-vendor read-only tools a human has in chat (uisp_status,
siklu_status, config_query) and unifies "consumes the live-call budget" into
BUDGETED_LIVE_TOOL_NAMES.
"""

from __future__ import annotations

from obi.core.toolset import (
    BUDGETED_LIVE_TOOL_NAMES,
    READ_ONLY,
    WATCH_TOOL_NAMES,
    build_watch_tools,
)


def test_watch_toolset_is_read_only() -> None:
    tools = build_watch_tools()
    assert tools, "the watch toolset must not be empty"
    for tool in tools:
        assert tool.risk_class == READ_ONLY, f"{tool.name} is not read-only"
        assert tool.mutating is False, f"{tool.name} is mutating"


def test_watch_toolset_excludes_unsafe_or_slow_tools() -> None:
    names = {tool.name for tool in build_watch_tools()}
    assert names == set(WATCH_TOOL_NAMES)
    # Discovery is admin/CLI-only (`obi routeros discover`); no agent toolset may reach it.
    assert "rescan" not in names
    # Feature 034: the watcher may consult live UISP link health (read-only, two GETs).
    assert "uisp_links" in names
    # Feature 036: the watcher reaches the same per-vendor read-only tools chat has.
    assert {"uisp_status", "siklu_status", "config_query"} <= names
    # Feature 090: stored metric history, so a finding can be quantified rather than asserted.
    assert "metrics_history" in names
    # Feature 108: ADMIN's store tools — remote HTTP reads, budgeted below.
    assert {
        "backbone_rf_health",
        "victoria_query",
        "victoria_discover",
        "weather_observed",
    } <= names
    # Feature 101: the audit tool is registry-wide but deliberately NOT the watcher's — an
    # investigation has no business reading obi's own trail mid-run.
    assert "audit_query" not in names


def test_mesh_locate_is_never_the_watchers() -> None:
    """Feature 111 FR-006: the mesh_locate tool can transmit a position request on shared
    LoRa airtime, so NO autonomous path may reach it — the watcher's exclusion is the
    enforcement, and this pin is the guarantee. Adding it to WATCH_TOOL_NAMES is a
    constitution-relevant decision, not a wiring convenience."""

    assert "mesh_locate" not in WATCH_TOOL_NAMES
    assert "mesh_locate" not in {tool.name for tool in build_watch_tools()}


def test_live_call_budget_consumers_are_the_budgeted_set() -> None:
    """028/036: the budget bounds exactly BUDGETED_LIVE_TOOL_NAMES — the three RouterOS live tools
    plus the UISP controller reads and Siklu SSH reads. config_query (local read) is excluded.
    111: mesh_locate is budgeted (it contacts the attached radio) but deliberately NOT offered
    to the watcher at all — the one allowed budgeted-minus-watch exception."""

    budgeted = set(BUDGETED_LIVE_TOOL_NAMES)
    assert budgeted - {"mesh_locate"} <= set(WATCH_TOOL_NAMES)
    assert {"routeros_query", "routeros_ping", "routeros_traceroute"} <= budgeted
    assert {"uisp_status", "uisp_links", "siklu_status"} <= budgeted
    # Feature 108: the victoria-store tools contact a remote HTTP host — always budgeted.
    assert {
        "backbone_rf_health",
        "victoria_query",
        "victoria_discover",
        "weather_observed",
    } <= budgeted
    assert "config_query" not in budgeted
    # Feature 090: metrics_history reads a LOCAL store and contacts nothing, so it neither spends
    # the budget nor gets stripped at budget 0 — the watcher can always quantify a finding.
    assert "metrics_history" not in budgeted
    surviving = {name for name in WATCH_TOOL_NAMES if name not in BUDGETED_LIVE_TOOL_NAMES}
    assert "metrics_history" in surviving
    # Feature 101: audit_query reads a LOCAL store and contacts nothing — never budgeted.
    assert "audit_query" not in budgeted
    # Feature 110: weather_forecast reads a LOCAL store and contacts nothing — offered to the
    # watcher, never budgeted, available even at budget 0.
    assert "weather_forecast" in surviving
    assert "weather_forecast" not in budgeted
    # Feature 112: gate_traffic reads a LOCAL store and contacts nothing — offered to the
    # watcher's investigation loop, never budgeted, available even at budget 0.
    assert "gate_traffic" in surviving
    assert "gate_traffic" not in budgeted
