"""Unit (feature 112): the gate-traffic SQLite store.

Pins the data-model.md contract: every cycle inserts a poll row (failure is evidence),
entries dedup by key across re-served logs, gate-status rows land only on change, raw
payloads only on `new`, prune cascades per-poll children while `fetch_state` and young
entries survive, and the read-only connection never creates a store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core import trafficstore
from obi.core.trafficstore import GateStatus, ParsedEntry

US = 1_000_000
T0 = 1_700_000_000 * US


def _entry(key: str, published_us: int | None, text: str = "Gravel to Gate: 90 min") -> (
    ParsedEntry
):
    return ParsedEntry(
        entry_key=key,
        published_us=published_us,
        title="Travel Time Update",
        author="Gate/Perimeter TOC",
        pinned=False,
        text=text,
        raw=f'{{"id": "{key}"}}',
    )


def _cycle(
    conn: trafficstore.Connection,
    *,
    now_us: int,
    entries: list[ParsedEntry] | None = None,
    status: GateStatus | None = None,
    feed_outcome: str = "new",
    feed_error: str | None = None,
    status_outcome: str = "new",
    status_error: str | None = None,
    feed_body: str | None = '[{"id": "x"}]',
) -> int:
    poll_id = trafficstore.insert_cycle(
        conn,
        fetched_us=now_us,
        feed_outcome=feed_outcome,
        feed_error=feed_error,
        status_outcome=status_outcome,
        status_error=status_error,
        entries=entries or [],
        gate_status=status,
        feed_body=feed_body if feed_outcome == "new" else None,
    )
    conn.commit()
    return poll_id


def test_roundtrip_entries_and_status(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    status = GateStatus(
        inbound_state="open",
        outbound_state="closed",
        advisory="dust hold on egress",
        raw='{"inbound":"open"}',
    )
    _cycle(
        conn,
        now_us=T0,
        entries=[_entry("a", T0 - 3600 * US), _entry("b", T0 - 60 * US, "60 min")],
        status=status,
    )

    read = trafficstore.connect_readonly(tmp_path / "traffic.db")
    entries = trafficstore.recent_entries(read, limit=5)
    assert [e.text for e in entries] == ["60 min", "Gravel to Gate: 90 min"]  # newest first
    assert entries[0].published_us == T0 - 60 * US
    assert entries[0].raw == '{"id": "b"}'
    newest_status = trafficstore.newest_gate_status(read)
    assert newest_status is not None
    assert newest_status.inbound_state == "open"
    assert newest_status.outbound_state == "closed"
    assert newest_status.advisory == "dust hold on egress"
    assert newest_status.captured_us == T0
    poll = trafficstore.newest_poll(read)
    assert poll is not None
    assert (poll.feed_outcome, poll.status_outcome) == ("new", "new")
    assert trafficstore.entry_count(read) == 2


def test_every_cycle_records_a_poll_even_on_total_failure(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    _cycle(
        conn,
        now_us=T0,
        feed_outcome="refused",
        feed_error="http 403",
        status_outcome="failed",
        status_error="timed out",
        feed_body=None,
    )
    poll = trafficstore.newest_poll(trafficstore.connect_readonly(tmp_path / "traffic.db"))
    assert poll is not None
    assert poll.feed_outcome == "refused"
    assert poll.feed_error == "http 403"
    assert poll.status_outcome == "failed"
    assert poll.status_error == "timed out"


def test_entries_dedup_on_reserved_log(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    _cycle(conn, now_us=T0, entries=[_entry("a", T0 - 3600 * US)])
    # the feed re-serves the whole log next poll, plus one new entry
    _cycle(
        conn,
        now_us=T0 + 300 * US,
        entries=[_entry("a", T0 - 3600 * US), _entry("c", T0 + 200 * US, "45 min")],
    )
    read = trafficstore.connect_readonly(tmp_path / "traffic.db")
    assert trafficstore.entry_count(read) == 2  # "a" not duplicated
    entries = trafficstore.recent_entries(read, limit=5)
    assert entries[0].entry_key == "c"
    # the surviving "a" row keeps its ORIGINAL first_seen poll
    assert entries[1].first_seen_us == T0


def test_unstamped_entry_orders_by_first_seen(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    _cycle(conn, now_us=T0, entries=[_entry("old", T0 - 7200 * US)])
    _cycle(conn, now_us=T0 + 300 * US, entries=[_entry("nostamp", None, "free text")])
    read = trafficstore.connect_readonly(tmp_path / "traffic.db")
    entries = trafficstore.recent_entries(read, limit=5)
    assert entries[0].entry_key == "nostamp"  # COALESCE(published, first_seen) ordering
    assert entries[0].published_us is None
    assert entries[0].first_seen_us == T0 + 300 * US


def test_gate_status_rows_only_on_change(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    status = GateStatus("open", "open", None, '{"inbound":"open","outbound":"open"}')
    _cycle(conn, now_us=T0, status=status)
    # unchanged next poll: caller records status_outcome="unchanged", passes no row
    _cycle(conn, now_us=T0 + 300 * US, status=None, status_outcome="unchanged")
    read = trafficstore.connect_readonly(tmp_path / "traffic.db")
    rows = read.execute("SELECT COUNT(*) FROM gate_statuses").fetchone()
    assert rows[0] == 1
    newest = trafficstore.newest_gate_status(read)
    assert newest is not None and newest.captured_us == T0


def test_raw_payload_only_on_new(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    _cycle(conn, now_us=T0, entries=[_entry("a", T0)], feed_body='[{"id":"a"}]')
    _cycle(conn, now_us=T0 + 300 * US, feed_outcome="unchanged", feed_body=None)
    read = trafficstore.connect_readonly(tmp_path / "traffic.db")
    assert read.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1


def test_fetch_state_upserts_and_survives_prune(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    trafficstore.set_fetch_state(conn, source="feed", etag='W/"abc"', body_sha256="d0d0")
    trafficstore.set_fetch_state(conn, source="feed", etag='W/"def"', body_sha256="beef")
    conn.commit()
    state = trafficstore.get_fetch_state(conn, source="feed")
    assert state is not None
    assert state.etag == 'W/"def"' and state.body_sha256 == "beef"
    assert trafficstore.get_fetch_state(conn, source="status") is None
    trafficstore.prune(conn, older_than_us=T0 + 10**12)
    conn.commit()
    assert trafficstore.get_fetch_state(conn, source="feed") is not None


def test_prune_cascades_children_and_old_entries(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    old_status = GateStatus("closed", None, None, '{"inbound":"closed"}')
    new_status = GateStatus("open", None, None, '{"inbound":"open"}')
    _cycle(conn, now_us=T0 - 500 * US, status=old_status, feed_outcome="refused",
           feed_error="http 403", feed_body=None)
    _cycle(conn, now_us=T0, entries=[_entry("old", T0 - 10 * US)], status=new_status)
    _cycle(conn, now_us=T0 + 400 * US, entries=[_entry("new", T0 + 350 * US)])
    removed = trafficstore.prune(conn, older_than_us=T0 + 100 * US)
    conn.commit()
    assert removed == 2
    read = trafficstore.connect_readonly(tmp_path / "traffic.db")
    assert read.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 1
    # superseded status rows prune with their polls; the NEWEST status always survives
    # (last-known-with-age keeps serving through a long-unchanged stretch)
    assert read.execute("SELECT COUNT(*) FROM gate_statuses").fetchone()[0] == 1
    survivor = trafficstore.newest_gate_status(read)
    assert survivor is not None and survivor.inbound_state == "open"
    assert read.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1
    assert trafficstore.entry_count(read) == 1
    assert trafficstore.recent_entries(read, limit=5)[0].entry_key == "new"


def test_coverage_and_empty_store(tmp_path: Path) -> None:
    conn = trafficstore.connect(tmp_path / "traffic.db")
    assert trafficstore.coverage(conn) is None
    assert trafficstore.newest_poll(conn) is None
    assert trafficstore.newest_gate_status(conn) is None
    assert trafficstore.recent_entries(conn, limit=5) == []
    _cycle(conn, now_us=T0)
    _cycle(conn, now_us=T0 + 300 * US, feed_outcome="unchanged", feed_body=None)
    assert trafficstore.coverage(conn) == (T0, T0 + 300 * US)


def test_readonly_never_creates(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    with pytest.raises(trafficstore.StoreError):
        trafficstore.connect_readonly(missing).execute("SELECT 1")
    assert not missing.exists()
