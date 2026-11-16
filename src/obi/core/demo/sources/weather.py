"""weather.db source (feature 134, T035): numeric snapshots + hourly rows; alerts excluded.

NWS alert headlines name real counties and zones, so the ``alerts`` table ships empty (the
readers already handle "no active alerts"). Snapshot ids are renumbered and the hourly rows
re-pointed; every timestamp is shifted.
"""

from __future__ import annotations

from obi.core import weatherstore
from obi.core.demo import open_source_db
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport, finish_db, new_db

NAME = "weather"
_SNAPSHOT_COLS = (
    "forecast_ok", "forecast_error", "alerts_ok", "alerts_error", "temp_f", "humidity_pct",
    "dew_point_f", "precip_in", "wind_mph", "wind_dir_deg", "gust_mph",
)  # fmt: skip
_HOURLY_COLS = (
    "temp_f", "humidity_pct", "dew_point_f", "precip_in", "precip_prob_pct", "wind_mph",
    "wind_dir_deg", "gust_mph",
)  # fmt: skip


def run(ctx: SourceContext) -> SourceReport:
    src = ctx.state_dir / "weather.db"
    if not src.exists():
        return SourceReport(NAME, ABSENT)
    rw, shift = ctx.rewriter, ctx.shift
    conn = open_source_db(src)
    out = new_db(ctx.out_state / "weather.db", weatherstore.connect)
    rows_in = rows_out = 0
    try:
        new_ids: dict[int, int] = {}
        for row in conn.execute(
            "SELECT * FROM snapshots WHERE fetched_us BETWEEN ? AND ? ORDER BY fetched_us, id",
            ctx.window,
        ):
            rows_in += 1
            values = [row[c] for c in _SNAPSHOT_COLS]
            for i, col in enumerate(_SNAPSHOT_COLS):
                if col.endswith("_error") and values[i]:
                    values[i] = rw.text(str(values[i]))
            cur = out.execute(
                f"INSERT INTO snapshots (fetched_us, {', '.join(_SNAPSHOT_COLS)})"
                f" VALUES (?{', ?' * len(_SNAPSHOT_COLS)})",
                (shift.us(row["fetched_us"]), *values),
            )
            new_ids[row["id"]] = int(cur.lastrowid or 0)
            rows_out += 1
        for old, new in new_ids.items():
            for hour in conn.execute(
                "SELECT * FROM hourly WHERE snapshot_id = ? ORDER BY hour_us", (old,)
            ):
                out.execute(
                    f"INSERT INTO hourly (snapshot_id, hour_us, {', '.join(_HOURLY_COLS)})"
                    f" VALUES (?, ?{', ?' * len(_HOURLY_COLS)})",
                    (new, shift.us(hour["hour_us"]), *(hour[c] for c in _HOURLY_COLS)),
                )
        alerts = conn.execute(
            "SELECT count(*) FROM alerts WHERE snapshot_id IN"
            " (SELECT id FROM snapshots WHERE fetched_us BETWEEN ? AND ?)",
            ctx.window,
        ).fetchone()[0]
        ctx.count_excluded("weather-alert-names-real-places", int(alerts))
    finally:
        conn.close()
        finish_db(out)
    return SourceReport(NAME, INCLUDED, rows_in=rows_in, rows_out=rows_out)
