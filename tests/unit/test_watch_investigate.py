"""US1 — bounded, read-only investigation loop (feature 014, T019)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, seed_logs, tool_response, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _fault_rows() -> list[tuple[str, datetime, str, str]]:
    return [("core-1", NOW - timedelta(minutes=2), "interface,info", "link ether1 down")]


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def test_investigation_then_report(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, investigation_max_steps=2)
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, _fault_rows())
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    # The model investigates once (logs_query), then reports.
    responses = [
        tool_response("logs_query", {"host": "core-1"}, use_id="t1"),
        tool_response(
            "report_findings",
            {
                "findings": [
                    {
                        "device": "core-1",
                        "severity": "info",
                        "confidence": "low",
                        "summary": "s",
                        "description": "d",
                        "evidence": ["link ether1 down"],
                    }
                ]
            },
            use_id="t2",
        ),
    ]
    run = run_cycle(
        cfg, store, audit=_audit(cfg), now=NOW, llm_client=StubLLMClient(responses)
    )
    assert run.llm_calls == 2  # one investigate + one report
    assert run.outcome == "findings"


def test_investigation_step_cap_enforced(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, investigation_max_steps=1)
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, _fault_rows())
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    # The model keeps investigating and never reports → the loop must stop at the cap.
    stub = StubLLMClient(
        [tool_response("logs_query", {"host": "core-1"}, use_id=f"t{i}") for i in range(5)]
    )
    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=stub)

    assert run.llm_calls == 2  # investigation_max_steps + 1
    assert run.outcome == "no_op"  # nothing was reported
    assert store.list_findings() == []
