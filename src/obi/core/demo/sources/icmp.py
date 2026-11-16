"""ICMP reachability source (feature 134, T032): ``icmp.db`` device state + UISP target list."""

from __future__ import annotations

import json
from typing import Any

from obi.core import icmpstore
from obi.core.demo import open_source_db
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport, finish_db, new_db

NAME = "icmp"


def run(ctx: SourceContext) -> SourceReport:
    src = ctx.state_dir / "icmp.db"
    targets = ctx.state_dir / "icmp-uisp-targets.json"
    if not src.exists() and not targets.exists():
        return SourceReport(NAME, ABSENT)
    rw, shift = ctx.rewriter, ctx.shift
    rows_in = rows_out = 0
    if src.exists():
        conn = open_source_db(src)
        out = new_db(ctx.out_state / "icmp.db", icmpstore.connect)
        try:
            rows: list[tuple[Any, ...]] = []
            for row in conn.execute("SELECT * FROM device_state ORDER BY device"):
                rows_in += 1
                rec = {key: row[key] for key in row.keys()}  # noqa: SIM118 - sqlite3.Row
                rows.append(
                    (rw.ident(rec["device"]), rw.ident(rec["address"]), rec["up"],
                     rec["consecutive_misses"],
                     shift.us(rec["last_seen_us"]) if rec["last_seen_us"] else None,
                     shift.us(rec["last_change_us"]), shift.us(rec["updated_us"]),
                     rec.get("last_rtt_us"))
                )  # fmt: skip
            rows.sort(key=_device_key)
            out.executemany(
                "INSERT OR REPLACE INTO device_state (device, address, up, consecutive_misses,"
                " last_seen_us, last_change_us, updated_us, last_rtt_us) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
            rows_out = len(rows)
        finally:
            conn.close()
            finish_db(out)
    if targets.exists():
        data = json.loads(targets.read_text(encoding="utf-8"))
        rewritten = rw.json(data)
        (ctx.out_state / "icmp-uisp-targets.json").write_text(
            json.dumps(rewritten, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return SourceReport(NAME, INCLUDED, rows_in=rows_in, rows_out=rows_out)


def _device_key(row: tuple[Any, ...]) -> str:
    return str(row[0])
