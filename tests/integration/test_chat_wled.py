"""Integration (feature 103): a chat turn drives the portal through the full stack.

Scripted LLM → wled_set/wled_revert handlers → lighting engine → real WledDriver over
httpx.MockTransport (never the real device). Asserts the POST bodies on the wire, the audited
snapshot, and the unconfigured/unreachable degradations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from obi.chat.session import ask
from obi.core import lighting as lighting_mod
from obi.core.config import WledSettings
from obi.core.drivers.wled import WledDriver
from tests.fakes import chat_config, chat_runtime, text_response, tool_response

FIXTURES = Path(__file__).parent.parent / "fixtures" / "wled"


class PortalTransport:
    """A WLED device on a mock wire: fixture reads, merge-on-POST, request log."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = json.loads((FIXTURES / "state.json").read_text())
        self.info: dict[str, Any] = json.loads((FIXTURES / "info.json").read_text())
        self.effects: list[str] = json.loads((FIXTURES / "effects.json").read_text())
        self.palettes: list[str] = json.loads((FIXTURES / "palettes.json").read_text())
        self.posts: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path == "/json/state":
                body = json.loads(request.content)
                self.posts.append(body)
                merged = {**self.state, **{k: v for k, v in body.items() if k not in ("v", "seg")}}
                if "seg" in body:
                    by_id = {s["id"]: s for s in merged.get("seg", [])}
                    for change in body["seg"]:
                        if change.get("id") in by_id:
                            by_id[change["id"]] = {**by_id[change["id"]], **change}
                    merged["seg"] = list(by_id.values())
                self.state = merged
                return httpx.Response(200, json=self.state)
            if path == "/json/info":
                return httpx.Response(200, json=self.info)
            if path == "/json/state":
                return httpx.Response(200, json=self.state)
            if path == "/json/eff":
                return httpx.Response(200, json=self.effects)
            if path == "/json/pal":
                return httpx.Response(200, json=self.palettes)
            raise AssertionError(f"unexpected request: {request.method} {path}")

        return httpx.MockTransport(handle)


@pytest.fixture
def portal(monkeypatch: pytest.MonkeyPatch) -> PortalTransport:
    device = PortalTransport()
    monkeypatch.setattr(
        lighting_mod,
        "WledDriver",
        lambda settings: WledDriver(settings, transport=device.transport()),
    )
    return device


def _wled_config(tmp_path: Path) -> Any:
    return chat_config(tmp_path, wled=WledSettings(devices={"portal": "https://portal.test"}))


def test_chat_turn_sets_the_portal_and_audits_the_snapshot(
    tmp_path: Path, portal: PortalTransport
) -> None:
    config = _wled_config(tmp_path)
    original_state = dict(portal.state)
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("wled_set", {"color": "purple"}),
            text_response("The portal is purple now."),
        ],
    )
    result = ask(runtime, [], "make the portal purple")
    assert result.answer == "The portal is purple now."
    # One state write hit the wire, targeting every segment, echo requested.
    assert len(portal.posts) == 1
    sent = portal.posts[0]
    assert sent["v"] is True
    assert {seg["id"] for seg in sent["seg"]} == {s["id"] for s in original_state["seg"]}
    assert all(seg["col"] == [[160, 32, 240]] for seg in sent["seg"])
    # The audit trail answers "what changed, and what was overwritten" (SC-004).
    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    wled_records = [r for r in records if r["action"] == "wled_set"]
    assert wled_records and wled_records[-1]["outcome"] == "ok"
    assert wled_records[-1]["params"]["snapshot"] == original_state
    assert wled_records[-1]["reason"] == "make the portal purple"


def test_chat_turn_sets_the_palette_by_spoken_name(tmp_path: Path, portal: PortalTransport) -> None:
    """The radio-call-24 request class end to end: 'ice fire' resolves to Icefire and lands."""

    config = _wled_config(tmp_path)
    icefire = portal.palettes.index("Icefire")
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("wled_set", {"palette": "ice fire"}),
            text_response("Portal palette is Icefire now."),
        ],
    )
    result = ask(runtime, [], "set the portal color palette to ice fire")
    assert result.answer == "Portal palette is Icefire now."
    assert len(portal.posts) == 1
    assert all(seg["pal"] == icefire for seg in portal.posts[0]["seg"])
    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    wled_records = [r for r in records if r["action"] == "wled_set"]
    assert wled_records and wled_records[-1]["outcome"] == "ok"
    assert wled_records[-1]["params"]["requested"] == {"palette": "ice fire"}


def test_chat_revert_restores_palette_speed_and_colors_verbatim(
    tmp_path: Path, portal: PortalTransport
) -> None:
    """Full-look revert (107 US4): the new dimensions ride the verbatim snapshot."""

    config = _wled_config(tmp_path)
    original_state = dict(portal.state)
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("wled_set", {"palette": "icefire", "speed_pct": 10, "color2": "blue"}),
            text_response("Done."),
            tool_response("wled_revert", {}, use_id="tool_2"),
            text_response("Put it back."),
        ],
    )
    messages: list[dict[str, Any]] = []
    ask(runtime, messages, "icefire palette, slow, blue background")
    changed = portal.state["seg"][0]
    assert changed["pal"] == portal.palettes.index("Icefire") and changed["sx"] == 26
    assert ask(runtime, messages, "put it back").answer == "Put it back."
    assert portal.posts[-1] == {**original_state, "v": True}


def test_chat_revert_turn_restores_the_overwritten_look(
    tmp_path: Path, portal: PortalTransport
) -> None:
    config = _wled_config(tmp_path)
    original_state = dict(portal.state)
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("wled_set", {"color": "red", "brightness_pct": 100}),
            text_response("Red at full."),
            tool_response("wled_revert", {}, use_id="tool_2"),
            text_response("Put it back."),
        ],
    )
    messages: list[dict[str, Any]] = []
    ask(runtime, messages, "portal red, full brightness")
    assert ask(runtime, messages, "put it back").answer == "Put it back."
    # The revert POSTs the pre-change state VERBATIM (plus the echo flag).
    assert portal.posts[-1] == {**original_state, "v": True}


def test_chat_without_wled_config_degrades_cleanly(tmp_path: Path) -> None:
    runtime = chat_runtime(
        chat_config(tmp_path),
        responses=[tool_response("wled_set", {"color": "red"}), text_response("no device")],
    )
    ask(runtime, [], "make the portal red")
    # The handler's tool result told the model no device is configured.
    tool_results = [
        block
        for call in runtime.client.calls
        for message in call["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_results and "No lighting device is configured" in str(tool_results[-1]["content"])


def test_chat_unreachable_portal_is_honest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        lighting_mod,
        "WledDriver",
        lambda settings: WledDriver(settings, transport=httpx.MockTransport(refuse)),
    )
    runtime = chat_runtime(
        _wled_config(tmp_path),
        responses=[tool_response("wled_set", {"color": "red"}), text_response("it is down")],
    )
    ask(runtime, [], "make the portal red")
    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    wled_records = [r for r in records if r["action"] == "wled_set"]
    assert wled_records and wled_records[-1]["outcome"] == "unreachable"
