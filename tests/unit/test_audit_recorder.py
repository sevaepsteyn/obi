"""Unit (feature 042): AuditRecorder — ambient scope, fail-open, redaction backstop, bounds."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import auditstore
from obi.core.audit import (
    AuditRecorder,
    AuditTrail,
    audit_scope,
    current_session_id,
    record_device_command,
    record_event,
    recorder_from_config,
)
from obi.core.config import AuditSettings, load_config
from obi.core.models import AuditOutcome, AuditRecord


def _settings(tmp_path: Path, **overrides: object) -> AuditSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "db_path": tmp_path / "audit.db",
        "retention_days": 90,
        "max_payload_bytes": 5_000,
    }
    defaults.update(overrides)
    return AuditSettings(**defaults)  # type: ignore[arg-type]


def _events(tmp_path: Path) -> list[dict[str, object]]:
    conn = auditstore.connect_readonly(tmp_path / "audit.db")
    try:
        rows, _ = auditstore.query_events(conn, limit=1000, include_payload=True)
        return list(reversed(rows))  # oldest first for assertion convenience
    finally:
        conn.close()


def test_record_stamps_ambient_source_and_session(tmp_path: Path) -> None:
    recorder = AuditRecorder(_settings(tmp_path))
    with audit_scope(recorder, "chat", "chat-abc"):
        assert current_session_id() == "chat-abc"
        record_event("human_message", summary="hi", payload={"direction": "in", "content": "hi"})
    events = _events(tmp_path)
    assert len(events) == 1
    assert events[0]["source"] == "chat"
    assert events[0]["session_id"] == "chat-abc"
    recorder.close()


def test_no_scope_is_a_silent_noop(tmp_path: Path) -> None:
    record_event("tool_call", summary="x", payload={})
    record_device_command("core-gw-1", "snmp", "walk interfaces")
    assert not (tmp_path / "audit.db").exists()
    assert current_session_id() is None


def test_none_recorder_scope_is_a_noop(tmp_path: Path) -> None:
    with audit_scope(None, "chat", "chat-abc"):
        record_event("tool_call", summary="x", payload={})
        assert current_session_id() is None
    assert not (tmp_path / "audit.db").exists()


def test_fail_open_on_unwritable_store(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    recorder = AuditRecorder(_settings(tmp_path, db_path=blocked / "audit.db"))
    with audit_scope(recorder, "chat", "chat-1"):
        record_event("tool_call", summary="x", payload={})  # must not raise
        record_event("tool_call", summary="y", payload={})
    assert recorder.failures == 2


def test_secret_backstop_scrubs_nested_payloads(tmp_path: Path) -> None:
    recorder = AuditRecorder(_settings(tmp_path), known_secrets=("hunter2secret",))
    with audit_scope(recorder, "chat", None):
        record_event(
            "llm_call",
            summary="model call with hunter2secret inside",
            payload={
                "messages": [{"role": "user", "content": "the password is hunter2secret ok"}],
                "nested": {"deep": ["hunter2secret"]},
            },
        )
    recorder.close()
    (event,) = _events(tmp_path)
    text = json.dumps(event)
    assert "hunter2secret" not in text
    assert "<redacted-by-obi>" in text


def test_short_secrets_are_not_scrubbed(tmp_path: Path) -> None:
    recorder = AuditRecorder(_settings(tmp_path), known_secrets=("ab",))
    with audit_scope(recorder, "chat", None):
        record_event("tool_call", summary="about", payload={"content": "about"})
    recorder.close()
    (event,) = _events(tmp_path)
    assert event["summary"] == "about"


def test_oversized_field_truncated_with_marker(tmp_path: Path) -> None:
    recorder = AuditRecorder(_settings(tmp_path, max_payload_bytes=5_000))
    big = "x" * 20_000
    with audit_scope(recorder, "watch", "watch-1"):
        record_event("llm_call", summary="big", payload={"response": big, "small": "ok"})
    recorder.close()
    (event,) = _events(tmp_path)
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert "truncated by obi audit" in str(payload["response"])
    assert payload["small"] == "ok"
    assert event["truncated"] is True
    assert isinstance(event["original_bytes"], int) and event["original_bytes"] > 20_000


def test_audit_trail_tee_produces_action_events(tmp_path: Path) -> None:
    recorder = AuditRecorder(_settings(tmp_path))
    trail = AuditTrail(tmp_path / "audit.jsonl")
    record = AuditRecord(
        timestamp=datetime.now(UTC),
        action="routeros_status",
        target="core-gw-1",
        reason="is it up?",
        outcome=AuditOutcome.OK,
        params={"password": "nope", "path": "/system/resource"},
    )
    with audit_scope(recorder, "chat", "chat-7"):
        trail.record(record)
    recorder.close()

    # JSONL keeps working…
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    # …and the same record landed in the store as an `action` event, post-key-redaction.
    (event,) = _events(tmp_path)
    assert event["kind"] == "action"
    assert event["target"] == "core-gw-1"
    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["params"]["password"] == "***"
    assert payload["reason"] == "is it up?"


def test_audit_trail_without_scope_writes_jsonl_only(tmp_path: Path) -> None:
    trail = AuditTrail(tmp_path / "audit.jsonl")
    trail.record(
        AuditRecord(
            timestamp=datetime.now(UTC),
            action="discover",
            target="scope",
            reason="cli",
            outcome=AuditOutcome.OK,
            params={},
        )
    )
    assert (tmp_path / "audit.jsonl").exists()
    assert not (tmp_path / "audit.db").exists()


def test_prune_on_first_write(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    conn = auditstore.connect(db)
    auditstore.insert_event(
        conn,
        ts=datetime.now(UTC) - timedelta(days=400),
        kind="tool_call",
        source="chat",
        session_id=None,
        target=None,
        summary="ancient",
        payload={},
        duration_ms=None,
        outcome="ok",
        error=None,
        truncated=False,
        original_bytes=None,
    )
    conn.close()

    recorder = AuditRecorder(_settings(tmp_path, retention_days=90))
    with audit_scope(recorder, "chat", None):
        record_event("tool_call", summary="fresh", payload={})
    recorder.close()
    events = _events(tmp_path)
    assert [e["summary"] for e in events] == ["fresh"]


def test_recorder_from_config_disabled_and_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[routeros]\n"
        'username = "admin"\n'
        'password = "supersecretpw"\n'
        "[discovery]\n"
        'seeds = ["172.25.110.1"]\n'
        "[audit]\n"
        f'db_path = "{tmp_path / "audit.db"}"\n'
    )
    config = load_config(config_path)
    recorder = recorder_from_config(config)
    assert recorder is not None
    with audit_scope(recorder, "cli:status", "cli-1"):
        record_event("tool_call", summary="pw is supersecretpw", payload={})
    recorder.close()
    (event,) = _events(tmp_path)
    assert "supersecretpw" not in json.dumps(event)

    config_path.write_text(
        "[routeros]\n"
        'username = "admin"\n'
        'password = "supersecretpw"\n'
        "[discovery]\n"
        'seeds = ["172.25.110.1"]\n'
        "[audit]\n"
        "enabled = false\n"
    )
    assert recorder_from_config(load_config(config_path)) is None
