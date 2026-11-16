"""Meshtastic positions source (feature 134, T038).

Fixes are windowed by observation time, relocated with the network (microdegree integers), and
re-keyed to ``node-NN``. The stored address was rendered as a city-grid address at record time —
an event concept (US4-1) — so it is replaced by the relocated plain coordinates. The daemon's
lock/socket/rate-limit files describe the live host and are skipped.
"""

from __future__ import annotations

import json

from obi.core import meshstore
from obi.core.demo import open_source_db
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport, finish_db, new_db

NAME = "mesh"


def run(ctx: SourceContext) -> SourceReport:
    mesh_dir = ctx.state_dir / "meshtastic"
    src = mesh_dir / "positions.db"
    if not src.exists():
        return SourceReport(NAME, ABSENT)
    lo_s, hi_s = ctx.window[0] // 1_000_000, ctx.window[1] // 1_000_000
    conn = open_source_db(src)
    out = new_db(ctx.out_state / "meshtastic" / "positions.db", meshstore.connect)
    rows_in = 0
    try:
        rows = []
        for row in conn.execute(
            "SELECT * FROM position_fix WHERE observed_at_s BETWEEN ? AND ?"
            " ORDER BY observed_at_s, id",
            (lo_s, hi_s),
        ):
            rows_in += 1
            lat, lon = ctx.relocator.point(row["lat_udeg"] / 1e7, row["lon_udeg"] / 1e7)
            fix = int(ctx.shift.s(row["fix_time_s"])) if row["fix_time_s"] else 0
            rows.append(
                (ctx.mapping.node(row["node_id"]), round(lat * 1e7), round(lon * 1e7),
                 row["altitude_m"], fix, int(ctx.shift.s(row["observed_at_s"])),
                 row["hops_away"], row["snr_db"], f"{lat:.5f}, {lon:.5f}")
            )  # fmt: skip
        out.executemany(
            "INSERT OR IGNORE INTO position_fix (node_id, lat_udeg, lon_udeg, altitude_m,"
            " fix_time_s, observed_at_s, hops_away, snr_db, address) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    finally:
        conn.close()
        finish_db(out)
    status = mesh_dir / "daemon-status.json"
    if status.exists():
        rewritten = ctx.rewriter.json(json.loads(status.read_text(encoding="utf-8")))
        (ctx.out_state / "meshtastic" / "daemon-status.json").write_text(
            json.dumps(rewritten, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    ctx.count_excluded("mesh-city-grid-address-replaced", len(rows))
    return SourceReport(NAME, INCLUDED, rows_in=rows_in, rows_out=len(rows))
