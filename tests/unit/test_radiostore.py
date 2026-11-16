"""Unit (feature 088): the radio transcript store (transmissions / calls / outbound).

Covers schema creation via storebase, record/query round-trips, the monotonic tail cursor,
FTS search, the transmitted-outbound ⟺ kind='obi' transmissions-row invariant (one
transaction), and prune (no-op at retention_days=0 semantics; clip unlinking is the
caller's job — prune returns the clip paths of pruned rows).
"""

from __future__ import annotations

from datetime import UTC, timedelta, timezone
from pathlib import Path

from obi.core import radiostore
from obi.core.radiostore import OutboundRecord, TransmissionRecord


def _rx(started_us: int, transcript: str, **kw: object) -> TransmissionRecord:
    defaults: dict[str, object] = {
        "started_us": started_us,
        "ended_us": started_us + 2_400_000,
        "kind": "rx",
        "transcript": transcript,
        "stt_gate": "ok",
        "peak_dbfs": -8.1,
        "rms_dbfs": -27.0,
    }
    defaults.update(kw)
    return TransmissionRecord(**defaults)  # type: ignore[arg-type]


def test_schema_created_and_stamped(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 1
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"transmissions", "calls", "outbound"} <= tables
    conn.close()


def test_record_and_query_round_trip(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    tx_id = radiostore.record_transmission(
        conn, _rx(1_000_000, "obi obi this is nono", caller="nono"), tz=UTC
    )
    assert tx_id >= 1
    rows = radiostore.query_window(conn, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == tx_id
    assert row.kind == "rx"
    assert row.transcript == "obi obi this is nono"
    assert row.caller == "nono"
    assert row.duration_ms == 2400
    assert row.stt_gate == "ok"
    assert row.ts_iso.startswith("1970-01-01")
    conn.close()


def test_ts_iso_localized_at_write_time(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    pdt = timezone(timedelta(hours=-7))
    radiostore.record_transmission(conn, _rx(0, "hello"), tz=pdt)
    row = radiostore.query_window(conn, limit=1)[0]
    assert row.ts_iso == "1969-12-31 17:00:00"  # rendered through the display tz, 069 rule
    conn.close()


def test_monotonic_cursor_and_query_since(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    assert radiostore.latest_cursor(conn) is None
    first = radiostore.record_transmission(conn, _rx(1_000_000, "one"), tz=UTC)
    second = radiostore.record_transmission(conn, _rx(2_000_000, "two"), tz=UTC)
    assert radiostore.latest_cursor(conn) == second
    tail = radiostore.query_since(conn, after_id=first, limit=10)
    assert [r.transcript for r in tail] == ["two"]
    assert radiostore.query_since(conn, after_id=second, limit=10) == []
    conn.close()


def test_self_rows_excluded_unless_asked(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    radiostore.record_transmission(conn, _rx(1_000_000, "someone"), tz=UTC)
    radiostore.record_transmission(conn, _rx(2_000_000, "our own echo", kind="self"), tz=UTC)
    assert [r.kind for r in radiostore.query_window(conn, limit=10)] == ["rx"]
    both = radiostore.query_window(conn, limit=10, include_self=True)
    assert sorted(r.kind for r in both) == ["rx", "self"]
    only_self = radiostore.query_window(conn, limit=10, kind="self")
    assert [r.kind for r in only_self] == ["self"]
    conn.close()


def test_fts_search_matches_literally(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    radiostore.record_transmission(conn, _rx(1_000_000, "the mountain link is down"), tz=UTC)
    radiostore.record_transmission(conn, _rx(2_000_000, "radio check"), tz=UTC)
    hits = radiostore.query_window(conn, limit=10, contains="mountain")
    assert len(hits) == 1 and "mountain" in hits[0].transcript
    # Operator text is quoted as a phrase, never parsed as FTS syntax: a stray quote is
    # neutralized (still matches — the tokenizer ignores punctuation) instead of erroring,
    # and phrase semantics hold (reversed word order does not match).
    assert len(radiostore.query_window(conn, limit=10, contains='mountain "link')) == 1
    assert radiostore.query_window(conn, limit=10, contains="link mountain") == []
    conn.close()


def test_calls_lifecycle(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller="nono", tier="safe")
    assert call_id >= 1
    radiostore.update_call(conn, call_id, state="composing", say_again_used=True)
    radiostore.close_call(conn, call_id, closed_us=9_000_000, closure="cleared")
    row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    assert row is not None
    conn.close()


def test_transmitted_outbound_creates_obi_transmission_atomically(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller="nono", tier="safe")
    outbound_id, tx_id = radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=2_000_000,
            call_id=call_id,
            kind="ack",
            text="go for obi",
            render_ms=900,
            disposition="transmitted",
            gate_log='{"clear_checks": 1}',
            tx_started_us=2_100_000,
            tx_ended_us=3_000_000,
        ),
        tz=UTC,
    )
    assert outbound_id >= 1 and tx_id is not None
    rows = radiostore.query_window(conn, limit=10)
    assert len(rows) == 1
    obi_row = rows[0]
    assert obi_row.id == tx_id
    assert obi_row.kind == "obi"
    assert obi_row.transcript == "go for obi"
    assert obi_row.call_id == call_id
    # The join surfaces the outbound metadata on the obi row (web API contract).
    assert obi_row.outbound_kind == "ack"
    assert obi_row.disposition == "transmitted"
    conn.close()


def test_shadow_outbound_creates_no_transmission(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller=None, tier="safe")
    outbound_id, tx_id = radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=2_000_000,
            call_id=call_id,
            kind="answer",
            text="mountain link is up, over",
            render_ms=2500,
            disposition="shadow_dmed",
            gate_log=None,
        ),
        tz=UTC,
    )
    assert outbound_id >= 1 and tx_id is None
    assert radiostore.query_window(conn, limit=10) == []
    conn.close()


def test_prune_returns_clip_paths_and_removes_fts(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    radiostore.record_transmission(
        conn, _rx(1_000_000, "old traffic", clip_path="1970/01/01/1.wav"), tz=UTC
    )
    keep = radiostore.record_transmission(conn, _rx(5_000_000, "new traffic"), tz=UTC)
    clips = radiostore.prune(conn, older_than_us=3_000_000)
    assert clips == ["1970/01/01/1.wav"]
    rows = radiostore.query_window(conn, limit=10)
    assert [r.id for r in rows] == [keep]
    assert radiostore.query_window(conn, limit=10, contains="old") == []
    conn.close()


def test_readonly_connection_reads(tmp_path: Path) -> None:
    db = tmp_path / "radio.db"
    conn = radiostore.connect(db)
    radiostore.record_transmission(conn, _rx(1_000_000, "hello"), tz=UTC)
    conn.close()
    ro = radiostore.connect_readonly(db)
    assert len(radiostore.query_window(ro, limit=10)) == 1
    ro.close()


# --- v2 migration: speaker attribution columns (088 follow-up, 2026-08-20) ------------------


def test_v1_store_migrates_to_v2_preserving_rows(tmp_path: Path) -> None:
    import sqlite3

    # Build a genuine v1 database by hand (the frozen base schema, stamped v1), with one
    # pre-migration row — exactly what the live store looks like before the deploy.
    db = tmp_path / "radio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(radiostore._SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO transmissions (started_us, ended_us, ts_iso, duration_ms, kind,"
        " transcript, stt_gate) VALUES (1, 2, '1970-01-01 00:00:00', 1, 'rx', 'old row', 'ok')"
    )
    conn.commit()
    conn.close()

    conn = radiostore.connect(db)  # the daemon s RW open runs the migrations
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    rows = radiostore.query_window(conn, limit=10)
    assert len(rows) == 1
    assert rows[0].transcript == "old row"
    assert rows[0].speaker is None and rows[0].speaker_confidence is None
    conn.close()


def test_speaker_fields_round_trip(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    radiostore.record_transmission(
        conn,
        _rx(1_000_000, "waterworks go for it", speaker="it", speaker_confidence="high"),
        tz=UTC,
    )
    radiostore.record_transmission(conn, _rx(2_000_000, "unattributed"), tz=UTC)
    rows = radiostore.query_window(conn, limit=10)
    assert (rows[0].speaker, rows[0].speaker_confidence) == ("it", "high")
    assert (rows[1].speaker, rows[1].speaker_confidence) == (None, None)
    conn.close()
    ro = radiostore.connect_readonly(tmp_path / "radio.db")
    assert radiostore.query_window(ro, limit=10)[0].speaker == "it"
    ro.close()


def test_v3_accepts_the_standby_outbound_kind(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller="mira", tier="safe")
    outbound_id, tx_id = radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=2_000_000,
            call_id=call_id,
            kind="standby",
            text="stand by",
            disposition="transmitted",
            render_ms=800,
            tx_started_us=2_000_000,
            tx_ended_us=3_000_000,
        ),
        tz=UTC,
    )
    assert outbound_id >= 1 and tx_id is not None


def test_v2_outbound_rows_survive_the_v3_rebuild(tmp_path: Path) -> None:
    import sqlite3

    # A v2 store with an outbound row (the live store's shape before this deploy).
    db = tmp_path / "radio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(radiostore._SCHEMA)
    conn.executescript(radiostore._MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "INSERT INTO calls (opened_us, caller, tier, state) VALUES (1, 'mira', 'safe', 'open')"
    )
    conn.execute(
        "INSERT INTO outbound (created_us, ts_iso, call_id, kind, text, disposition)"
        " VALUES (1, '1970-01-01 00:00:00', 1, 'ack', 'go for obi', 'transmitted')"
    )
    conn.commit()
    conn.close()

    conn = radiostore.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    rows = conn.execute("SELECT kind, text FROM outbound").fetchall()
    assert rows == [("ack", "go for obi")]
    conn.close()


# --- feature 092: operator transmit requests + the 'operator' outbound kind ---------------


def test_v4_accepts_the_operator_outbound_kind_with_no_call(tmp_path: Path) -> None:
    """An operator test transmission has no call behind it (contract tx-request Q1/Q9).

    The v3 rebuild made ``outbound.call_id`` NOT NULL, which would reject exactly this row,
    so v4 widens the column as well as the kind CHECK.
    """

    conn = radiostore.connect(tmp_path / "radio.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    outbound_id, tx_id = radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=2_000_000,
            call_id=None,
            kind="operator",
            text="radio check, this is obi",
            disposition="transmitted",
            render_ms=900,
            tx_started_us=2_000_000,
            tx_ended_us=3_000_000,
        ),
        tz=UTC,
    )
    assert outbound_id >= 1 and tx_id is not None
    row = conn.execute(
        "SELECT kind, call_id FROM outbound WHERE id = ?", (outbound_id,)
    ).fetchone()
    assert row[0] == "operator" and row[1] is None
    # The paired kind='obi' transmissions row still exists (the 088 atomicity invariant).
    tx = conn.execute("SELECT kind, call_id FROM transmissions WHERE id = ?", (tx_id,)).fetchone()
    assert tx[0] == "obi" and tx[1] is None


def test_v3_outbound_rows_survive_the_v4_rebuild(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller="mira", tier="safe")
    radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=2_000_000,
            call_id=call_id,
            kind="answer",
            text="the mountain link is up",
            disposition="transmitted",
            tx_started_us=2_000_000,
            tx_ended_us=3_000_000,
        ),
        tz=UTC,
    )
    conn.close()
    # Reopening runs migrations again (idempotent) and must not lose the row.
    reopened = radiostore.connect(tmp_path / "radio.db")
    kept = reopened.execute("SELECT kind, text FROM outbound").fetchall()
    assert kept == [("answer", "the mountain link is up")]


def test_unknown_outbound_kind_still_rejected(tmp_path: Path) -> None:
    import pytest

    conn = radiostore.connect(tmp_path / "radio.db")
    with pytest.raises(ValueError):
        radiostore.record_outbound(
            conn,
            OutboundRecord(
                created_us=1, call_id=None, kind="freestyle", text="x", disposition="transmitted"
            ),
            tz=UTC,
        )


# --- feature 093: the 'garble_close' outbound kind (the on-air garble sign-off) -------------


def test_v5_accepts_the_garble_close_outbound_kind(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller="mira", tier="safe")
    outbound_id, tx_id = radiostore.record_outbound(
        conn,
        OutboundRecord(
            created_us=2_000_000,
            call_id=call_id,
            kind="garble_close",
            text="unable to copy, out",
            disposition="transmitted",
            render_ms=800,
            tx_started_us=2_000_000,
            tx_ended_us=3_000_000,
        ),
        tz=UTC,
    )
    assert outbound_id >= 1 and tx_id is not None
    row = conn.execute("SELECT kind, call_id FROM outbound WHERE id = ?", (outbound_id,)).fetchone()
    assert row[0] == "garble_close" and row[1] == call_id


def test_v4_outbound_rows_survive_the_v5_rebuild(tmp_path: Path) -> None:
    import sqlite3

    # A v4 store with an outbound row — the live store's shape before this deploy.
    db = tmp_path / "radio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(radiostore._SCHEMA)
    for migration in radiostore._MIGRATIONS[:3]:
        conn.executescript(migration)
    conn.execute("PRAGMA user_version = 4")
    conn.execute(
        "INSERT INTO calls (opened_us, caller, tier, state) VALUES (1, 'mira', 'safe', 'open')"
    )
    conn.execute(
        "INSERT INTO outbound (created_us, ts_iso, call_id, kind, text, disposition)"
        " VALUES (1, '1970-01-01 00:00:00', 1, 'operator', 'radio check', 'transmitted')"
    )
    conn.commit()
    conn.close()

    conn = radiostore.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5  # 124 moved the head to 6
    rows = conn.execute("SELECT kind, text FROM outbound").fetchall()
    assert rows == [("operator", "radio check")]
    # call_id stayed nullable (the v4 widening) and the index came back with the rebuild.
    indexes = {r[1] for r in conn.execute("PRAGMA index_list('outbound')").fetchall()}
    assert "idx_radio_outbound_call" in indexes
    conn.close()


def test_tx_request_enqueue_lands_pending(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = radiostore.enqueue_tx_request(
        conn, created_us=1_000_000, text="radio check", ttl_s=120, tz=UTC
    )
    row = radiostore.get_tx_request(conn, req_id)
    assert row is not None
    assert row.state == "pending"
    assert row.text == "radio check"
    assert row.origin == "cli"
    assert row.expires_us == 1_000_000 + 120 * 1_000_000
    assert row.disposition is None


def test_tx_request_claim_is_atomic_and_single_winner(tmp_path: Path) -> None:
    """Q4 — a request is attempted at most once, even if two claimers race."""

    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = radiostore.enqueue_tx_request(
        conn, created_us=1_000_000, text="hello", ttl_s=120, tz=UTC
    )
    first = radiostore.claim_tx_request(conn, req_id, now_us=1_500_000)
    second = radiostore.claim_tx_request(conn, req_id, now_us=1_500_000)
    assert first is True
    assert second is False
    assert radiostore.get_tx_request(conn, req_id).state == "claimed"  # type: ignore[union-attr]


def test_tx_request_expires_at_claim_time(tmp_path: Path) -> None:
    """Q5 — a request that sat through a daemon outage must never air late."""

    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = radiostore.enqueue_tx_request(
        conn, created_us=1_000_000, text="stale", ttl_s=10, tz=UTC
    )
    claimed = radiostore.claim_tx_request(conn, req_id, now_us=1_000_000 + 11 * 1_000_000)
    assert claimed is False
    row = radiostore.get_tx_request(conn, req_id)
    assert row is not None and row.state == "expired"


def test_tx_request_settle_records_the_outcome(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = radiostore.enqueue_tx_request(
        conn, created_us=1_000_000, text="hello", ttl_s=120, tz=UTC
    )
    radiostore.claim_tx_request(conn, req_id, now_us=1_500_000)
    radiostore.settle_tx_request(
        conn,
        req_id,
        settled_us=2_000_000,
        disposition="transmitted",
        detail="keyed 1200 ms",
        outbound_id=7,
    )
    row = radiostore.get_tx_request(conn, req_id)
    assert row is not None
    assert row.state == "done"
    assert row.disposition == "transmitted"
    assert row.detail == "keyed 1200 ms"
    assert row.outbound_id == 7


def test_tx_request_terminal_states_are_one_way(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = radiostore.enqueue_tx_request(
        conn, created_us=1_000_000, text="hello", ttl_s=120, tz=UTC
    )
    radiostore.claim_tx_request(conn, req_id, now_us=1_500_000)
    radiostore.settle_tx_request(
        conn, req_id, settled_us=2_000_000, disposition="transmitted", detail=None, outbound_id=1
    )
    # A settled request cannot be re-claimed and re-aired.
    assert radiostore.claim_tx_request(conn, req_id, now_us=2_500_000) is False
    assert radiostore.get_tx_request(conn, req_id).state == "done"  # type: ignore[union-attr]


def test_pending_tx_requests_returns_oldest_first(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    second = radiostore.enqueue_tx_request(
        conn, created_us=2_000_000, text="second", ttl_s=120, tz=UTC
    )
    first = radiostore.enqueue_tx_request(
        conn, created_us=1_000_000, text="first", ttl_s=120, tz=UTC
    )
    ids = [r.id for r in radiostore.pending_tx_requests(conn)]
    assert ids == [first, second]


def test_prune_covers_tx_requests(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    radiostore.enqueue_tx_request(conn, created_us=1_000_000, text="old", ttl_s=120, tz=UTC)
    radiostore.prune(conn, older_than_us=5_000_000)
    assert conn.execute("SELECT COUNT(*) FROM tx_requests").fetchone()[0] == 0


# --- feature 124: outbound calls — kinds hail/delivery, calls.direction, call_requests ------


def test_v6_accepts_the_outbound_call_kinds_and_direction(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    call_id = radiostore.open_call(
        conn, opened_us=1_000_000, caller="dana", tier="safe", direction="outbound"
    )
    for kind, text in (("hail", "Dana, Dana, obi"), ("delivery", "the link is up, over")):
        outbound_id, _tx = radiostore.record_outbound(
            conn,
            OutboundRecord(
                created_us=2_000_000,
                call_id=call_id,
                kind=kind,
                text=text,
                disposition="shadow_logged",
            ),
            tz=UTC,
        )
        assert outbound_id >= 1
    row = conn.execute("SELECT direction FROM calls WHERE id = ?", (call_id,)).fetchone()
    assert row[0] == "outbound"
    conn.close()


def test_v6_direction_defaults_inbound_for_existing_callers(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    call_id = radiostore.open_call(conn, opened_us=1_000_000, caller="mira", tier="safe")
    row = conn.execute("SELECT direction FROM calls WHERE id = ?", (call_id,)).fetchone()
    assert row[0] == "inbound"
    conn.close()


def test_v5_rows_survive_the_v6_rebuild(tmp_path: Path) -> None:
    import sqlite3

    # A v5 store with calls + outbound rows — the live store's shape before this deploy.
    db = tmp_path / "radio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(radiostore._SCHEMA)
    for migration in radiostore._MIGRATIONS[:4]:
        conn.executescript(migration)
    conn.execute("PRAGMA user_version = 5")
    conn.execute(
        "INSERT INTO calls (opened_us, caller, tier, state) VALUES (1, 'mira', 'safe', 'open')"
    )
    conn.execute(
        "INSERT INTO outbound (created_us, ts_iso, call_id, kind, text, disposition)"
        " VALUES (1, '1970-01-01 00:00:00', 1, 'garble_close', 'unable to copy, out',"
        " 'transmitted')"
    )
    conn.commit()
    conn.close()

    conn = radiostore.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    rows = conn.execute("SELECT kind, text FROM outbound").fetchall()
    assert rows == [("garble_close", "unable to copy, out")]
    # Pre-124 calls read as inbound; the outbound index survived the rebuild.
    assert conn.execute("SELECT direction FROM calls").fetchone()[0] == "inbound"
    indexes = {r[1] for r in conn.execute("PRAGMA index_list('outbound')").fetchall()}
    assert "idx_radio_outbound_call" in indexes
    conn.close()


def _call_req(conn: object, created_us: int = 1_000_000, ttl_s: int = 600) -> int:
    return radiostore.enqueue_call_request(
        conn,  # type: ignore[arg-type]
        created_us=created_us,
        callee="Dana",
        message="the link is back up, over",
        ttl_s=ttl_s,
        tz=UTC,
    )


def test_call_request_enqueue_lands_pending(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = _call_req(conn)
    row = radiostore.get_call_request(conn, req_id)
    assert row is not None
    assert row.state == "pending"
    assert row.callee == "Dana"
    assert row.message == "the link is back up, over"
    assert row.origin == "cli"
    assert row.attempts == 0
    assert row.expires_us == 1_000_000 + 600 * 1_000_000
    assert row.outcome is None
    conn.close()


def test_call_request_claim_is_atomic_and_single_winner(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = _call_req(conn)
    assert radiostore.claim_call_request(conn, req_id, now_us=1_500_000) is True
    assert radiostore.claim_call_request(conn, req_id, now_us=1_500_000) is False
    assert radiostore.get_call_request(conn, req_id).state == "claimed"  # type: ignore[union-attr]
    conn.close()


def test_call_request_expires_at_claim_time(tmp_path: Path) -> None:
    """O5 — a request that sat through a daemon outage must never hail hours late."""

    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = _call_req(conn, ttl_s=10)
    claimed = radiostore.claim_call_request(conn, req_id, now_us=1_000_000 + 11 * 1_000_000)
    assert claimed is False
    row = radiostore.get_call_request(conn, req_id)
    assert row is not None
    assert row.state == "expired"
    assert row.outcome == "expired"
    conn.close()


def test_call_request_requeue_preserves_attempts(tmp_path: Path) -> None:
    """O6/O7 — busy channel or an inbound supersede never consumes a retry."""

    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = _call_req(conn)
    radiostore.claim_call_request(conn, req_id, now_us=1_500_000)
    radiostore.requeue_call_request(
        conn, req_id, attempts=1, call_id=42, detail="inbound caller outranked the attempt"
    )
    row = radiostore.get_call_request(conn, req_id)
    assert row is not None
    assert row.state == "pending"
    assert row.claimed_us is None
    assert row.attempts == 1
    assert row.call_id == 42
    # …and it is claimable again.
    assert radiostore.claim_call_request(conn, req_id, now_us=2_000_000) is True
    conn.close()


def test_call_request_settle_records_the_outcome(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = _call_req(conn)
    radiostore.claim_call_request(conn, req_id, now_us=1_500_000)
    radiostore.settle_call_request(
        conn,
        req_id,
        settled_us=2_000_000,
        outcome="delivered",
        attempts=0,
        detail="go-ahead 'go for dana'; keyed 2100 ms",
        call_id=7,
    )
    row = radiostore.get_call_request(conn, req_id)
    assert row is not None
    assert row.state == "done"
    assert row.outcome == "delivered"
    assert row.call_id == 7
    # Terminal is one-way — never re-claimed, never re-hailed.
    assert radiostore.claim_call_request(conn, req_id, now_us=2_500_000) is False
    conn.close()


def test_call_request_settle_rejects_unknown_outcome(tmp_path: Path) -> None:
    import pytest

    conn = radiostore.connect(tmp_path / "radio.db")
    req_id = _call_req(conn)
    with pytest.raises(ValueError, match="unknown call outcome"):
        radiostore.settle_call_request(
            conn, req_id, settled_us=1, outcome="vanished", attempts=0, detail=None, call_id=None
        )
    conn.close()


def test_pending_call_requests_returns_oldest_first(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    second = _call_req(conn, created_us=2_000_000)
    first = _call_req(conn, created_us=1_000_000)
    assert [r.id for r in radiostore.pending_call_requests(conn)] == [first, second]
    conn.close()


def test_prune_covers_call_requests(tmp_path: Path) -> None:
    conn = radiostore.connect(tmp_path / "radio.db")
    _call_req(conn, created_us=1_000_000)
    radiostore.prune(conn, older_than_us=5_000_000)
    assert radiostore.pending_call_requests(conn) == []
    conn.close()
