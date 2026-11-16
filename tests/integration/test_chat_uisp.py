"""Integration (US1): a UISP question drives a uisp_status tool call → grounded answer, and the
UISP read is audited once with the operator's question as intent (feature 006)."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.models import Device, DeviceStatus, Reachability
from tests.fakes import (
    FakeSource,
    chat_config,
    chat_runtime,
    text_response,
    tool_response,
    uisp_settings,
)


def test_us1_uisp_question_grounded_and_audited(tmp_path: Path) -> None:
    config = chat_config(tmp_path, uisp=uisp_settings())
    up = DeviceStatus(
        Device("ridge-ap", "10.9.0.5"), Reachability.REACHABLE,
        uptime_seconds=3600, cpu_load_percent=4.0, free_memory_bytes=100_000_000,
    )
    down = DeviceStatus(
        Device("valley-ap", "10.9.0.6"), Reachability.UNREACHABLE, notes=["offline"]
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("uisp_status", {}),
            text_response("One UISP device is down: valley-ap."),
        ],
        uisp_source=FakeSource([up, down]),
    )

    result = ask(runtime, [], "what UISP devices are down?")

    assert result.tools_used == ["uisp_status"]
    assert "valley-ap" in result.answer

    # The UISP read was audited exactly once, carrying the question as intent (Principle V),
    # and the controller token never appears in the trail (Principle VI).
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    uisp_records = [ln for ln in lines if '"uisp_status"' in ln]
    assert len(uisp_records) == 1
    assert "what UISP devices are down?" in uisp_records[0]
    assert "UISP-TOKEN" not in (tmp_path / "audit.jsonl").read_text()
