"""Audit trail source (feature 134, T029): an ALLOWLIST of non-conversational events.

FR-013 excludes everything people said and everything a model composed from it: every
``llm_call``, ``human_message`` and ``report`` row, and ANY row — tool calls included — whose
source is a conversational surface (chat, voice, Slack, radio, MCP). What remains is what obi did
to the network (``device_command``, ``action``, ``tool_call`` from the CLI, web tool route,
watcher, mesh daemon). Payloads are rewritten, then truncated above the profile's byte cap
(research R7: device output dominates the volume); every drop and truncation is counted.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from obi.core import auditstore
from obi.core.demo import open_source_db
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport, finish_db, new_db

NAME = "audit"
KEPT_KINDS = frozenset({"device_command", "action", "tool_call"})
CONVERSATIONAL = frozenset({"chat", "voice", "slack", "radio", "mcp"})
_JSONL_DROP_PREFIXES = ("chat", "slack", "radio", "voice", "mcp", "page", "human", "llm")
_BATCH = 5000


def is_conversational(source: str) -> bool:
    return source.split(":", 1)[0].casefold() in CONVERSATIONAL


def _clip(obj: Any, limit: int) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + "…[truncated]"
    if isinstance(obj, dict):
        return {k: _clip(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip(v, limit) for v in obj[:200]]
    return obj


def _payload(ctx: SourceContext, text: str) -> tuple[str, int, int | None]:
    """Rewrite the JSON payload; (payload, truncated flag, original byte count when truncated).

    Long device output is cut BEFORE the rewrite (rewriting text that is then thrown away
    dominated the first live run); the rewrite still sees everything that ships. A payload whose
    original exceeded the cap is flagged truncated and counted, however it was cut.
    """

    cap = ctx.profile.audit_payload_max_bytes
    original = len(text.encode("utf-8"))
    try:
        rewritten = json.dumps(
            ctx.rewriter.json(_clip(json.loads(text), cap // 2)), ensure_ascii=False
        )
    except ValueError:
        rewritten = json.dumps({"text": ctx.rewriter.text(_clip(text, cap // 2))})
    if len(rewritten.encode("utf-8")) > cap:
        prefix = rewritten.encode("utf-8")[: cap // 2].decode("utf-8", errors="ignore")
        rewritten = json.dumps({"truncated": True, "prefix": prefix + "…[truncated]"})
    if original <= cap:
        return rewritten, 0, None
    ctx.count_excluded("audit-payload-truncated")
    return rewritten, 1, original


def _events(ctx: SourceContext) -> tuple[int, int]:
    src = ctx.state_dir / "audit.db"
    if not src.exists():
        return 0, 0
    rw, shift = ctx.rewriter, ctx.shift
    conn = open_source_db(src)
    out = new_db(ctx.out_state / "audit.db", auditstore.connect)
    rows_in = rows_out = 0
    batch: list[tuple[Any, ...]] = []
    sampled: dict[tuple[str, str], int] = {}

    def flush() -> None:
        out.executemany(
            "INSERT INTO events (ts_us, ts_iso, kind, source, session_id, target, summary,"
            " payload, duration_ms, outcome, error, truncated, original_bytes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
        batch.clear()

    try:
        for row in conn.execute(
            "SELECT * FROM events WHERE ts_us BETWEEN ? AND ? ORDER BY ts_us, id", ctx.window
        ):
            rows_in += 1
            if row["kind"] not in KEPT_KINDS:
                ctx.count_excluded("audit-kind-not-allowlisted")
                continue
            if is_conversational(row["source"]):
                ctx.count_excluded("audit-conversational-source")
                continue
            if row["kind"] == "device_command" and row["outcome"] == "ok":
                # Routine polling dominates the volume: keep 1 in N per (surface, target).
                series = (str(row["source"]).split(":", 1)[0], str(row["target"]))
                n = sampled.get(series, 0)
                sampled[series] = n + 1
                if n % ctx.profile.audit_command_sample:
                    ctx.count_excluded("audit-device-command-sampled")
                    continue
            payload, truncated, original = _payload(ctx, row["payload"])
            batch.append(
                (shift.us(row["ts_us"]), shift.iso(row["ts_iso"]), row["kind"],
                 rw.text(row["source"]),
                 rw.text(row["session_id"]) if row["session_id"] else None,
                 rw.ident(row["target"]) if row["target"] else None, rw.text(row["summary"]),
                 payload, row["duration_ms"], row["outcome"],
                 rw.text(row["error"]) if row["error"] else None,
                 truncated or row["truncated"], original or row["original_bytes"])
            )  # fmt: skip
            rows_out += 1
            if len(batch) >= _BATCH:
                flush()
        if batch:
            flush()
    finally:
        conn.close()
        finish_db(out)
    return rows_in, rows_out


def _jsonl(ctx: SourceContext) -> tuple[int, int]:
    src = ctx.state_dir / "audit.jsonl"
    if not src.exists():
        return 0, 0
    lo, hi = ctx.window
    rows_in = rows_out = 0
    lines: list[str] = []
    with src.open(encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            try:
                record = json.loads(raw)
                stamp = datetime.fromisoformat(str(record["timestamp"]))
            except (ValueError, KeyError, TypeError):
                continue
            ts = int(stamp.timestamp() * 1_000_000)
            if not lo <= ts <= hi:
                continue
            rows_in += 1
            action = str(record.get("action", "")).casefold()
            if action.startswith(_JSONL_DROP_PREFIXES):
                ctx.count_excluded("audit-conversational-source")
                continue
            if "actor" in record and record["actor"] is not None:
                record["actor"] = ctx.mapping.person(str(record["actor"]))
            lines.append(json.dumps(ctx.rewriter.json(record), ensure_ascii=False))
            rows_out += 1
    (ctx.out_state / "audit.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )
    return rows_in, rows_out


def run(ctx: SourceContext) -> SourceReport:
    if not (ctx.state_dir / "audit.db").exists() and not (ctx.state_dir / "audit.jsonl").exists():
        return SourceReport(NAME, ABSENT)
    a_in, a_out = _events(ctx)
    j_in, j_out = _jsonl(ctx)
    return SourceReport(NAME, INCLUDED, rows_in=a_in + j_in, rows_out=a_out + j_out)
