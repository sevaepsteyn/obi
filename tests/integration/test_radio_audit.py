"""Integration (feature 088, US4): SC-010 — a radio exchange is explainable from the audit
trail ALONE.

Runs a faked full exchange with a real AuditRecorder attached, then reads only audit.db and
reconstructs the decision chain: the directed call, every outbound with its gate log, and
the closure. No peeking at the radio store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core import auditstore
from obi.core.audit import AuditRecorder
from obi.core.config import AuditSettings
from tests.integration.test_radio_exchange import (
    _CALL_Q_TRANSCRIPTS,
    _CALL_THEN_QUESTION,
    _harness,
)


def test_exchange_is_reconstructable_from_audit_alone(
    tmp_path: Path, monkeypatch: Any
) -> None:
    audit_db = tmp_path / "audit.db"
    recorder = AuditRecorder(AuditSettings(db_path=audit_db))
    # The harness builds its own runner; inject the recorder through the seam it exposes.
    import tests.integration.test_radio_exchange as exchange

    original_runner_init = exchange.RadioRunner.__init__

    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["recorder"] = recorder
        original_runner_init(self, *args, **kwargs)

    monkeypatch.setattr(exchange.RadioRunner, "__init__", patched_init)
    _harness(
        tmp_path,
        _CALL_THEN_QUESTION,
        _CALL_Q_TRANSCRIPTS,
        composer=lambda *a, **k: "mountain link is up, over",
    )

    conn = auditstore.connect_readonly(audit_db)
    try:
        events, total = auditstore.query_events(
            conn, sources=["radio"], limit=50, include_payload=True
        )
    finally:
        conn.close()
    assert total >= 4
    events.reverse()  # oldest first — read as a story

    # One session id threads the whole call.
    sessions = {e["session_id"] for e in events}
    assert len(sessions) == 1 and next(iter(sessions)).startswith("radio-call-")

    summaries = [e["summary"] for e in events]
    assert any("directed call from nono" in s for s in summaries)
    assert any(s.startswith("radio ack:") for s in summaries)
    assert any(s.startswith("radio answer:") for s in summaries)
    assert any("call closed: timeout" in s for s in summaries)

    # Every outbound event carries the deterministic gate's evaluation log (G11).
    outbound = [e for e in events if e["summary"].startswith("radio ")]
    for event in outbound:
        gate_log = json.loads(event["payload"]["gate_log"])
        assert "clear_checks" in gate_log and "render_ms" in gate_log
    ack = next(e for e in events if e["summary"].startswith("radio ack:"))
    assert ack["payload"]["text"] == "nono, go for obi"  # roster-identified caller
