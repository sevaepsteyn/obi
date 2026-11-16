"""Integration: a Siklu question drives a siklu_status tool call → grounded, single-audited answer.

The Siklu read is audited exactly once with the operator's question as intent, and the radio
credential never appears in the answer or the audit trail (feature 020, FR-022).
"""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ChatRuntime, ask
from tests.fakes import (
    chat_config,
    chat_runtime,
    siklu_driver_from_fixture,
    siklu_settings,
    text_response,
    tool_response,
)


def _runtime(tmp_path: Path, *, links: bool = False) -> ChatRuntime:
    driver, _ = siklu_driver_from_fixture("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70")
    settings = siklu_settings(hosts=["172.18.89.130", "172.18.83.224"])
    config = chat_config(tmp_path, routeros=False, siklu=settings)
    args = {"links": True} if links else {}
    return chat_runtime(
        config,
        responses=[
            tool_response("siklu_status", args),
            text_response("Both Siklu ends are up: tamarack↔basswood-70."),
        ],
        with_driver=False,
        siklu_driver=driver,
    )


def test_query_siklu_grounded_and_audited_once(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    result = ask(runtime, [], "what Siklu links are down?")

    assert result.tools_used == ["siklu_status"]
    assert "tamarack" in result.answer.lower()

    audit_text = (tmp_path / "audit.jsonl").read_text()
    siklu_records = [ln for ln in audit_text.splitlines() if '"siklu_status"' in ln]
    assert len(siklu_records) == 1
    assert "what Siklu links are down?" in siklu_records[0]
    # No credential substring anywhere in the answer or the audit trail.
    assert "fakesecret44028f" not in audit_text
    assert "fakesecret44028f" not in result.answer


def test_query_siklu_links_no_false_asymmetry(tmp_path: Path) -> None:
    from obi.core.toolset import _siklu_status

    runtime = _runtime(tmp_path, links=True)
    runtime.ctx.question = "show me the siklu links"
    # Call the handler exactly as the dispatch does, and assert on the rendered link view that is
    # fed back to the model.
    rendered = _siklu_status(runtime.ctx, {"links": True})

    assert "basswood↔tamarack-70" in rendered
    assert " up " in f" {rendered} "
    # qam128/qam32 difference must NOT surface as a bracketed asymmetry flag.
    assert "[" not in rendered


def test_query_siklu_unconfigured_message(tmp_path: Path) -> None:
    # A chat with another source but no [siklu] returns a plain "not configured" message, no raise.
    from tests.fakes import FakeSource, uisp_settings

    config = chat_config(tmp_path, routeros=False, uisp=uisp_settings())
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("siklu_status", {}),
            text_response("No Siklu radios are configured."),
        ],
        with_driver=False,
        uisp_source=FakeSource([]),
    )
    result = ask(runtime, [], "are any siklu links down?")
    assert result.tools_used == ["siklu_status"]
    # No siklu_status audit churn when unconfigured.
    audit_path = tmp_path / "audit.jsonl"
    audit_text = audit_path.read_text() if audit_path.exists() else ""
    assert '"siklu_status"' not in audit_text
