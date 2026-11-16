"""US1 — LLM judgement as an opportunistic enhancement (feature 014, T017/T018)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.llm import LLMResponse, ToolUseBlock, Usage
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, seed_logs, tool_response, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _fault_rows() -> list[tuple[str, datetime, str, str]]:
    return [
        ("core-1", NOW - timedelta(minutes=2), "interface,info", "link ether1 down"),
        ("core-1", NOW - timedelta(minutes=1), "interface,info", "link ether1 down"),
    ]


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def test_llm_finding_written_with_required_fields(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, _fault_rows())
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    response = tool_response(
        "report_findings",
        {
            "findings": [
                {
                    "device": "core-1",
                    "severity": "warning",
                    "confidence": "high",
                    "summary": "ether1 flapping",
                    "description": "ether1 went down repeatedly in the window",
                    "evidence": ["link ether1 down"],
                }
            ]
        },
    )
    run = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=StubLLMClient([response])
    )

    assert run.outcome == "findings"
    findings = store.list_findings()
    assert len(findings) == 1
    finding = findings[0]
    assert finding.source == "llm" and finding.device == "core-1"
    assert finding.severity == "warning" and finding.confidence == "high"
    assert finding.summary and finding.description and finding.evidence
    assert (store.reports_dir / f"{finding.id}.json").exists()
    assert (store.reports_dir / f"{finding.id}.md").exists()


def test_empty_window_skips_the_model(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [("core-1", NOW - timedelta(minutes=2), "system,info", "ok")],
    )
    store = WatchStore(cfg.watch.watch_dir)  # first run ⇒ no new events
    stub = StubLLMClient([tool_response("report_findings", {"findings": []})])

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=stub)
    assert run.new_event_count == 0
    assert run.llm_calls == 0 and run.tokens_input == 0 and run.tokens_output == 0
    assert stub.calls == []  # the model was never invoked (SC-003)


def test_tokens_are_tallied(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, _fault_rows())
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    response = LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(id="t1", name="report_findings", input={"findings": []})],
        usage=Usage(input_tokens=12, output_tokens=7),
    )
    run = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=StubLLMClient([response])
    )
    assert run.llm_calls == 1 and run.tokens_input == 12 and run.tokens_output == 7


def test_llm_unavailable_degrades_with_no_findings(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, _fault_rows())
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=None)
    assert "llm_unavailable" in run.conditions
    assert run.outcome == "degraded"
    assert run.finding_ids == []  # no offline detection — the watcher needs the cloud
    assert run.cursor_after is not None and run.cursor_after.id > 0  # advanced past analyzed
