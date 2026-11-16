"""Feature 137 (T036/T037): every LLM-facing string is free of deployment identity.

Renders the chat system prompt, the radio persona, the MCP instructions and every surface's tool
descriptions under (a) no config, (b) an empty config and (c) a full FICTIONAL config, and runs
the identity guard over each — with the developer's private inputs when present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import load_config
from obi.core.prompt import build_radio_addendum, build_system_prompt
from obi.core.toolset import (
    build_chat_tools,
    build_operator_tools,
    build_radio_tools,
    build_watch_tools,
)
from obi.mcp.server import build_instructions
from tests.identity_render import fatal_in

FULL = """
[logs]
db_path = "{tmp}/logs.db"

[weather]
latitude = 45.0
longitude = -100.0
place_name = "Northfield"

[traffic]
base_url = "https://traffic.example.net"

[brc]
origin_lat = 45.0
origin_lon = -100.0
year = 2030

[victoria]
base_url = "https://metrics.example.net"
display_name = "the regional ops store"

[meshtastic]
names = {{ "!0fa4e001" = "Tumbleweed" }}

[wled.devices]
gate = {{ url = "http://192.0.2.5", description = "the arch over the gate" }}

[radio]
ftdi_serial = "FX00TEST"
channel_name = "the valley ops channel"
pronunciations = {{ GW = "gee double-you" }}
speech_aliases = {{ "gee dub" = "GW" }}
"""


def _config(tmp_path: Path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body.format(tmp=tmp_path))
    path.chmod(0o600)
    return load_config(path)


def _surfaces(config) -> dict[str, str]:
    tools = {
        "chat": build_chat_tools(config),
        "mcp": build_operator_tools(config),
        "watch": build_watch_tools(config),
        "radio": build_radio_tools("operator", config),
    }
    out = {
        "prompt.chat": build_system_prompt(config),
        "prompt.radio": build_radio_addendum(config),
        "prompt.mcp": build_instructions(config),
    }
    for surface, items in tools.items():
        for tool in items:
            out[f"tools.{surface}.{tool.name}"] = tool.description
    return out


@pytest.mark.parametrize("mode", ["none", "empty", "full"])
def test_rendered_text_has_no_identity(tmp_path: Path, mode: str) -> None:
    config = (
        None
        if mode == "none"
        else _config(tmp_path, '[logs]\ndb_path = "{tmp}/logs.db"\n' if mode == "empty" else FULL)
    )
    leaks = {
        label: hits for label, text in _surfaces(config).items() if (hits := fatal_in(text, label))
    }
    assert leaks == {}
    for label, text in _surfaces(config).items():
        for token in ("{store}", "{lighting}", "{weather_place}", "{mesh_example}"):
            assert token not in text, (label, token)


def test_event_paragraphs_follow_config(tmp_path: Path) -> None:
    empty = _config(tmp_path, '[logs]\ndb_path = "{tmp}/logs.db"\n')
    full = _config(tmp_path, FULL)
    prompt_empty, prompt_full = build_system_prompt(empty), build_system_prompt(full)
    for marker in ("WEATHER is a LOCAL store", "gate_traffic", "backbone_rf_health", "wled_set"):
        assert marker not in prompt_empty, marker
        assert marker in prompt_full, marker
    assert "Northfield" in prompt_full and "the regional ops store" in prompt_full
    assert "the arch over the gate" in prompt_full


def test_radio_persona_from_config(tmp_path: Path) -> None:
    full = _config(tmp_path, FULL)
    persona = build_radio_addendum(full)
    assert "the valley ops channel" in persona
    assert 'GW is pronounced "gee double-you" and may be transcribed as "gee dub"' in persona
    assert "A LOCATION is one fact" in persona  # [brc] configured
    empty = build_radio_addendum(_config(tmp_path, '[logs]\ndb_path = "{tmp}/logs.db"\n'))
    assert "the operations radio channel" in empty and "A LOCATION is one fact" not in empty


def test_tool_descriptions_render_store_and_lighting(tmp_path: Path) -> None:
    full = _config(tmp_path, FULL)
    by_name = {t.name: t.description for t in build_operator_tools(full)}
    assert "the regional ops store" in by_name["victoria_query"]
    assert "the arch over the gate" in by_name["wled_set"]
    assert "'Tumbleweed'" in by_name["mesh_locate"]
