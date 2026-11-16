"""Unit (feature 111): the mesh_locate tool — registration, gating, budget, audit event.

The engine itself is pinned by test_mesh_locator.py; here the handler seam is faked so
these tests never construct a real driver (which would scan this machine's real USB
ports). What matters at this layer: the ToolDefinition's read-only registration shape,
the not-configured gate, the live-budget contract, and the free 042 tool_call event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from obi.core import auditstore, toolset
from obi.core.audit import AuditRecorder, AuditTrail, audit_scope
from obi.core.config import AuditSettings, MeshtasticSettings
from obi.core.toolset import (
    LiveCallBudget,
    MeshSource,
    ToolContext,
    _mesh_locate,
    build_dispatch,
    build_tools,
)
from tests.fakes import chat_config


class _FakeLocator:
    calls: list[tuple[str, str, str | None]] = []

    def __init__(self, settings: MeshtasticSettings, audit: AuditTrail, **kwargs: Any) -> None:
        self.settings = settings

    def locate(self, node_ref: str, *, reason: str, actor: str | None) -> str:
        _FakeLocator.calls.append((node_ref, reason, actor))
        return f"{node_ref} is at 6:15 and 2,670 feet from the Man — position 2 minutes old."


@pytest.fixture(autouse=True)
def _fake_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLocator.calls = []
    monkeypatch.setattr(toolset, "MeshLocator", _FakeLocator)


def _ctx(tmp_path: Path, *, configured: bool = True, budget: int | None = None) -> ToolContext:
    settings = MeshtasticSettings()
    config = chat_config(tmp_path, mesh=settings if configured else None)
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        mesh_source=MeshSource(settings) if configured else None,
        question="where is tooma?",
        actor="U123",
        live_budget=LiveCallBudget(budget) if budget is not None else None,
    )


def test_registered_read_only_with_the_contract_schema() -> None:
    tools = {t.name: t for t in build_tools()}
    tool = tools["mesh_locate"]
    assert tool.mutating is False
    assert tool.risk_class == "read_only"
    assert tool.input_schema["required"] == ["node"]
    assert tool.input_schema["additionalProperties"] is False
    assert set(tool.input_schema["properties"]) == {"node"}


def test_handler_delegates_with_question_and_actor(tmp_path: Path) -> None:
    answer = _mesh_locate(_ctx(tmp_path), {"node": "tooma"})
    assert answer.startswith("tooma is at 6:15")
    assert _FakeLocator.calls == [("tooma", "where is tooma?", "U123")]


def test_missing_node_is_an_error(tmp_path: Path) -> None:
    assert _mesh_locate(_ctx(tmp_path), {}) == "error: 'node' is required."
    assert _FakeLocator.calls == []


def test_unconfigured_host_answers_cleanly(tmp_path: Path) -> None:
    answer = _mesh_locate(_ctx(tmp_path, configured=False), {"node": "tooma"})
    assert "[meshtastic]" in answer
    assert _FakeLocator.calls == []


def test_exhausted_live_budget_skips_before_any_device_contact(tmp_path: Path) -> None:
    answer = _mesh_locate(_ctx(tmp_path, budget=0), {"node": "tooma"})
    assert "live-call budget" in answer
    assert _FakeLocator.calls == []


def test_budget_is_consumed_once_per_call(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, budget=1)
    assert _mesh_locate(ctx, {"node": "tooma"}).startswith("tooma is at")
    assert "live-call budget" in _mesh_locate(ctx, {"node": "tooma"})


def test_dispatched_call_lands_one_tool_event(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    tools = [t for t in build_tools() if t.name == "mesh_locate"]
    dispatch = build_dispatch(ctx, tools)
    recorder = AuditRecorder(
        AuditSettings(
            enabled=True, db_path=tmp_path / "audit.db", retention_days=90,
            max_payload_bytes=5000,
        )
    )
    with audit_scope(recorder, "chat", "chat-test"):
        dispatch["mesh_locate"]({"node": "tooma"})
    recorder.close()
    conn = auditstore.connect_readonly(tmp_path / "audit.db")
    try:
        rows, _ = auditstore.query_events(conn, limit=100, include_payload=True)
    finally:
        conn.close()
    tool_events = [r for r in rows if r["kind"] == "tool_call"]
    assert len(tool_events) == 1
    assert "mesh_locate" in str(tool_events[0])
