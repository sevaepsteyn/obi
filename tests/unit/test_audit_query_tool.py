"""T013 (US4): the `audit_query` registry tool — bounded, honest, summary-only (feature 101).

Seeds the 042 store directly via ``auditstore.insert_event`` and drives the tool through
``build_dispatch`` exactly as a surface would. Payloads must never surface (summaries only);
empties must be honest (090 rule); validation failures must come back as ``error: …`` strings.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from obi.core import auditstore
from obi.core.audit import AuditTrail
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import chat_config, logs_settings


def _ctx(tmp_path: Path, **config_overrides: Any) -> ToolContext:
    config = chat_config(tmp_path, routeros=False, **config_overrides)
    return ToolContext(
        config=config, driver=None, audit=AuditTrail(config.audit_path), question="q"
    )


def _seed(
    ctx: ToolContext,
    *,
    ts: datetime,
    kind: str = "tool_call",
    source: str = "chat",
    target: str | None = None,
    summary: str = "tool something",
    payload: dict[str, Any] | None = None,
    outcome: str = "ok",
) -> None:
    conn = auditstore.connect(ctx.config.audit.db_path)
    try:
        auditstore.insert_event(
            conn,
            ts=ts,
            kind=kind,
            source=source,
            session_id="seed-session",
            target=target,
            summary=summary,
            payload=payload or {},
            duration_ms=None,
            outcome=outcome,
            error=None,
            truncated=False,
            original_bytes=None,
        )
    finally:
        conn.close()


def _query(ctx: ToolContext, args: dict[str, Any]) -> str:
    return build_dispatch(ctx, build_tools())["audit_query"](args)


def test_no_store_yet_is_an_honest_message(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = _query(ctx, {})
    assert "no audit store yet" in out
    assert str(ctx.config.audit.db_path) in out


def test_lists_newest_first_with_footer(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime.now(UTC)
    _seed(ctx, ts=now - timedelta(hours=2), summary="oldest event")
    _seed(ctx, ts=now - timedelta(hours=1), summary="middle event")
    _seed(ctx, ts=now - timedelta(minutes=5), summary="newest event")

    out = _query(ctx, {})

    assert out.index("newest event") < out.index("middle event") < out.index("oldest event")
    assert "shown 3 of 3 matching events (last 24h)" in out


def test_hours_window_excludes_older_events(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime.now(UTC)
    _seed(ctx, ts=now - timedelta(hours=30), summary="ancient event")
    _seed(ctx, ts=now - timedelta(minutes=5), summary="fresh event")

    out = _query(ctx, {"hours": 1})

    assert "fresh event" in out and "ancient event" not in out
    assert "shown 1 of 1 matching events (last 1h)" in out


def test_kind_source_and_target_filters(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime.now(UTC)
    _seed(ctx, ts=now, kind="tool_call", source="chat", target="gw", summary="a tool ran")
    _seed(ctx, ts=now, kind="llm_call", source="watch", target=None, summary="a model call")

    assert "a model call" not in _query(ctx, {"kind": "tool_call"})
    assert "a tool ran" not in _query(ctx, {"source": "watch"})
    assert "a model call" not in _query(ctx, {"target": "gw"})


def test_text_search_matches_summaries(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime.now(UTC)
    _seed(ctx, ts=now, summary="ping toward basswood-gw")
    _seed(ctx, ts=now, summary="metrics history read")

    out = _query(ctx, {"text": "basswood"})

    assert "basswood-gw" in out and "metrics history" not in out


def test_limit_caps_lines_but_footer_counts_all(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime.now(UTC)
    for i in range(5):
        _seed(ctx, ts=now - timedelta(minutes=i), summary=f"event number {i}")

    out = _query(ctx, {"limit": 2})

    assert "shown 2 of 5 matching events" in out
    assert out.count("event number") == 2


def test_zero_matches_is_honest_and_restates_filters(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, ts=datetime.now(UTC), kind="tool_call", summary="only a tool call")

    out = _query(ctx, {"kind": "report"})

    assert "no matching events" in out
    assert "report" in out  # the active filter is restated


def test_validation_errors_are_error_strings(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, ts=datetime.now(UTC))

    assert _query(ctx, {"kind": "nonsense"}).startswith("error")
    assert "llm_call" in _query(ctx, {"kind": "nonsense"})  # the valid kinds are listed
    assert _query(ctx, {"hours": 0}).startswith("error")
    assert _query(ctx, {"hours": 99999}).startswith("error")
    assert _query(ctx, {"limit": 0}).startswith("error")
    assert _query(ctx, {"limit": 500}).startswith("error")


def test_payload_content_never_surfaces(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(
        ctx,
        ts=datetime.now(UTC),
        summary="a call with a rich payload",
        payload={"result": "TOPSECRETPAYLOADBODY", "arguments": {"x": 1}},
    )

    out = _query(ctx, {})

    assert "a call with a rich payload" in out
    assert "TOPSECRETPAYLOADBODY" not in out


def test_error_outcome_is_visible_on_the_line(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, ts=datetime.now(UTC), summary="a failed call", outcome="error")

    out = _query(ctx, {})
    (line,) = [ln for ln in out.splitlines() if "a failed call" in ln]
    assert "error" in line


def test_timestamps_render_in_log_tz_when_configured(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, logs=logs_settings(log_tz="America/Los_Angeles"))
    # 15:00 UTC = 08:00 PDT on this date.
    _seed(ctx, ts=datetime(2026, 8, 20, 15, 0, tzinfo=UTC), summary="tz probe", )

    out = _query(ctx, {"hours": 2160})

    (line,) = [ln for ln in out.splitlines() if "tz probe" in ln]
    assert "08:00" in line


def test_store_is_opened_readonly(tmp_path: Path, monkeypatch: Any) -> None:
    """The tool must never create or write the store — connect_readonly is the only door."""

    ctx = _ctx(tmp_path)
    _seed(ctx, ts=datetime.now(UTC))

    def refuse_write(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise AssertionError("audit_query must not open the store read-write")

    monkeypatch.setattr(auditstore, "connect", refuse_write)
    assert "shown 1 of 1" in _query(ctx, {})
