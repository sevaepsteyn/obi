"""T005: dispatch audits each tool call (reason = the question) and never raises to the loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.inventory import InventoryEntry
from obi.core.toolset import ToolContext, ToolDefinition, build_dispatch, build_tools
from tests.fakes import FakeDriver, chat_config, save_inventory


def test_dispatch_audits_with_question_as_reason(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(config.inventory_path, [InventoryEntry("172.25.110.1", "gw")])
    ctx = ToolContext(
        config=config,
        driver=FakeDriver({}),
        audit=AuditTrail(config.audit_path),
        question="what is up?",
    )

    out = build_dispatch(ctx, build_tools())["inventory_list"]({})

    assert "gw" in out
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert any('"inventory_list"' in line and "what is up?" in line for line in lines)


def test_dispatch_never_raises_to_loop(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    ctx = ToolContext(
        config=config, driver=FakeDriver({}), audit=AuditTrail(config.audit_path), question="q"
    )

    def boom(_ctx: ToolContext, _args: dict[str, Any]) -> str:
        raise RuntimeError("kaboom")

    tool = ToolDefinition(name="boom", description="d", input_schema={}, handler=boom)
    out = build_dispatch(ctx, [tool])["boom"]({})

    assert "error running boom" in out and "kaboom" in out
    # The failure is still audited.
    assert any("error" in line for line in (tmp_path / "audit.jsonl").read_text().splitlines())
