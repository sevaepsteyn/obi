"""metrics.db source (feature 134, T031): window, decimate, rewrite identity columns.

Every Nth sample per (device, metric, ifindex) series is kept in timestamp order (research R7):
counter rates stay exact because readers compute them from deltas. Device-health profiles share
this table and come along unchanged in shape.
"""

from __future__ import annotations

from obi.core import metricstore
from obi.core.demo import open_source_db
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport, finish_db, new_db

NAME = "metrics"
_BATCH = 10000


def run(ctx: SourceContext) -> SourceReport:
    src = ctx.state_dir / "metrics.db"
    if not src.exists():
        return SourceReport(NAME, ABSENT)
    rw, shift = ctx.rewriter, ctx.shift
    every = ctx.profile.metrics_decimate
    conn = open_source_db(src)
    out = new_db(ctx.out_state / "metrics.db", metricstore.connect)
    seen: dict[tuple[str, str, str], int] = {}
    names: dict[str, str] = {}
    rows_in = rows_out = 0
    batch: list[tuple[object, ...]] = []

    def ident(value: str) -> str:
        cached = names.get(value)
        if cached is None:
            cached = names[value] = rw.ident(value)
        return cached

    try:
        for row in conn.execute(
            "SELECT ts_us, ts_iso, device, address, profile, metric, ifindex, ifname, kind, value"
            " FROM samples WHERE ts_us BETWEEN ? AND ? ORDER BY ts_us, id",
            ctx.window,
        ):
            rows_in += 1
            series = (row["device"], row["metric"], row["ifindex"])
            n = seen.get(series, 0)
            seen[series] = n + 1
            if n % every:
                continue
            batch.append(
                (shift.us(row["ts_us"]), shift.iso(row["ts_iso"]), ident(row["device"]),
                 ident(row["address"]) if row["address"] else row["address"],
                 row["profile"], row["metric"], row["ifindex"],
                 ident(row["ifname"]) if row["ifname"] else row["ifname"], row["kind"],
                 row["value"])
            )  # fmt: skip
            if len(batch) >= _BATCH:
                out.executemany(_INSERT, batch)
                rows_out += len(batch)
                batch.clear()
        if batch:
            out.executemany(_INSERT, batch)
            rows_out += len(batch)
    finally:
        conn.close()
        finish_db(out)
    ctx.count_excluded("metric-sample-decimated", rows_in - rows_out)
    return SourceReport(NAME, INCLUDED, rows_in=rows_in, rows_out=rows_out)


_INSERT = (
    "INSERT OR IGNORE INTO samples (ts_us, ts_iso, device, address, profile, metric, ifindex,"
    " ifname, kind, value) VALUES (?,?,?,?,?,?,?,?,?,?)"
)
