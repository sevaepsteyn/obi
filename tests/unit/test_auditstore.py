"""Unit (feature 042): the audit event store — schema, filters, FTS, trace, stats, prune."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from obi.core import auditstore

_BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _insert(
    conn: Any,
    *,
    offset_s: float = 0.0,
    kind: str = "tool_call",
    source: str = "chat",
    session_id: str | None = "chat-1",
    target: str | None = None,
    summary: str = "a tool ran",
    payload: dict[str, Any] | None = None,
    duration_ms: float | None = 12.5,
    outcome: str = "ok",
    error: str | None = None,
) -> int:
    return auditstore.insert_event(
        conn,
        ts=_BASE + timedelta(seconds=offset_s),
        kind=kind,
        source=source,
        session_id=session_id,
        target=target,
        summary=summary,
        payload=payload or {"tool": "logs_query"},
        duration_ms=duration_ms,
        outcome=outcome,
        error=error,
        truncated=False,
        original_bytes=None,
    )


def _store(tmp_path: Path) -> Any:
    return auditstore.connect(tmp_path / "audit.db")


def test_insert_and_read_roundtrip(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    event_id = _insert(conn, payload={"tool": "logs_query", "arguments": {"host": "core-gw-1"}})
    event = auditstore.get_event(conn, event_id)
    assert event is not None
    assert event["kind"] == "tool_call"
    assert event["source"] == "chat"
    assert event["session_id"] == "chat-1"
    assert event["payload"]["arguments"] == {"host": "core-gw-1"}
    assert event["outcome"] == "ok"
    assert event["truncated"] is False


def test_get_event_unknown_id(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    assert auditstore.get_event(conn, 999) is None


def test_filters_compose_and(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    _insert(conn, offset_s=0, kind="llm_call", source="watch", session_id="watch-1")
    _insert(conn, offset_s=10, kind="tool_call", source="watch", session_id="watch-1")
    _insert(conn, offset_s=20, kind="tool_call", source="chat", session_id="chat-1")
    _insert(conn, offset_s=30, kind="tool_call", source="chat", session_id="chat-1",
            outcome="error", error="boom")

    rows, total = auditstore.query_events(conn, kinds=["tool_call"], sources=["chat"])
    assert total == 2
    rows, total = auditstore.query_events(
        conn, kinds=["tool_call"], sources=["chat"], outcome="error"
    )
    assert total == 1
    assert rows[0]["error"] == "boom"

    # Time range bounds inclusive both ends.
    rows, total = auditstore.query_events(
        conn, since=_BASE + timedelta(seconds=10), until=_BASE + timedelta(seconds=20)
    )
    assert total == 2

    rows, total = auditstore.query_events(conn, session_id="watch-1")
    assert total == 2

    rows, total = auditstore.query_events(conn, kinds=["llm_call", "tool_call"])
    assert total == 4


def test_target_filter_and_newest_first(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    _insert(conn, offset_s=0, kind="device_command", target="core-gw-1")
    _insert(conn, offset_s=5, kind="device_command", target="chestnut-gw")
    _insert(conn, offset_s=9, kind="device_command", target="core-gw-1")

    rows, total = auditstore.query_events(conn, target="core-gw-1")
    assert total == 2
    assert rows[0]["ts"] > rows[1]["ts"]  # newest first


def test_limit_and_total(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    for i in range(10):
        _insert(conn, offset_s=i)
    rows, total = auditstore.query_events(conn, limit=3)
    assert len(rows) == 3
    assert total == 10


def test_free_text_search_over_summary_and_payload(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    _insert(conn, summary="ping core-gw-1", payload={"tool": "routeros_ping"})
    _insert(conn, summary="ask the model", payload={"content": "is tamarack-gw up?"})
    _insert(conn, summary="unrelated", payload={"tool": "snmp_metrics"})

    rows, total = auditstore.query_events(conn, text="core-gw-1")
    assert total == 1
    assert rows[0]["summary"] == "ping core-gw-1"
    # payload text matches too
    rows, total = auditstore.query_events(conn, text="tamarack-gw")
    assert total == 1


def test_trace_orders_causally(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    # Same-microsecond events must keep insertion order (id tiebreak).
    _insert(conn, offset_s=0, kind="human_message", session_id="chat-9", summary="in")
    _insert(conn, offset_s=0, kind="llm_call", session_id="chat-9", summary="llm")
    _insert(conn, offset_s=1, kind="tool_call", session_id="chat-9", summary="tool")
    _insert(conn, offset_s=2, kind="human_message", session_id="chat-9", summary="out")
    _insert(conn, offset_s=1, kind="tool_call", session_id="other", summary="noise")

    trace = auditstore.session_trace(conn, "chat-9")
    assert [e["kind"] for e in trace] == ["human_message", "llm_call", "tool_call", "human_message"]
    assert all("payload" in e for e in trace)


def test_stats_aggregates(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    _insert(conn, offset_s=0, kind="llm_call", source="chat",
            payload={"model": "m", "input_tokens": 100, "output_tokens": 10})
    _insert(conn, offset_s=60, kind="llm_call", source="watch",
            payload={"model": "m", "input_tokens": 200, "output_tokens": 20})
    _insert(conn, offset_s=30, kind="tool_call", source="chat", payload={"tool": "logs_query"})
    _insert(conn, offset_s=40, kind="tool_call", source="chat", payload={"tool": "logs_query"})
    _insert(conn, offset_s=50, kind="tool_call", source="chat", payload={"tool": "snmp_metrics"})
    _insert(conn, offset_s=55, kind="device_command", target="core-gw-1", outcome="error",
            error="timeout")

    result = auditstore.stats(conn)
    assert result["by_kind"] == {"llm_call": 2, "tool_call": 3, "device_command": 1}
    assert result["by_source"] == {"chat": 5, "watch": 1}
    assert result["tokens"]["total_input"] == 300
    assert result["tokens"]["total_output"] == 30
    assert result["tokens"]["by_source"]["watch"] == {"input": 200, "output": 20}
    assert result["top_tools"][0] == {"tool": "logs_query", "count": 2}
    assert result["top_targets"] == [{"target": "core-gw-1", "count": 1}]
    assert result["errors"] == 1
    assert sum(b["input"] for b in result["tokens_over_time"]) == 300


def test_stats_empty_store(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    result = auditstore.stats(conn)
    assert result["by_kind"] == {}
    assert result["tokens"]["total_input"] == 0
    assert result["tokens_over_time"] == []


def test_prune_deletes_only_old_rows(tmp_path: Path) -> None:
    conn = _store(tmp_path)
    _insert(conn, offset_s=0)
    _insert(conn, offset_s=3600)
    deleted = auditstore.prune(conn, older_than=_BASE + timedelta(seconds=1800))
    assert deleted == 1
    _, total = auditstore.query_events(conn)
    assert total == 1
    # FTS stays consistent after the delete trigger.
    _, total = auditstore.query_events(conn, text="tool")
    assert total == 1


def test_store_status_present_and_absent(tmp_path: Path) -> None:
    missing = auditstore.store_status(tmp_path / "nope.db")
    assert missing["present"] is False

    conn = _store(tmp_path)
    _insert(conn, offset_s=0)
    _insert(conn, offset_s=60)
    conn.close()
    status = auditstore.store_status(tmp_path / "audit.db")
    assert status["present"] is True
    assert status["event_count"] == 2
    assert status["size_bytes"] > 0
    assert status["oldest"] <= status["newest"]


def test_concurrent_writers_lose_nothing(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    auditstore.connect(db).close()  # create schema once

    def write_batch(worker: int) -> None:
        conn = auditstore.connect(db)
        for i in range(25):
            _insert(conn, offset_s=worker * 100 + i, session_id=f"w{worker}")
        conn.close()

    threads = [threading.Thread(target=write_batch, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conn = auditstore.connect_readonly(db)
    _, total = auditstore.query_events(conn, limit=1)
    assert total == 100
