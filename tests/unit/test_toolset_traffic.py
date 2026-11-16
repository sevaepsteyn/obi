"""Unit (feature 112): the ``gate_traffic`` chat handler.

The promises under test: the newest Travel Time Log entry is served VERBATIM with its
playa-local published time and age plus up to four earlier entries for trend; stale data
and stale checks stay distinguishable (both ages always render); absence is stated with
the last-check time, never invented around (pre-event 403 included); gate status renders
lanes + advisory verbatim, all-null as "not reported", and a failed source as unknown —
never defaulted; no number is ever derived; the worst-case payload stays inside the radio
budget (≤ 1,600 chars); and a dispatched call lands exactly one feature-042 tool event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from obi.core import auditstore, trafficstore
from obi.core.audit import AuditRecorder, AuditTrail, audit_scope
from obi.core.config import AuditSettings, SiteSettings, TrafficSettings
from obi.core.toolset import ToolContext, _gate_traffic, build_dispatch, build_tools
from obi.core.trafficstore import GateStatus, ParsedEntry
from tests.fakes import chat_config

PLAYA = ZoneInfo("America/Los_Angeles")
US = 1_000_000
MIN_US = 60 * US
HOUR_US = 3600 * US


def _now_us() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000_000)


def _entry(key: str, published_us: int | None, text: str) -> ParsedEntry:
    return ParsedEntry(
        entry_key=key,
        published_us=published_us,
        title="Travel Time Update",
        author="Gate/Perimeter TOC",
        pinned=False,
        text=text,
        raw=f'{{"id": "{key}", "text": "{text}"}}',
    )


def _seed(
    tmp_path: Path,
    *,
    cycles: list[dict[str, object]] | None = None,
    store: bool = True,
) -> ToolContext:
    db_path = tmp_path / "traffic.db"
    if store:
        conn = trafficstore.connect(db_path)
        for cycle in cycles or []:
            fetched_us = cycle["fetched_us"]
            assert isinstance(fetched_us, int)
            entries = cycle.get("entries", [])
            assert isinstance(entries, list)
            trafficstore.insert_cycle(
                conn,
                fetched_us=fetched_us,
                feed_outcome=str(cycle.get("feed_outcome", "new" if entries else "unchanged")),
                feed_error=cycle.get("feed_error"),  # type: ignore[arg-type]
                status_outcome=str(cycle.get("status_outcome", "unchanged")),
                status_error=cycle.get("status_error"),  # type: ignore[arg-type]
                entries=entries,
                gate_status=cycle.get("status"),  # type: ignore[arg-type]
                feed_body=None,
            )
        conn.commit()
        conn.close()
    config = chat_config(
        tmp_path,
        traffic=TrafficSettings(base_url="https://traffic.example.net", db_path=db_path),
        site=SiteSettings(timezone="America/Los_Angeles"),
    )
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        question="how long is the gate wait time?",
    )


# --- US1: travel-time answers -------------------------------------------------------------------


def test_newest_entry_verbatim_with_time_age_and_trend(tmp_path: Path) -> None:
    now = _now_us()
    entries = [
        _entry("a", now - 23 * MIN_US, "Gravel to Gate: 90 min. Gate to Greeters: 25 min."),
        _entry("b", now - 2 * HOUR_US, "Gravel to Gate: 60 min."),
        _entry("c", now - 4 * HOUR_US, "Gravel to Gate: 45 min."),
    ]
    ctx = _seed(tmp_path, cycles=[{"fetched_us": now - 2 * MIN_US, "entries": entries}])
    out = _gate_traffic(ctx, {})
    assert "Gravel to Gate: 90 min. Gate to Greeters: 25 min." in out  # verbatim
    assert "23m ago" in out
    local = datetime.fromtimestamp((now - 23 * MIN_US) / US, tz=PLAYA)
    assert f"{local:%H:%M}" in out  # playa-local published time
    assert "Gate/Perimeter TOC" in out
    # trend: the earlier entries follow, newest first
    assert out.index("90 min") < out.index("60 min") < out.index("45 min")


def test_trend_is_capped_at_five_entries(tmp_path: Path) -> None:
    now = _now_us()
    entries = [
        _entry(f"e{i}", now - i * HOUR_US, f"Gravel to Gate: {i * 10} min report")
        for i in range(1, 8)
    ]
    ctx = _seed(tmp_path, cycles=[{"fetched_us": now, "entries": entries}])
    out = _gate_traffic(ctx, {})
    assert "10 min report" in out and "50 min report" in out
    assert "60 min report" not in out and "70 min report" not in out  # 5 served, rest cut


def test_stale_data_and_fresh_check_read_differently(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now - 3 * HOUR_US,
                "entries": [_entry("a", now - 3 * HOUR_US, "Gravel to Gate: 90 min.")],
            },
            {"fetched_us": now - 2 * MIN_US, "feed_outcome": "unchanged"},
        ],
    )
    out = _gate_traffic(ctx, {})
    assert "3h ago" in out  # the data's age
    assert "Checked 2m ago" in out  # the check's age — a quiet TOC, not a dead uplink
    assert "unchanged" in out


def test_pre_event_refusal_states_absence_with_last_check(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now - 4 * MIN_US,
                "feed_outcome": "refused",
                "feed_error": "http 403",
                "status_outcome": "new",
                "status": GateStatus(None, None, None, '{"inbound":null}'),
            }
        ],
    )
    out = _gate_traffic(ctx, {})
    assert "hasn't published travel times yet" in out
    assert "Checked 4m ago" in out
    assert "90" not in out and "min." not in out  # no invented number anywhere


def test_never_polled_store_names_the_collector(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, store=False)
    out = _gate_traffic(ctx, {})
    assert "obi traffic run" in out


def test_unstamped_entry_is_labeled_first_seen(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now - 10 * MIN_US,
                "entries": [_entry("x", None, "Exodus flowing, no wait.")],
            }
        ],
    )
    out = _gate_traffic(ctx, {})
    assert "first seen" in out
    assert "Exodus flowing, no wait." in out
    assert "10m ago" in out


# --- US2: gate status ---------------------------------------------------------------------------


def test_gate_status_lanes_and_advisory_verbatim(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now - 8 * MIN_US,
                "status_outcome": "new",
                "status": GateStatus("open", "closed", "egress paused for dust", "{}"),
            }
        ],
    )
    out = _gate_traffic(ctx, {})
    assert "inbound open" in out
    assert "outbound closed" in out
    assert "egress paused for dust" in out  # advisory verbatim
    assert "8m ago" in out


def test_all_null_status_is_not_reported_never_defaulted(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now - 3 * MIN_US,
                "status_outcome": "new",
                "status": GateStatus(None, None, None, '{"inbound":null}'),
            }
        ],
    )
    out = _gate_traffic(ctx, {})
    assert "not currently reporting gate status" in out
    assert "open" not in out and "closed" not in out  # absence, never a default


def test_status_source_failure_is_unknown_beside_served_entries(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now - 5 * MIN_US,
                "entries": [_entry("a", now - 5 * MIN_US, "Gravel to Gate: 30 min.")],
                "status_outcome": "failed",
                "status_error": "timed out",
            }
        ],
    )
    out = _gate_traffic(ctx, {})
    assert "Gravel to Gate: 30 min." in out  # partial data served honestly
    assert "unknown" in out and "timed out" in out  # gate status marked unknown-as-of


# --- bounds + audit -----------------------------------------------------------------------------


def test_worst_case_payload_fits_the_radio_budget(tmp_path: Path) -> None:
    now = _now_us()
    entries = [
        _entry(f"long{i}", now - i * HOUR_US, f"Report {i}: " + "congestion detail " * 40)
        for i in range(1, 9)
    ]
    ctx = _seed(
        tmp_path,
        cycles=[
            {
                "fetched_us": now,
                "entries": entries,
                "status_outcome": "new",
                "status": GateStatus("open", "open", "A" * 400, "{}"),
            }
        ],
    )
    out = _gate_traffic(ctx, {})
    assert len(out) <= 1600
    assert "…" in out  # the cap is visible, never a silent truncation


def test_dispatched_call_lands_one_tool_event(tmp_path: Path) -> None:
    now = _now_us()
    ctx = _seed(
        tmp_path,
        cycles=[{"fetched_us": now, "entries": [_entry("a", now, "Gravel to Gate: 20 min.")]}],
    )
    tools = [t for t in build_tools() if t.name == "gate_traffic"]
    assert tools, "gate_traffic must be a registered tool"
    dispatch = build_dispatch(ctx, tools)
    recorder = AuditRecorder(
        AuditSettings(
            enabled=True, db_path=tmp_path / "audit.db", retention_days=90, max_payload_bytes=5000
        )
    )
    with audit_scope(recorder, "chat", "chat-test"):
        dispatch["gate_traffic"]({})
    recorder.close()
    conn = auditstore.connect_readonly(tmp_path / "audit.db")
    try:
        rows, _ = auditstore.query_events(conn, limit=100, include_payload=True)
    finally:
        conn.close()
    tool_events = [r for r in rows if r["kind"] == "tool_call"]
    assert len(tool_events) == 1
    assert "gate_traffic" in str(tool_events[0])
