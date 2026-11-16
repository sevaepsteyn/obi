"""Unit (feature 042, T012): AuditedLLMClient records full request/response/usage, or errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from obi.core import auditstore
from obi.core.audit import AuditRecorder, audit_scope
from obi.core.config import AuditSettings
from obi.core.llm import AuditedLLMClient, ChatUnavailable, LLMResponse, TextBlock, Usage
from tests.fakes import StubLLMClient


def _events(db: Path) -> list[dict[str, Any]]:
    conn = auditstore.connect_readonly(db)
    try:
        rows, _ = auditstore.query_events(conn, limit=10, include_payload=True)
        return rows
    finally:
        conn.close()


def _call(client: AuditedLLMClient) -> LLMResponse:
    return client.create_message(
        system="you are obi",
        tools=[{"name": "inventory_summary", "input_schema": {}}],
        messages=[{"role": "user", "content": "hi"}],
        model="claude-test-1",
        max_tokens=64,
    )


def test_records_request_response_usage(tmp_path: Path) -> None:
    inner = StubLLMClient(
        [
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="hello")],
                usage=Usage(input_tokens=42, output_tokens=7),
            )
        ]
    )
    recorder = AuditRecorder(AuditSettings(db_path=tmp_path / "audit.db"))
    with audit_scope(recorder, "chat", "chat-x"):
        response = _call(AuditedLLMClient(inner))
    recorder.close()

    assert response.text() == "hello"  # pass-through: the inner response is returned intact
    (event,) = _events(tmp_path / "audit.db")
    assert event["kind"] == "llm_call"
    payload = event["payload"]
    assert payload["model"] == "claude-test-1"
    assert payload["system"] == "you are obi"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tool_names"] == ["inventory_summary"]
    assert payload["response"] == [{"type": "text", "text": "hello"}]
    assert payload["input_tokens"] == 42
    assert payload["output_tokens"] == 7
    assert payload["stop_reason"] == "end_turn"
    assert event["outcome"] == "ok"
    assert "42→7" in event["summary"]


def test_records_cache_token_classes(tmp_path: Path) -> None:
    """089: the audited payload carries both prompt-cache classes (default 0 stays 0)."""

    inner = StubLLMClient(
        [
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="hello")],
                usage=Usage(
                    input_tokens=42,
                    output_tokens=7,
                    cache_creation_input_tokens=1200,
                    cache_read_input_tokens=9800,
                ),
            )
        ]
    )
    recorder = AuditRecorder(AuditSettings(db_path=tmp_path / "audit.db"))
    with audit_scope(recorder, "chat", "chat-x"):
        _call(AuditedLLMClient(inner))
    recorder.close()

    (event,) = _events(tmp_path / "audit.db")
    payload = event["payload"]
    assert payload["cache_creation_input_tokens"] == 1200
    assert payload["cache_read_input_tokens"] == 9800


def test_to_response_maps_cache_usage() -> None:
    """089: `_to_response` reads the SDK usage's cache fields defensively (missing ⇒ 0)."""

    from obi.core.llm import _to_response

    class SdkUsage:
        input_tokens = 5
        output_tokens = 3
        cache_creation_input_tokens = 11
        cache_read_input_tokens = 22

    class SdkMessage:
        content: list[Any] = []
        stop_reason = "end_turn"
        usage = SdkUsage()

    usage = _to_response(SdkMessage()).usage
    assert (usage.input_tokens, usage.output_tokens) == (5, 3)
    assert (usage.cache_creation_input_tokens, usage.cache_read_input_tokens) == (11, 22)

    class BareUsage:  # an SDK/back-end that reports no cache fields at all
        input_tokens = 5
        output_tokens = 3

    class BareMessage:
        content: list[Any] = []
        stop_reason = "end_turn"
        usage = BareUsage()

    bare = _to_response(BareMessage()).usage
    assert (bare.cache_creation_input_tokens, bare.cache_read_input_tokens) == (0, 0)


def test_records_error_then_reraises(tmp_path: Path) -> None:
    class Down:
        def create_message(self, **kwargs: Any) -> LLMResponse:
            raise ChatUnavailable("offline")

    recorder = AuditRecorder(AuditSettings(db_path=tmp_path / "audit.db"))
    with audit_scope(recorder, "voice", None), pytest.raises(ChatUnavailable, match="offline"):
        _call(AuditedLLMClient(Down()))
    recorder.close()

    (event,) = _events(tmp_path / "audit.db")
    assert event["outcome"] == "error"
    assert event["error"] == "offline"
    assert event["payload"]["messages"]  # the request that failed is still reconstructable


def test_no_scope_is_pure_passthrough(tmp_path: Path) -> None:
    inner = StubLLMClient([LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])])
    response = _call(AuditedLLMClient(inner))
    assert response.text() == "ok"
    assert not (tmp_path / "audit.db").exists()
