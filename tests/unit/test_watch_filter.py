"""Window filters before the model: routeros_only scope + ignore_topics (feature 014)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import (
    StubLLMClient,
    prime_cursor,
    seed_logs,
    tool_response,
    watch_config,
    watch_inventory,
)

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _audit(cfg):  # type: ignore[no-untyped-def]
    return AuditTrail(cfg.audit_path)


def _run(cfg, store):  # type: ignore[no-untyped-def]
    stub = StubLLMClient([tool_response("report_findings", {"findings": []})])
    run = run_cycle(cfg, store, audit=_audit(cfg), now=NOW, llm_client=stub)
    window_text = stub.calls[0]["messages"][0]["content"] if stub.calls else ""
    return run, stub, window_text


def test_routeros_only_keeps_inventory_hosts(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])  # identity core-1, address 172.25.110.1
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [
            ("core-1", NOW, "interface,info", "a routeros line"),
            ("wave-ptp-60", NOW + timedelta(seconds=1), "daemon,info", "a ubiquiti line"),
        ],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    run, _stub, window_text = _run(cfg, store)

    assert run.new_event_count == 1  # only the RouterOS host reached the model
    assert "core-1" in window_text and "wave-ptp-60" not in window_text


def test_routeros_only_matches_by_address(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])  # address 172.25.110.1
    # A RouterOS device that logs under its bare IP (its inventory address) is still in scope.
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [("172.25.110.1", NOW, "wireguard,info", "handshake giving up")],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    run, _stub, window_text = _run(cfg, store)
    assert run.new_event_count == 1 and "172.25.110.1" in window_text


def test_ignore_topics_drops_matching(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, ignore_topics=["dhcp,info"])
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [
            ("core-1", NOW, "dhcp,info", "routine lease chatter"),
            ("core-1", NOW + timedelta(seconds=1), "system,error", "disk read failure"),
        ],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    run, _stub, window_text = _run(cfg, store)

    assert run.new_event_count == 1  # the dhcp,info line was dropped
    assert "disk read failure" in window_text and "routine lease chatter" not in window_text


def test_fully_filtered_window_skips_model_but_advances_watermark(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])
    # Seed only out-of-scope hosts → everything is filtered out.
    seed_logs(
        cfg.logs.mirror_root,
        cfg.logs.db_path,
        [
            ("wave-a-60", NOW, "daemon,info", "x"),
            ("wave-b-60", NOW + timedelta(seconds=1), "daemon,info", "y"),
        ],
    )
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    run, stub, _ = _run(cfg, store)

    assert stub.calls == []  # the model was never invoked
    assert run.new_event_count == 0 and run.outcome == "no_op"
    assert run.cursor_after is not None and run.cursor_after.id > 0  # watermark still advanced
