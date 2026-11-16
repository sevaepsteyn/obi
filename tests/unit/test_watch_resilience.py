"""Polish — graceful degradation and watermark safety (feature 014, T027, FR-012)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import WatchCursor, run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import prime_cursor, watch_config

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def test_missing_store_degrades_cleanly(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # the SQLite store is never created
    store = WatchStore(cfg.watch.watch_dir)

    run = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, llm_client=None)
    assert run.outcome == "degraded"
    assert "store_unavailable" in run.conditions
    assert run.cursor_before == run.cursor_after  # not advanced (both None)


def test_store_error_does_not_advance_cursor(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # no store on disk
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store, cursor_id=5)

    run = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, llm_client=None)
    assert run.outcome == "degraded"
    assert run.cursor_after == WatchCursor(id=5)  # left exactly where it was
    assert store.load_cursor() == WatchCursor(id=5)
