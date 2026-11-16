"""US1 — cursor-advance semantics under filter-before-LIMIT (feature 062, A1, research D3).

The July-2026 incident: ignorable noise consumed the raw window budget, so the cursor advanced
only ``budget`` ids/run and fell ever further behind while real events sat unjudged. These tests
pin the fix: the budget counts CANDIDATES (noise is swept for free), and the cursor never stalls
while unscanned range remains, yet never skips a candidate.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import StubLLMClient, prime_cursor, seed_logs, tool_response, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _empty_llm() -> StubLLMClient:
    return StubLLMClient([tool_response("report_findings", {"findings": []})])


def _small_window(cfg, size: int):  # type: ignore[no-untyped-def]
    return dataclasses.replace(cfg, watch=dataclasses.replace(cfg.watch, max_window_events=size))


def test_noise_only_backlog_drains_to_newest_in_one_run(tmp_path: Path) -> None:
    # 50 ignorable rows (system,info,account) + 1 real row, budget 2. Under the OLD raw-window
    # design the cursor would crawl 2 ids/run; under A1 the candidate is judged and the cursor
    # jumps to the newest scanned id in a single run — noise never holds it back (FR-004).
    cfg = _small_window(watch_config(tmp_path, ignore_topics=["system,info,account"]), 2)
    rows = [
        ("core-1", NOW - timedelta(minutes=60 - i), "system,info,account", f"login {i}")
        for i in range(50)
    ]
    rows.append(("core-1", NOW - timedelta(minutes=1), "system,error,critical", "no storage"))
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())

    assert run.new_event_count == 1  # only the real event was a candidate
    assert run.remaining_candidates == 0  # fully caught up despite 50 noise rows and budget 2
    # cursor landed on the NEWEST ingested id (all 51 rows), not id=2.
    assert run.cursor_after is not None and run.cursor_after.id == 51


def test_candidate_saturated_window_advances_to_last_candidate(tmp_path: Path) -> None:
    # 5 real candidates, budget 2: the run judges the oldest 2 and stops the cursor on the 2nd;
    # the remaining 3 are next run's work, none skipped (FR-002).
    cfg = _small_window(watch_config(tmp_path), 2)
    rows = [
        ("core-1", NOW - timedelta(minutes=5 - i), "system,error", f"boom {i}") for i in range(5)
    ]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert run.cursor_after is not None and run.cursor_after.id == 2  # last judged candidate
    assert run.remaining_candidates == 3

    run2 = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert run2.cursor_after is not None and run2.cursor_after.id == 4
    run3 = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert run3.cursor_after is not None and run3.cursor_after.id == 5
    assert run3.remaining_candidates == 0  # all five judged across three runs, none skipped


def test_pure_noise_window_is_no_op_but_advances(tmp_path: Path) -> None:
    cfg = _small_window(watch_config(tmp_path, ignore_topics=["system,info,account"]), 2)
    rows = [
        ("core-1", NOW - timedelta(minutes=10 - i), "system,info,account", f"login {i}")
        for i in range(6)
    ]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, rows)
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)

    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=_empty_llm())
    assert run.new_event_count == 0 and run.outcome == "no_op"
    assert run.cursor_after is not None and run.cursor_after.id == 6  # swept past all noise


