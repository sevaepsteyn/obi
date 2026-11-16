"""Integration (feature 042, T009/T019): one chat turn produces its complete causal trace.

Drives the real ask() seam with the stub LLM and fake sources inside an audit scope, then
asserts the audit event store reconstructs the whole interaction — the operator's message,
the full model request/response with token usage, the tool call with arguments and result,
and obi's reply — all under one session, in causal order (FR-001/2/4/5/6). Also proves the
disabled mode: no recorder → zero events, identical behavior (FR-012, SC-006).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from obi.chat.session import ask
from obi.core import auditstore
from obi.core.audit import AuditRecorder, audit_scope
from obi.core.config import AuditSettings
from obi.core.inventory import InventoryEntry
from obi.core.llm import AuditedLLMClient, LLMResponse, TextBlock, ToolUseBlock, Usage
from obi.core.models import DiscoveryState
from tests.fakes import chat_config, chat_runtime, save_inventory

SESSION = "chat-testsession"


def _tool_use_response(name: str, tool_input: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            TextBlock(text="let me check"),
            ToolUseBlock(id="tool_1", name=name, input=tool_input),
        ],
        usage=Usage(input_tokens=120, output_tokens=15),
    )


def _final_response(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(input_tokens=200, output_tokens=30),
    )


def _trace(db: Path, session: str = SESSION) -> list[dict[str, Any]]:
    conn = auditstore.connect_readonly(db)
    try:
        return auditstore.session_trace(conn, session)
    finally:
        conn.close()


def _run_turn(tmp_path: Path, *, recorder: AuditRecorder | None) -> Any:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config,
        responses=[
            _tool_use_response("inventory_summary", {}),
            _final_response("1 device, all reachable."),
        ],
    )
    # Mirror build_runtime's production wiring: the audited decorator wraps the client.
    runtime = replace(runtime, client=AuditedLLMClient(runtime.client))
    with audit_scope(recorder, "chat", SESSION):
        return ask(runtime, [], "how is the network?")


def test_chat_turn_reconstructs_causally(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    recorder = AuditRecorder(AuditSettings(db_path=db))
    result = _run_turn(tmp_path, recorder=recorder)
    recorder.close()
    assert result.answer == "1 device, all reachable."

    trace = _trace(db)
    kinds = [e["kind"] for e in trace]
    # Causal order: question in → model asks for a tool → the tool runs (its engine intent
    # record tees in as `action` beforehand) → model answers → reply out.
    assert kinds[0] == "human_message"
    assert kinds[-1] == "human_message"
    assert kinds.count("llm_call") == 2
    assert kinds.count("tool_call") == 1
    assert kinds.index("tool_call") > kinds.index("llm_call")

    # Every event belongs to the one session and the one source (FR-005).
    assert all(e["session_id"] == SESSION for e in trace)
    assert all(e["source"] == "chat" for e in trace)

    # Human turn payloads carry direction + full content (FR-004).
    turn_in, turn_out = trace[0], trace[-1]
    assert turn_in["payload"]["direction"] == "in"
    assert turn_in["payload"]["content"] == "how is the network?"
    assert turn_out["payload"]["direction"] == "out"
    assert turn_out["payload"]["content"] == "1 device, all reachable."

    # The llm_call payload reproduces the full request and response + usage (FR-001, SC-003).
    first_llm = next(e for e in trace if e["kind"] == "llm_call")
    payload = first_llm["payload"]
    assert payload["messages"][0]["content"] == "how is the network?"
    assert payload["response"][1]["name"] == "inventory_summary"
    assert payload["input_tokens"] == 120
    assert payload["output_tokens"] == 15
    assert payload["system"]  # the full system prompt is stored
    assert first_llm["duration_ms"] is not None

    # The tool_call payload carries name, arguments, and the full result (FR-002).
    tool_event = next(e for e in trace if e["kind"] == "tool_call")
    assert tool_event["payload"]["tool"] == "inventory_summary"
    assert tool_event["payload"]["arguments"] == {}
    assert tool_event["payload"]["result"]
    assert tool_event["payload"]["is_error"] is False


def test_disabled_mode_records_nothing_and_still_works(tmp_path: Path) -> None:
    result = _run_turn(tmp_path, recorder=None)
    assert result.answer == "1 device, all reachable."
    assert not (tmp_path / "audit.db").exists()
