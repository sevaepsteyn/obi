"""Config-gated tool visibility (feature 137, T024): an unconfigured feature's tools are hidden.

The filter only ever REMOVES tools (never widens a surface): each tool declares the config
sections it needs in ``ToolDefinition.requires`` and every surface builder applies
``available_tools`` when given a config.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from obi.core.toolset import (
    available_tools,
    build_chat_tools,
    build_operator_tools,
    build_radio_tools,
    build_tools,
    build_watch_tools,
)

#: Tool → the config sections it needs (contracts/config-keys.md + the feature-111/103/108 wiring).
#: A configured-section stand-in carrying the attributes description rendering reads.
_SECTION = SimpleNamespace(
    devices={"gate": "http://192.0.2.5"}, descriptions={}, display_names={},
    display_name=None, place_name=None,
)  # fmt: skip

EXPECTED = {
    "weather_forecast": ("weather",),
    "gate_traffic": ("traffic",),
    "playa_locate": ("brc",),
    "backbone_rf_health": ("victoria",),
    "victoria_query": ("victoria",),
    "victoria_discover": ("victoria",),
    "weather_observed": ("victoria",),
    "mesh_locate": ("meshtastic",),
    "wled_status": ("wled",),
    "wled_set": ("wled",),
    "wled_revert": ("wled",),
}

SECTIONS = ("weather", "traffic", "brc", "victoria", "meshtastic", "wled")


def _config(**present: bool) -> SimpleNamespace:
    return SimpleNamespace(**{s: (_SECTION if present.get(s) else None) for s in SECTIONS})


def test_requires_declared_exactly() -> None:
    declared = {t.name: t.requires for t in build_operator_tools() if t.requires}
    assert declared == EXPECTED


def test_default_requires_is_empty() -> None:
    tool = build_tools()[0]
    assert replace(tool, requires=()).requires == ()


def test_empty_config_hides_every_gated_tool_on_every_surface() -> None:
    empty = _config()
    for tools in (
        build_operator_tools(empty),
        build_chat_tools(empty),
        build_watch_tools(empty),
        build_radio_tools("operator", empty),
    ):
        assert not {t.name for t in tools} & set(EXPECTED)


def test_full_config_keeps_everything() -> None:
    full = _config(**dict.fromkeys(SECTIONS, True))
    assert [t.name for t in build_operator_tools(full)] == [t.name for t in build_operator_tools()]


def test_filter_only_removes() -> None:
    partial = _config(weather=True)
    kept = {t.name for t in available_tools(build_operator_tools(), partial)}
    assert "weather_forecast" in kept and "gate_traffic" not in kept
    assert kept <= {t.name for t in build_operator_tools()}
