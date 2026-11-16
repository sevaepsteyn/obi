"""Integration (feature 013): a syslog question drives a logs_query tool call → grounded answer,
audited once with the operator's question as intent; runs on a logs-only config."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.chat.session import ask
from obi.core.models import LogEvent
from tests.fakes import (
    FakeLogSource,
    chat_config,
    chat_runtime,
    logs_settings,
    text_response,
    tool_response,
)


def test_syslog_question_grounded_and_audited(tmp_path: Path) -> None:
    # Logs-only config (no [routeros]/[uisp]/[netbox]) → a logs-only host answers from syslog.
    config = chat_config(tmp_path, routeros=False, logs=logs_settings())
    events = [
        LogEvent(
            ts=datetime(2026, 6, 3, 11, 0, 2, tzinfo=UTC),
            host="chestnut-gw",
            topics="system,error",
            message="login failure for admin",
            subsystem="system",
            severity="error",
        )
    ]
    runtime = chat_runtime(
        config,
        responses=[
            tool_response(
                "logs_query", {"host": "chestnut-gw", "severity": "error", "since": "1d"}
            ),
            text_response(
                "chestnut-gw logged a system error: login failure for admin at 11:00:02."
            ),
        ],
        with_driver=False,
        log_source=FakeLogSource(events),
    )

    result = ask(runtime, [], "what errors did chestnut-gw log today?")

    assert result.tools_used == ["logs_query"]
    assert "login failure" in result.answer

    # The log read is audited exactly once, carrying the question as intent (Principle V).
    trail = (tmp_path / "audit.jsonl").read_text()
    records = [ln for ln in trail.splitlines() if '"logs_query"' in ln]
    assert len(records) == 1
    assert "what errors did chestnut-gw log today?" in records[0]
