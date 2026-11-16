"""Unit (feature 112): dashboard fetch outcomes, defensive feed parsing, the poll cycle.

Fixture-driven and fully offline (injected fetchers). Pins research D2–D4 plus the LIVE
schema (verified 2026-08-29): 403 is a recorded ``refused`` outcome, unchanged is detected
by ETag (304) or body hash, the public envelope's ``synced.traffic`` posts parse (id /
Z-stamped created_at / entity author / note_tweet overflow), the pre-event fallback chains
still hold, and a drift payload still yields served entries as raw text (FR-006/SC-005) —
availability never drops on a shape surprise.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core import traffic, trafficstore
from obi.core.config import TrafficSettings
from obi.core.traffic import FetchOutcome, parse_feed, parse_gate_status, run_cycle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "traffic"

US = 1_000_000
T0 = 1_756_500_000 * US


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- parse_feed -----------------------------------------------------------------------------


def test_parse_feed_list_shape_exact() -> None:
    entries = parse_feed(_fixture("synthetic-feed-list.json"))
    assert entries is not None and len(entries) == 3
    newest = entries[0]
    assert newest.entry_key == "toc-105"
    # 2026-08-29T14:05:00-07:00
    assert newest.published_us == 1_788_037_500 * US
    assert newest.text == "Gravel to Gate: 90 min. Gate to Greeters: 25 min."
    assert newest.title == "Travel Time Update"
    assert newest.author == "Gate/Perimeter TOC"
    assert newest.pinned is False
    assert entries[2].pinned is True
    assert json.loads(newest.raw)["id"] == "toc-105"  # raw JSON kept verbatim


def test_parse_feed_wrapped_shape_fallback_spellings() -> None:
    entries = parse_feed(_fixture("synthetic-feed-wrapped.json"))
    assert entries is not None and len(entries) == 3
    assert entries[0].entry_key.endswith("105")
    assert entries[0].published_us == 1_788_037_500 * US  # ISO with offset
    assert entries[0].text == "Gravel to Gate: 90 min."
    assert entries[1].published_us == 1_788_037_800 * US  # epoch seconds
    assert entries[2].published_us is None  # no stamp at all → first-seen fallback later
    assert entries[2].text == "Exodus flowing normally, no wait at the gravel."


def test_parse_feed_drift_still_serves_text() -> None:
    entries = parse_feed(_fixture("synthetic-feed-drift.json"))
    assert entries is not None and len(entries) == 2
    dict_row, text_row = entries
    # no recognizable text field → the raw JSON IS the served text (FR-006)
    assert "gravel-gate" in dict_row.text and "90" in dict_row.text
    assert dict_row.published_us is None  # "when" is not a promised stamp key — no guessing
    assert dict_row.entry_key  # content-hash key, stable and non-empty
    assert text_row.text == "Gravel to Gate: 90 min (free-text row the schema never promised)"
    # same body parses to the same keys (dedup across re-served logs)
    again = parse_feed(_fixture("synthetic-feed-drift.json"))
    assert again is not None
    assert [e.entry_key for e in again] == [e.entry_key for e in entries]


def test_parse_feed_live_public_envelope() -> None:
    """The REAL /api/feed/public capture (2026-08-29, trimmed): the entry list nests at
    synced.traffic as @bmantraffic posts — id key, Z-suffixed created_at, entity-shaped
    author — and the top-level messages/feeds keys never masquerade as the entry list."""

    entries = parse_feed(_fixture("public-feed-live.json"))
    assert entries is not None and len(entries) == 3
    newest = entries[0]
    assert newest.entry_key == "2093806696779202917"
    # 2026-08-29T21:03:12.000Z
    assert newest.published_us == 1_788_037_392 * US
    assert newest.text == ("The current travel time from Gravel to Gate is 1 hour 30 minutes.")
    assert newest.author == "Burning Man Traffic"
    assert newest.pinned is False
    assert json.loads(newest.raw)["id"] == "2093806696779202917"


def test_parse_feed_note_tweet_full_text_wins() -> None:
    """A long post's note_tweet.text is the FULL text — preferred over the clipped
    text field, exactly as the dashboard itself renders it."""

    body = json.dumps(
        {
            "synced": {
                "traffic": [
                    {
                        "id": "42",
                        "text": "Long post preview…",
                        "note_tweet": {"text": "Long post FULL text with every detail."},
                        "created_at": "2026-08-29T20:00:00.000Z",
                        "author": {"name": "Burning Man Traffic", "username": "bmantraffic"},
                    }
                ]
            }
        }
    )
    entries = parse_feed(body)
    assert entries is not None and len(entries) == 1
    assert entries[0].text == "Long post FULL text with every detail."
    assert entries[0].author == "Burning Man Traffic"


def test_parse_feed_unusable_shapes() -> None:
    assert parse_feed("not json at all") is None
    assert parse_feed('{"message": "Forbidden"}') is None  # no list anywhere
    assert parse_feed('{"entries": {}}') is None


# --- parse_gate_status ----------------------------------------------------------------------


def test_parse_gate_status_null_fixture() -> None:
    body = _fixture("gate-status-null.json")
    status = parse_gate_status(body)
    assert status is not None
    assert status.inbound_state is None
    assert status.outbound_state is None
    assert status.advisory is None
    assert status.raw == body  # verbatim


def test_parse_gate_status_lenient_shapes() -> None:
    status = parse_gate_status(
        '{"inbound": "open", "outbound": "closed", "status": "egress paused for dust"}'
    )
    assert status is not None
    assert status.inbound_state == "open"
    assert status.outbound_state == "closed"
    assert status.advisory == "egress paused for dust"

    nested = parse_gate_status(
        '{"inbound": {"state": "open"}, "outbound": {"open": false, "message": "hold at gate"}}'
    )
    assert nested is not None
    assert nested.inbound_state == "open"
    assert nested.outbound_state == "closed"
    assert nested.advisory == "hold at gate"

    unrecognized = parse_gate_status('["some", "array"]')  # valid JSON, unknown shape
    assert unrecognized is not None
    assert unrecognized.inbound_state is None and unrecognized.raw == '["some", "array"]'

    assert parse_gate_status("<html>") is None  # not JSON → failed


# --- run_cycle ------------------------------------------------------------------------------


def _settings(tmp_path: Path) -> TrafficSettings:
    return TrafficSettings(base_url="https://traffic.example.net", db_path=tmp_path / "traffic.db")


def _feed(outcome: FetchOutcome):
    def fetch(if_none_match: str | None) -> FetchOutcome:
        return outcome

    return fetch


def _status(outcome: FetchOutcome):
    def fetch() -> FetchOutcome:
        return outcome

    return fetch


def test_cycle_pre_event_refusal_is_a_recorded_outcome(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = trafficstore.connect(settings.db_path)
    summary = run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(
            FetchOutcome(status_code=403, etag=None, body=_fixture("toc-feed-403.json"))
        ),
        status_fetcher=_status(
            FetchOutcome(status_code=200, etag=None, body=_fixture("gate-status-null.json"))
        ),
    )
    assert summary.feed_outcome == "refused"
    assert summary.feed_error == "http 403"
    assert summary.status_outcome == "new"
    assert summary.new_entries == 0
    poll = trafficstore.newest_poll(conn)
    assert poll is not None and poll.feed_outcome == "refused"
    status = trafficstore.newest_gate_status(conn)
    assert status is not None and status.inbound_state is None  # all-null captured honestly


def test_cycle_total_transport_failure_still_records(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = trafficstore.connect(settings.db_path)
    summary = run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(FetchOutcome(status_code=None, etag=None, body=None, error="timed out")),
        status_fetcher=_status(
            FetchOutcome(status_code=None, etag=None, body=None, error="connecterror")
        ),
    )
    assert summary.feed_outcome == "failed" and summary.feed_error == "timed out"
    assert summary.status_outcome == "failed" and summary.status_error == "connecterror"
    assert trafficstore.newest_poll(conn) is not None


def test_cycle_new_then_unchanged_by_hash(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = trafficstore.connect(settings.db_path)
    body = _fixture("synthetic-feed-list.json")
    status_body = _fixture("gate-status-null.json")
    first = run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(FetchOutcome(status_code=200, etag=None, body=body)),
        status_fetcher=_status(FetchOutcome(status_code=200, etag=None, body=status_body)),
    )
    assert first.feed_outcome == "new" and first.new_entries == 3
    second = run_cycle(
        settings,
        conn,
        now_us=T0 + 300 * US,
        feed_fetcher=_feed(FetchOutcome(status_code=200, etag=None, body=body)),
        status_fetcher=_status(FetchOutcome(status_code=200, etag=None, body=status_body)),
    )
    assert second.feed_outcome == "unchanged" and second.new_entries == 0
    assert second.status_outcome == "unchanged"
    assert trafficstore.entry_count(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM raw_payloads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM gate_statuses").fetchone()[0] == 1


def test_cycle_304_via_stored_etag(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = trafficstore.connect(settings.db_path)
    body = _fixture("synthetic-feed-list.json")
    run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(FetchOutcome(status_code=200, etag='W/"v1"', body=body)),
        status_fetcher=_status(
            FetchOutcome(status_code=200, etag=None, body=_fixture("gate-status-null.json"))
        ),
    )
    state = trafficstore.get_fetch_state(conn, source="feed")
    assert state is not None and state.etag == 'W/"v1"'

    seen: list[str | None] = []

    def conditional(if_none_match: str | None) -> FetchOutcome:
        seen.append(if_none_match)
        return FetchOutcome(status_code=304, etag='W/"v1"', body=None)

    summary = run_cycle(
        settings,
        conn,
        now_us=T0 + 300 * US,
        feed_fetcher=conditional,
        status_fetcher=_status(
            FetchOutcome(status_code=200, etag=None, body=_fixture("gate-status-null.json"))
        ),
    )
    assert seen == ['W/"v1"']  # the stored validator was actually sent
    assert summary.feed_outcome == "unchanged"


def test_cycle_status_change_writes_a_new_row(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = trafficstore.connect(settings.db_path)
    run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(FetchOutcome(status_code=403, etag=None, body="{}")),
        status_fetcher=_status(
            FetchOutcome(status_code=200, etag=None, body=_fixture("gate-status-null.json"))
        ),
    )
    summary = run_cycle(
        settings,
        conn,
        now_us=T0 + 300 * US,
        feed_fetcher=_feed(FetchOutcome(status_code=403, etag=None, body="{}")),
        status_fetcher=_status(
            FetchOutcome(
                status_code=200,
                etag=None,
                body='{"inbound": "open", "outbound": "open", "status": null}',
            )
        ),
    )
    assert summary.status_outcome == "new"
    newest = trafficstore.newest_gate_status(conn)
    assert newest is not None and newest.inbound_state == "open"
    assert conn.execute("SELECT COUNT(*) FROM gate_statuses").fetchone()[0] == 2


def test_cycle_unparseable_bodies_fail_without_raising(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conn = trafficstore.connect(settings.db_path)
    summary = run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(FetchOutcome(status_code=200, etag=None, body="<html>")),
        status_fetcher=_status(FetchOutcome(status_code=200, etag=None, body="<html>")),
    )
    assert summary.feed_outcome == "failed"
    assert summary.feed_error == "unparseable response"
    assert summary.status_outcome == "failed"
    assert summary.status_error == "unparseable response"


def test_cycle_prunes_by_retention(tmp_path: Path) -> None:
    settings = TrafficSettings(
        base_url="https://traffic.example.net", db_path=tmp_path / "traffic.db", retention_days=1
    )
    conn = trafficstore.connect(settings.db_path)
    run_cycle(
        settings,
        conn,
        now_us=T0,
        feed_fetcher=_feed(FetchOutcome(status_code=403, etag=None, body="{}")),
        status_fetcher=_status(
            FetchOutcome(status_code=200, etag=None, body=_fixture("gate-status-null.json"))
        ),
    )
    summary = run_cycle(
        settings,
        conn,
        now_us=T0 + 3 * 86_400 * US,
        feed_fetcher=_feed(FetchOutcome(status_code=403, etag=None, body="{}")),
        status_fetcher=_status(
            FetchOutcome(status_code=200, etag=None, body=_fixture("gate-status-null.json"))
        ),
    )
    assert summary.pruned == 1
    assert conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 1


def test_default_fetchers_build_urls_from_base_only(tmp_path: Path) -> None:
    # FR-009: the request targets derive from settings.base_url and module constants alone
    settings = TrafficSettings(base_url="https://example.test")
    assert traffic.feed_url(settings) == "https://example.test/api/feed/public"
    assert traffic.gate_status_url(settings) == "https://example.test/api/gate-status"
