"""Contract (feature 042, T023): GET /api/audit.json per contracts/audit-web-api.md.

Real behavioral tests against the real TLS server and a store seeded with the production
insert code. Covers all four views, filters, absent-store grace, 400s, 404, and POST-405.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from obi.core import auditstore
from obi.core.config import AuditSettings
from obi.web.server import create_server

_BASE = datetime.now(UTC) - timedelta(hours=1)


def _seed(db_path: Path) -> None:
    conn = auditstore.connect(db_path)

    def put(offset_s: float, kind: str, source: str, session: str | None, summary: str,
            payload: dict[str, Any], *, target: str | None = None,
            outcome: str = "ok", error: str | None = None) -> None:
        auditstore.insert_event(
            conn, ts=_BASE + timedelta(seconds=offset_s), kind=kind, source=source,
            session_id=session, target=target, summary=summary, payload=payload,
            duration_ms=5.0, outcome=outcome, error=error, truncated=False,
            original_bytes=None,
        )

    put(0, "human_message", "chat", "chat-s1", "hello",
        {"direction": "in", "content": "hello"})
    put(1, "llm_call", "chat", "chat-s1", "model call",
        {"model": "m", "input_tokens": 100, "output_tokens": 10})
    put(2, "tool_call", "chat", "chat-s1", "tool logs_query",
        {"tool": "logs_query", "arguments": {}, "result": "…", "is_error": False})
    put(3, "device_command", "watch", "watch-w1", "[snmp] chestnut-gw: walk",
        {"transport": "snmp", "operation": "walk 1.2.3"}, target="chestnut-gw",
        outcome="error", error="timeout")
    put(4, "llm_call", "watch", "watch-w1", "judge",
        {"model": "m", "input_tokens": 300, "output_tokens": 20})
    conn.close()


def _serve(tmp_path: Path, audit_settings: AuditSettings | None) -> Iterator[str]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        audit_settings=audit_settings,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "audit.db"
    _seed(db_path)
    yield from _serve(tmp_path, AuditSettings(db_path=db_path))


@pytest.fixture()
def served_absent(tmp_path: Path) -> Iterator[str]:
    yield from _serve(tmp_path, AuditSettings(db_path=tmp_path / "missing.db"))


def _get(base: str, query: str) -> httpx.Response:
    return httpx.get(f"{base}/api/audit.json{query}", verify=False)


def test_list_default_and_filters(served: str) -> None:
    response = _get(served, "")
    assert response.status_code == 200
    body = response.json()
    assert body["store"] == {"present": True, "enabled": True}
    assert body["total"] == 5
    assert body["events"][0]["summary"] == "judge"  # newest first
    assert "payload" not in body["events"][0]  # list shape omits payloads

    body = _get(served, "?kind=llm_call&source=watch").json()
    assert body["total"] == 1

    body = _get(served, "?outcome=error").json()
    assert body["total"] == 1
    assert body["events"][0]["target"] == "chestnut-gw"

    body = _get(served, "?text=hello").json()
    assert body["total"] == 1

    body = _get(served, "?limit=2").json()
    assert len(body["events"]) == 2 and body["total"] == 5

    body = _get(served, "?session=chat-s1").json()
    assert body["total"] == 3


def test_detail_view_and_404(served: str) -> None:
    listing = _get(served, "?kind=tool_call").json()
    event_id = listing["events"][0]["id"]
    body = _get(served, f"?view=detail&id={event_id}").json()
    assert body["event"]["payload"]["tool"] == "logs_query"

    assert _get(served, "?view=detail&id=99999").status_code == 404
    assert _get(served, "?view=detail").status_code == 400


def test_trace_view(served: str) -> None:
    body = _get(served, "?view=trace&session=chat-s1").json()
    assert body["session"] == "chat-s1"
    assert [e["kind"] for e in body["events"]] == ["human_message", "llm_call", "tool_call"]
    assert all("payload" in e for e in body["events"])

    assert _get(served, "?view=trace").status_code == 400


def test_stats_view(served: str) -> None:
    body = _get(served, "?view=stats&since=24h&buckets=10").json()
    assert body["by_kind"]["llm_call"] == 2
    assert body["tokens"]["total_input"] == 400
    assert body["tokens"]["by_source"]["watch"] == {"input": 300, "output": 20}
    assert body["top_tools"] == [{"tool": "logs_query", "count": 1}]
    assert body["top_targets"] == [{"target": "chestnut-gw", "count": 1}]
    assert body["errors"] == 1
    assert sum(b["input"] for b in body["tokens_over_time"]) == 400

    assert _get(served, "?view=stats&buckets=0").status_code == 400


def test_absent_store_is_a_state(served_absent: str) -> None:
    response = _get(served_absent, "")
    assert response.status_code == 200
    body = response.json()
    assert body["store"]["present"] is False
    assert body["events"] == []
    assert "no audit events yet" in body["hint"]


def test_bad_params_400(served: str) -> None:
    assert _get(served, "?view=bogus").status_code == 400
    assert _get(served, "?kind=bogus").status_code == 400
    assert _get(served, "?outcome=maybe").status_code == 400
    assert _get(served, "?limit=0").status_code == 400


def test_post_is_405_and_widget_served(served: str) -> None:
    response = httpx.post(f"{served}/api/audit.json", verify=False)
    assert response.status_code == 405

    widget = httpx.get(f"{served}/widgets/audit.js", verify=False)
    assert widget.status_code == 200
    assert "audit" in widget.text
