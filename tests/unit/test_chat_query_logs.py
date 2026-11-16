"""Unit (feature 013): the `_logs_query` handler renders ingested syslog, passes filters through,
degrades cleanly when logs are absent/unavailable, and audits exactly one logs_query record."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.models import LogEvent, SourceOutcome
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import FakeLogSource, chat_config, logs_settings


def _event(host: str, topics: str, message: str, severity: str | None) -> LogEvent:
    return LogEvent(
        ts=datetime(2026, 6, 3, 11, 0, 0, tzinfo=UTC),
        host=host,
        topics=topics,
        message=message,
        subsystem=topics.split(",")[0],
        severity=severity,
    )


def _ctx(tmp_path: Path, source: FakeLogSource | None, *, logs: bool = True) -> ToolContext:
    config = chat_config(tmp_path, routeros=False, logs=logs_settings() if logs else None)
    return ToolContext(
        config=config,
        driver=None,
        log_source=source,
        audit=AuditTrail(config.audit_path),
        question="what did chestnut-gw log?",
    )


def _run(ctx: ToolContext, args: dict[str, object] | None = None) -> str:
    return build_dispatch(ctx, build_tools())["logs_query"](args or {})


def test_renders_log_lines_and_audits_once(tmp_path: Path) -> None:
    source = FakeLogSource(
        [
            _event("chestnut-gw", "system,error", "login failure for admin", "error"),
            _event("chestnut-gw", "dhcp,info", "assigned 100.90.13.55", "info"),
        ]
    )
    out = _run(_ctx(tmp_path, source))

    assert "chestnut-gw" in out
    assert "login failure for admin" in out
    assert "system,error" in out  # topics shown
    assert "newest first" in out and "by severity" in out  # header summary

    # Exactly one logs_query audit record, carrying the question as intent (Principle V).
    trail = (tmp_path / "audit.jsonl").read_text()
    records = [ln for ln in trail.splitlines() if '"logs_query"' in ln]
    assert len(records) == 1
    assert "what did chestnut-gw log?" in records[0]


def test_filters_passed_through(tmp_path: Path) -> None:
    source = FakeLogSource([_event("chestnut-gw", "system,error", "x", "error")])
    _run(
        _ctx(tmp_path, source),
        {"host": "chestnut", "severity": "error", "contains": "login", "since": "1h", "limit": 10},
    )
    call = source.calls[-1]
    assert call["host"] == "chestnut"
    assert call["severity"] == "error"
    assert call["contains"] == "login"
    assert call["limit"] == 10
    assert isinstance(call["since"], datetime)  # "1h" parsed to a concrete instant


def test_not_configured_message_and_no_audit(tmp_path: Path) -> None:
    # No [logs] and no source → a clear message, and the engine is never called (no audit record).
    out = _run(_ctx(tmp_path, None, logs=False))
    assert "No device logs are configured" in out
    audit = tmp_path / "audit.jsonl"
    assert not audit.exists() or '"logs_query"' not in audit.read_text()


def test_unavailable_store_degrades(tmp_path: Path) -> None:
    source = FakeLogSource([], outcome=SourceOutcome.UNREACHABLE, detail="no log store")
    out = _run(_ctx(tmp_path, source))
    assert "unavailable" in out.lower()


def test_empty_result_message(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, FakeLogSource([])), {"host": "nope"})
    assert "No log lines matched" in out


def test_truncated_hint(tmp_path: Path) -> None:
    source = FakeLogSource(
        [_event("chestnut-gw", "dhcp,info", f"line {i}", "info") for i in range(5)],
        truncated=True,
    )
    out = _run(_ctx(tmp_path, source), {"limit": 5})
    assert "most recent" in out  # narrow-to-see-more hint


# --- 050: the `syslog` source family is one vocabulary across every surface ---------------------


def test_source_family_passed_through(tmp_path: Path) -> None:
    """The reserved `syslog` value threads unchanged to the shared store query (where it resolves to
    device syslog only) — chat gains the value for free via `_filter_clauses`."""

    source = FakeLogSource([_event("chestnut-gw", "system,info", "x", "info")])
    _run(_ctx(tmp_path, source), {"source": "syslog"})
    assert source.calls[-1]["source"] == "syslog"


def test_source_schema_names_the_source_families() -> None:
    """The `logs_query` tool documents the three families so the chat LLM stops guessing collector
    hostnames (FR-007, US2)."""

    logs_query = next(t for t in build_tools() if t.name == "logs_query")
    description = logs_query.input_schema["properties"]["source"]["description"]
    for family in ("syslog", "unifi", "uisp"):
        assert family in description, family
