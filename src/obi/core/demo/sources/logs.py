"""logs.db source (feature 134, T027): window, rewrite, recompute ``dedup_key``, rebuild FTS.

Rows are renumbered from 1 (the live ``id`` would reveal the store's lifetime row count), the
``dedup_key`` is recomputed over the OUTPUT values with logstore's own formula, and the ingest
watermarks (file offsets, controller cursors) are dropped — they describe the live host. The
never-served ``raw_line`` ships empty (size, and one less raw copy of every line).
"""

from __future__ import annotations

import hashlib

from obi.core import logstore
from obi.core.demo import open_source_db
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport, finish_db, new_db

NAME = "logs"
_BATCH = 5000


def run(ctx: SourceContext) -> SourceReport:
    src = ctx.state_dir / "logs.db"
    if not src.exists():
        return SourceReport(NAME, ABSENT)
    rw, shift = ctx.rewriter, ctx.shift
    conn = open_source_db(src)
    out = new_db(ctx.out_state / "logs.db", logstore.connect)
    rows_in = rows_out = 0
    batch: list[tuple[object, ...]] = []

    def flush() -> int:
        before = out.total_changes
        out.executemany(
            "INSERT OR IGNORE INTO logs (ts_us, ts_iso, host, subsystem, topics, severity,"
            " message, raw_line, collectors, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
        batch.clear()
        return out.total_changes - before

    try:
        for row in conn.execute(
            "SELECT ts_us, ts_iso, host, subsystem, topics, severity, message, raw_line,"
            " collectors FROM logs WHERE ts_us BETWEEN ? AND ? ORDER BY ts_us, id",
            ctx.window,
        ):
            rows_in += 1
            ts = shift.us(row["ts_us"])
            host = rw.ident(row["host"])
            message = rw.text(row["message"])
            collectors = ",".join(rw.ident(c) for c in str(row["collectors"]).split(","))
            key = hashlib.sha256(f"{ts}\x1f{host}\x1f{message}".encode()).hexdigest()
            # raw_line is stored but never served (log_event_to_dict omits it); shipping it empty
            # halves the store and removes a second, rawer copy of every line.
            batch.append(
                (ts, shift.iso(row["ts_iso"]), host, row["subsystem"], row["topics"],
                 row["severity"], message, "", collectors, key)
            )  # fmt: skip
            if len(batch) >= _BATCH:
                rows_out += flush()
        if batch:
            rows_out += flush()
        out.execute("INSERT INTO logs_fts(logs_fts) VALUES ('rebuild')")
    finally:
        conn.close()
        finish_db(out)
    ctx.count_excluded("log-row-duplicate-after-rewrite", rows_in - rows_out)
    ctx.count_excluded("log-raw-line-emptied", rows_out)
    return SourceReport(NAME, INCLUDED, rows_in=rows_in, rows_out=rows_out)
