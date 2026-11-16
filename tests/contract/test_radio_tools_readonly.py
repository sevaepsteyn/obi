"""Contract (feature 088, redrawn by constitution 2.0.0): the radio tiers are static, and
NETWORK devices stay untouchable from RF.

The original FR-016 guarantee ("nothing state-changing is reachable from RF") was narrowed
by operator decision 2026-08-26: the decorative-lighting write tools (feature 103) are on
this surface for ANY caller — low-risk, snapshot-revertible, audited per claimed handle.
This guard now pins the mutating set to EXACTLY those two tools; everything else on the
tiers must remain read-only. The tiers are code, not config (the validate_web_tools shape).
"""

from __future__ import annotations

from obi.core.toolset import (
    RADIO_OPERATOR_TOOL_NAMES,
    RADIO_SAFE_TOOL_NAMES,
    build_operator_tools,
    build_radio_tools,
    validate_radio_tools,
)


def test_validate_radio_tools_passes() -> None:
    validate_radio_tools()


def test_tiers_are_collapsed() -> None:
    # Operator decision 2026-08-20: research R8's reduced safe tier is retired — every caller
    # on channel gets the same set. Read-only is what keeps this safe (asserted below); the
    # tiers only ever governed DISCLOSURE, never the ability to act.
    assert set(RADIO_SAFE_TOOL_NAMES) == set(RADIO_OPERATOR_TOOL_NAMES)


def test_radio_toolset_is_the_registry_minus_exclusions() -> None:
    # audit_query (feature 101) is excluded: RF carries no authentication and the trail
    # summarizes everything obi does — operator-only disclosure, not traffic for the channel.
    # (unifi_status, once excluded HERE — playa wifi is another team's system — is gone from
    # the registry entirely since feature 105, so the radio subtraction no longer names it.)
    # Since 103/2.0.0 the base is the OPERATOR toolset, so the wled writes ride in.
    # radio_call (feature 124) is excluded too: outbound calls originate from the CLI
    # and MCP only — a radio caller must never be able to make obi hail someone else.
    assert set(RADIO_OPERATOR_TOOL_NAMES) == {tool.name for tool in build_operator_tools()} - {
        "audit_query",
        "radio_call",
    }


def test_unifi_is_not_reachable_from_the_radio() -> None:
    # Registry-wide since 105, but this stays as the radio-side pin: RF never sees UniFi.
    for tier in ("safe", "operator"):
        assert "unifi_status" not in {tool.name for tool in build_radio_tools(tier)}


def test_mutating_set_is_exactly_the_wled_writes() -> None:
    by_name = {tool.name: tool for tool in build_operator_tools()}
    for name in (*RADIO_SAFE_TOOL_NAMES, *RADIO_OPERATOR_TOOL_NAMES):
        assert name in by_name, f"{name} is not a registered obi tool"
    mutating = {name for name in set(RADIO_SAFE_TOOL_NAMES) if by_name[name].mutating}
    # Constitution 2.0.0: decorative lighting only — network devices stay read-only from RF.
    assert mutating == {"wled_set", "wled_revert"}
    for name in set(RADIO_SAFE_TOOL_NAMES) - mutating:
        assert by_name[name].risk_class == "read_only"


def test_radio_persona_carries_the_channel_guidelines() -> None:
    from obi.core.prompt import RADIO_SYSTEM_ADDENDUM

    text = RADIO_SYSTEM_ADDENDUM.lower()
    for required in (
        "brief",
        "rated g",
        "operational",
        "over",
        "read-only",
        "affirmative",
        "negative",
        "stand by",
        "say again",
        "radio check",
        "decline",  # the tier-decline instruction (analysis G2)
    ):
        assert required in text, f"persona is missing {required!r}"
    assert "clear" in text  # obi never clears — the instruction must be present


def test_radio_addendum_carries_the_channel_vocabulary() -> None:
    # Field 2026-08-20 ("NOC is a lot more likely than North"): the composer's persona
    # lists the channel's real vocabulary so the model arbitrates mis-hears with priors.
    from obi.core.prompt import RADIO_SYSTEM_ADDENDUM, radio_system_addendum

    text = radio_system_addendum(
        vocab_terms=["NOC", "manzanita", "mountain"], callsigns=["Mira", "Cat"]
    )
    assert text.startswith(RADIO_SYSTEM_ADDENDUM)  # the base persona is intact
    assert "NOC, manzanita, mountain" in text
    assert "Mira, Cat" in text
    assert "close in sound" in text
    # Bare call ⇒ exactly the base persona, no dangling vocabulary block.
    assert radio_system_addendum() == RADIO_SYSTEM_ADDENDUM


def test_query_health_is_on_the_radio_and_the_watcher() -> None:
    """Feature 100: the health tool rides both restricted surfaces — a local, contacts-
    nothing read (the metrics_history precedent), deliberately NOT budgeted."""

    from obi.core.toolset import (
        BUDGETED_LIVE_TOOL_NAMES,
        WATCH_TOOL_NAMES,
        build_radio_tools,
    )

    assert "query_health" in {t.name for t in build_radio_tools("operator")}
    assert "query_health" in {t.name for t in build_radio_tools("safe")}
    assert "query_health" in WATCH_TOOL_NAMES
    assert "query_health" not in BUDGETED_LIVE_TOOL_NAMES


def test_dash_status_is_on_the_radio_and_the_watcher() -> None:
    """Feature 118: the board tool rides both restricted surfaces — a local, contacts-
    nothing file read (the query_health precedent), deliberately NOT budgeted; its payload
    bound (test_toolset_dash_status worst case ≤ 1,600 chars) covers the on-air budget."""

    from obi.core.toolset import (
        BUDGETED_LIVE_TOOL_NAMES,
        WATCH_TOOL_NAMES,
        build_radio_tools,
    )

    assert "dash_status" in {t.name for t in build_radio_tools("operator")}
    assert "dash_status" in {t.name for t in build_radio_tools("safe")}
    assert "dash_status" in WATCH_TOOL_NAMES
    assert "dash_status" not in BUDGETED_LIVE_TOOL_NAMES


def test_weather_forecast_is_on_the_radio_and_the_watcher() -> None:
    """Feature 110: the weather tool rides both restricted surfaces — a local, contacts-
    nothing snapshot read (the query_health precedent), deliberately NOT budgeted; its
    payload bound (test_toolset_weather worst case ≤ 1,600 chars) covers the on-air budget."""

    from obi.core.toolset import (
        BUDGETED_LIVE_TOOL_NAMES,
        WATCH_TOOL_NAMES,
        build_radio_tools,
    )

    assert "weather_forecast" in {t.name for t in build_radio_tools("operator")}
    assert "weather_forecast" in {t.name for t in build_radio_tools("safe")}
    assert "weather_forecast" in WATCH_TOOL_NAMES
    assert "weather_forecast" not in BUDGETED_LIVE_TOOL_NAMES


def test_gate_traffic_is_on_the_radio_and_the_watcher() -> None:
    """Feature 112 (US3/SC-006): the gate-traffic tool rides both restricted surfaces — a
    local, contacts-nothing store read (the weather_forecast/query_health precedent),
    deliberately NOT budgeted; its payload bound (test_toolset_traffic worst case ≤ 1,600
    chars) covers the on-air reply budget, so a radio call like "how long is the gate
    line?" composes inside the existing transmission machinery."""

    from obi.core.toolset import (
        BUDGETED_LIVE_TOOL_NAMES,
        WATCH_TOOL_NAMES,
        build_radio_tools,
    )

    assert "gate_traffic" in {t.name for t in build_radio_tools("operator")}
    assert "gate_traffic" in {t.name for t in build_radio_tools("safe")}
    assert "gate_traffic" in WATCH_TOOL_NAMES
    assert "gate_traffic" not in BUDGETED_LIVE_TOOL_NAMES
