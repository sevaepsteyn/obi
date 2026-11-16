"""Contract (feature 042, T020): the `obi audit` command family per contracts/audit-cli.md."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from obi.cli.main import app
from obi.core import auditstore

runner = CliRunner()

_BASE = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def _config(tmp_path: Path, *, enabled: bool = True) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "[logs]\n"
        "[audit]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        f'db_path = "{tmp_path / "audit.db"}"\n'
    )
    return path


def _seed(tmp_path: Path) -> None:
    conn = auditstore.connect(tmp_path / "audit.db")

    def put(offset_s: float, kind: str, source: str, session: str | None,
            summary: str, payload: dict[str, Any], *, target: str | None = None,
            outcome: str = "ok", error: str | None = None) -> int:
        return auditstore.insert_event(
            conn, ts=_BASE + timedelta(seconds=offset_s), kind=kind, source=source,
            session_id=session, target=target, summary=summary, payload=payload,
            duration_ms=10.0, outcome=outcome, error=error, truncated=False,
            original_bytes=None,
        )

    put(0, "human_message", "chat", "chat-s1", "how is core-gw-1?",
        {"direction": "in", "content": "how is core-gw-1?"})
    put(1, "llm_call", "chat", "chat-s1", "model call",
        {"model": "m", "messages": [], "response": [], "input_tokens": 100,
         "output_tokens": 10, "system": "s", "stop_reason": "tool_use"})
    put(2, "tool_call", "chat", "chat-s1", "tool routeros_status",
        {"tool": "routeros_status", "arguments": {"target": "core-gw-1"},
         "result": "up", "is_error": False})
    put(3, "device_command", "chat", "chat-s1", "[routeros-api] core-gw-1: probe",
        {"transport": "routeros-api", "operation": "probe"}, target="core-gw-1")
    put(4, "human_message", "chat", "chat-s1", "it is up",
        {"direction": "out", "content": "it is up"})
    put(60, "llm_call", "watch", "watch-w1", "judge call",
        {"model": "m", "messages": [], "response": [], "input_tokens": 500,
         "output_tokens": 50, "system": "s", "stop_reason": "end_turn"})
    put(61, "device_command", "watch", "watch-w1", "[snmp] chestnut-gw: walk",
        {"transport": "snmp", "operation": "walk 1.2.3"}, target="chestnut-gw",
        outcome="error", error="timeout")
    conn.close()


def test_list_filters_compose(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _config(tmp_path)

    result = runner.invoke(app, ["audit", "list", "--scope", str(config), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["total"] == 7
    assert data["events"][0]["id"] > data["events"][1]["id"]  # newest first

    result = runner.invoke(
        app,
        ["audit", "list", "--scope", str(config), "--json",
         "--kind", "llm_call", "--source", "watch"],
    )
    data = json.loads(result.stdout)
    assert data["total"] == 1
    assert data["events"][0]["source"] == "watch"

    result = runner.invoke(
        app, ["audit", "list", "--scope", str(config), "--json", "--outcome", "error"]
    )
    data = json.loads(result.stdout)
    assert data["total"] == 1
    assert data["events"][0]["error"] == "timeout"

    result = runner.invoke(
        app, ["audit", "list", "--scope", str(config), "--json", "--target", "core-gw-1"]
    )
    assert json.loads(result.stdout)["total"] == 1

    result = runner.invoke(
        app, ["audit", "list", "--scope", str(config), "--json", "--text", "core-gw-1"]
    )
    assert json.loads(result.stdout)["total"] >= 2  # summary + payload matches

    result = runner.invoke(
        app, ["audit", "list", "--scope", str(config), "--json", "--limit", "2"]
    )
    data = json.loads(result.stdout)
    assert len(data["events"]) == 2 and data["total"] == 7


def test_list_human_table(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(app, ["audit", "list", "--scope", str(_config(tmp_path))])
    assert result.exit_code == 0
    assert "llm_call" in result.stdout  # kind column renders (summaries may wrap)
    assert "7 of 7 matching" in result.stdout


def test_show_full_payload_and_unknown_id(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _config(tmp_path)

    listing = json.loads(
        runner.invoke(
            app, ["audit", "list", "--scope", str(config), "--json", "--kind", "tool_call"]
        ).stdout
    )
    event_id = listing["events"][0]["id"]

    result = runner.invoke(app, ["audit", "show", str(event_id), "--scope", str(config), "--json"])
    assert result.exit_code == 0
    event = json.loads(result.stdout)
    assert event["payload"]["arguments"] == {"target": "core-gw-1"}
    assert event["payload"]["result"] == "up"

    human = runner.invoke(app, ["audit", "show", str(event_id), "--scope", str(config)])
    assert human.exit_code == 0
    assert "routeros_status" in human.stdout

    missing = runner.invoke(app, ["audit", "show", "99999", "--scope", str(config)])
    assert missing.exit_code == 1


def test_trace_chronological_and_unknown_session(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _config(tmp_path)

    result = runner.invoke(app, ["audit", "trace", "chat-s1", "--scope", str(config), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["human_message", "llm_call", "tool_call", "device_command", "human_message"]
    assert all("payload" in e for e in data["events"])

    human = runner.invoke(app, ["audit", "trace", "chat-s1", "--scope", str(config)])
    assert human.exit_code == 0
    assert "5 event(s)" in human.stdout

    missing = runner.invoke(app, ["audit", "trace", "nope-1", "--scope", str(config)])
    assert missing.exit_code == 1


def test_stats_shape(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _config(tmp_path)

    result = runner.invoke(
        app,
        ["audit", "stats", "--scope", str(config), "--json", "--since", "2026-07-09T00:00:00Z"],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["by_kind"]["llm_call"] == 2
    assert data["by_source"]["chat"] == 5
    assert data["tokens"]["total_input"] == 600
    assert data["tokens"]["by_source"]["watch"] == {"input": 500, "output": 50}
    assert data["top_tools"] == [{"tool": "routeros_status", "count": 1}]
    assert data["top_targets"][0]["target"] in ("core-gw-1", "chestnut-gw")
    assert data["errors"] == 1

    human = runner.invoke(app, ["audit", "stats", "--scope", str(config)])
    assert human.exit_code == 0


def test_status_health_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _config(tmp_path)

    result = runner.invoke(app, ["audit", "status", "--scope", str(config), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["present"] is True
    assert data["enabled"] is True
    assert data["event_count"] == 7
    assert data["size_bytes"] > 0
    assert data["retention_days"] == 90

    human = runner.invoke(app, ["audit", "status", "--scope", str(config)])
    assert human.exit_code == 0
    assert "events: 7" in human.stdout


def test_empty_store_degrades_cleanly(tmp_path: Path) -> None:
    config = _config(tmp_path)  # no _seed → no db file
    for args in (["list"], ["stats"], ["trace", "x"], ["show", "1"]):
        result = runner.invoke(app, ["audit", *args, "--scope", str(config)])
        assert result.exit_code == 0, args
        assert "no audit events yet" in result.output

    status = runner.invoke(app, ["audit", "status", "--scope", str(config), "--json"])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["present"] is False


def test_queries_stay_fast_at_100k_events(tmp_path: Path) -> None:
    """SC-007: list/stats over a 100k-event store answer in well under 2 s."""

    import time

    conn = auditstore.connect(tmp_path / "audit.db")
    rows = []
    for i in range(100_000):
        ts = _BASE + timedelta(seconds=i)
        rows.append(
            (
                int(ts.timestamp() * 1_000_000),
                ts.isoformat(),
                ("llm_call", "tool_call", "device_command")[i % 3],
                ("chat", "watch", "cli:status")[i % 3],
                f"session-{i % 500}",
                f"device-{i % 30}",
                f"event number {i}",
                json.dumps({"tool": f"tool_{i % 12}", "input_tokens": 10, "output_tokens": 2}),
                1.0,
                "ok",
                None,
                0,
                None,
            )
        )
    conn.executemany(
        "INSERT INTO events (ts_us, ts_iso, kind, source, session_id, target, summary, payload,"
        " duration_ms, outcome, error, truncated, original_bytes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    start = time.monotonic()
    _, total = auditstore.query_events(
        conn, kinds=["tool_call"], sources=["watch"], limit=50
    )
    auditstore.stats(conn)
    elapsed = time.monotonic() - start
    conn.close()
    assert total > 0
    assert elapsed < 2.0, f"list+stats took {elapsed:.2f}s over 100k events"


def test_bad_filter_values_exit_2(tmp_path: Path) -> None:
    _seed(tmp_path)
    config = _config(tmp_path)
    assert runner.invoke(
        app, ["audit", "list", "--scope", str(config), "--kind", "bogus"]
    ).exit_code == 2
    assert runner.invoke(
        app, ["audit", "list", "--scope", str(config), "--outcome", "maybe"]
    ).exit_code == 2
