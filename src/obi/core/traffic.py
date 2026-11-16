"""BRC dashboard gate-traffic fetch + defensive parse + collection cycle (feature 112).

Two public endpoints on the ONE configured dashboard host (spec FR-009), fetched
independently each 5-minute cycle: ``/api/feed/public`` (the dashboard's public sync
channel — verified live 2026-08-29 when the event went public; the pre-event guess
``/api/feed/toc`` 403s forever, that channel never opened) and ``/api/gate-status``.
One :func:`run_cycle` writes exactly one poll row per invocation — including on refusal
or total failure, because the recorded outcome IS the degraded mode (Principle VI). No
retries: the next cron tick is the retry (the weather/elevation precedent), so a cycle
can never stall or overlap its successor.

Change detection (research D3): a stored ETag is replayed as ``If-None-Match`` (304 →
``unchanged``; the live endpoint serves a stable weak ETag and honors it); an identical
body SHA-256 also records ``unchanged`` without rewriting rows. The live feed is an
envelope dict whose Travel Time Log lives at ``synced.traffic`` — @bmantraffic posts of
``{id, text, note_tweet, created_at, author:{name, username}}`` — and :func:`parse_feed`
recognizes that shape plus the original research-D4 fallback chains, keeping every
entry's raw JSON verbatim; an entry with no recognizable text is still served as its raw
JSON (FR-006 — drift degrades polish, never availability).

Fetchers are injectable ``Callable``s (the offline-test seam); this module is the feature's
ONLY network I/O and — per the Principle IV quarantine convention — the only
traffic-related importer of ``httpx``. Requests carry the honest static ``User-Agent``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from obi.core import trafficstore
from obi.core.config import TrafficSettings
from obi.core.trafficstore import GateStatus, ParsedEntry

logger = logging.getLogger("obi")

_FEED_PATH = "/api/feed/public"
_GATE_STATUS_PATH = "/api/gate-status"
# Honest self-identification (the spec's respectful-use assumption); static, never config.
_USER_AGENT = "obi (network operations agent)"

#: The zone the feed's naive timestamps are in — the operator's ``[site] timezone`` (137),
#: set by :func:`run_cycle` for the duration of one cycle.
_FEED_TZ: ContextVar[str] = ContextVar("obi_traffic_tz", default="UTC")

# The research-D4 fallback chains, extended with the live schema (verified 2026-08-29):
# the public envelope nests its entry list at synced.traffic. Anything unrecognized
# still serves as raw text.
_LIST_KEYS = ("entries", "items", "feed", "data", "posts")
_SYNCED_LIST_KEYS = ("traffic", *_LIST_KEYS)
_KEY_KEYS = ("id", "uuid", "key")
_STAMP_KEYS = ("timestamp", "published", "created_at", "date", "time")
_TEXT_KEYS = ("text", "body", "message", "content")


def feed_url(settings: TrafficSettings) -> str:
    return settings.base_url + _FEED_PATH


def gate_status_url(settings: TrafficSettings) -> str:
    return settings.base_url + _GATE_STATUS_PATH


@dataclass(frozen=True)
class FetchOutcome:
    """One HTTP fetch, as facts: a status code + body when the transport worked, else a
    terse ``error``. Outcome classification happens in the cycle, not the fetcher."""

    status_code: int | None
    etag: str | None
    body: str | None
    error: str | None = None


FeedFetcher = Callable[[str | None], FetchOutcome]
StatusFetcher = Callable[[], FetchOutcome]


def _terse_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return type(exc).__name__.lower() or "failed"


def _default_feed_fetcher(settings: TrafficSettings) -> FeedFetcher:
    def fetch(if_none_match: str | None) -> FetchOutcome:
        headers = {"User-Agent": _USER_AGENT}
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        try:
            response = httpx.get(feed_url(settings), headers=headers, timeout=settings.timeout_s)
        except httpx.HTTPError as exc:
            logger.debug("traffic feed fetch failed: %s", exc)
            return FetchOutcome(status_code=None, etag=None, body=None, error=_terse_error(exc))
        return FetchOutcome(
            status_code=response.status_code,
            etag=response.headers.get("etag"),
            body=response.text,
        )

    return fetch


def _default_status_fetcher(settings: TrafficSettings) -> StatusFetcher:
    def fetch() -> FetchOutcome:
        try:
            response = httpx.get(
                gate_status_url(settings),
                headers={"User-Agent": _USER_AGENT},
                timeout=settings.timeout_s,
            )
        except httpx.HTTPError as exc:
            logger.debug("gate-status fetch failed: %s", exc)
            return FetchOutcome(status_code=None, etag=None, body=None, error=_terse_error(exc))
        return FetchOutcome(
            status_code=response.status_code,
            etag=response.headers.get("etag"),
            body=response.text,
        )

    return fetch


# --- defensive parsing (research D4) --------------------------------------------------------


def _stamp_to_us(value: object) -> int | None:
    """A best-effort published time: ISO-8601 (naive = playa-local), or epoch
    seconds/millis. ``None`` — never a guess — when nothing parses."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:  # epoch milliseconds
            seconds /= 1000.0
        if not 1e9 < seconds < 4e9:  # a plausible 2001–2096 stamp, nothing else
            return None
        return int(seconds * 1_000_000)
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(_FEED_TZ.get()))
        return int(stamp.timestamp() * 1_000_000)
    return None


def _first_str(entry: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if key in _KEY_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _author_from(element: dict[str, object]) -> str | None:
    """An author whether flat (``"author": "TOC"``) or entity-shaped, as the live feed
    publishes it (``"author": {"name": ..., "username": ...}``)."""

    flat = _first_str(element, ("author", "user", "by"))
    if flat is not None:
        return flat
    for key in ("author", "user", "by"):
        value = element.get(key)
        if isinstance(value, dict):
            nested = _first_str(value, ("name", "username"))
            if nested is not None:
                return nested
    return None


def _entry_from(element: object) -> ParsedEntry:
    raw = json.dumps(element, ensure_ascii=False)
    if not isinstance(element, dict):
        text = element if isinstance(element, str) else raw
        return ParsedEntry(
            entry_key=hashlib.sha256(raw.encode()).hexdigest(),
            published_us=None,
            title=None,
            author=None,
            pinned=False,
            text=text,
            raw=raw,
        )
    key = _first_str(element, _KEY_KEYS) or hashlib.sha256(raw.encode()).hexdigest()
    published_us = None
    for stamp_key in _STAMP_KEYS:
        if stamp_key in element:
            published_us = _stamp_to_us(element[stamp_key])
            if published_us is not None:
                break
    # The live feed's long-post overflow: note_tweet.text carries the FULL text when the
    # main text is clipped — the dashboard itself prefers it, so we do too.
    note = element.get("note_tweet")
    note_text = note.get("text") if isinstance(note, dict) else None
    text = (
        (note_text.strip() if isinstance(note_text, str) and note_text.strip() else None)
        or _first_str(element, _TEXT_KEYS)
        or raw  # raw JSON IS the text of last resort
    )
    title = _first_str(element, ("title",))
    author = _author_from(element)
    return ParsedEntry(
        entry_key=key,
        published_us=published_us,
        title=title,
        author=author,
        pinned=bool(element.get("pinned", False)),
        text=text,
        raw=raw,
    )


def parse_feed(body: str) -> list[ParsedEntry] | None:
    """Normalize a Travel Time Log response; ``None`` only when structurally unusable
    (not JSON, or no entry list anywhere). Order is preserved as served; storage order
    is by parsed/first-seen time anyway."""

    try:
        payload = json.loads(body)
    except ValueError:
        return None
    elements: list[object] | None = None
    if isinstance(payload, list):
        elements = payload
    elif isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                elements = value
                break
        if elements is None:
            # The live public envelope: the entry list nests under synced.traffic.
            synced = payload.get("synced")
            if isinstance(synced, dict):
                for key in _SYNCED_LIST_KEYS:
                    value = synced.get(key)
                    if isinstance(value, list):
                        elements = value
                        break
    if elements is None:
        return None
    return [_entry_from(element) for element in elements]


def _lane_state(value: object) -> tuple[str | None, str | None]:
    """(state, advisory-ish) from one lane value, whatever shape it takes."""

    if isinstance(value, str) and value.strip():
        return value.strip(), None
    if isinstance(value, dict):
        state = _first_str(value, ("state", "status"))
        if state is None and isinstance(value.get("open"), bool):
            state = "open" if value["open"] else "closed"
        advisory = _first_str(value, ("advisory", "message", "note"))
        return state, advisory
    return None, None


def parse_gate_status(body: str) -> GateStatus | None:
    """Normalize a gate-status response; ``None`` only when the body is not JSON. Any
    valid JSON yields a status — unrecognized shapes keep all parsed fields ``None``
    with the raw body as the authority (data-model: raw-only rows still render)."""

    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return GateStatus(inbound_state=None, outbound_state=None, advisory=None, raw=body)
    inbound_state, inbound_advisory = _lane_state(payload.get("inbound"))
    outbound_state, outbound_advisory = _lane_state(payload.get("outbound"))
    advisory = (
        _first_str(payload, ("status", "advisory", "message"))
        or inbound_advisory
        or outbound_advisory
    )
    return GateStatus(
        inbound_state=inbound_state,
        outbound_state=outbound_state,
        advisory=advisory,
        raw=body,
    )


# --- the collection cycle -------------------------------------------------------------------


@dataclass(frozen=True)
class CycleSummary:
    """One ``obi traffic run`` outcome — what was recorded, per source."""

    poll_id: int
    feed_outcome: str
    feed_error: str | None
    status_outcome: str
    status_error: str | None
    new_entries: int
    pruned: int


def _classify(outcome: FetchOutcome) -> tuple[str, str | None]:
    """(outcome, error) for a fetch that did not produce usable new data by itself."""

    if outcome.status_code is None:
        return "failed", outcome.error or "failed"
    if outcome.status_code == 304:
        return "unchanged", None
    if outcome.status_code in (401, 403):
        return "refused", f"http {outcome.status_code}"
    return "failed", f"http {outcome.status_code}"


def run_cycle(
    settings: TrafficSettings,
    conn: trafficstore.Connection,
    *,
    now_us: int,
    feed_fetcher: FeedFetcher | None = None,
    status_fetcher: StatusFetcher | None = None,
    timezone: str = "UTC",
) -> CycleSummary:
    """One collector cycle; ``timezone`` is the ``[site]`` zone of the feed's naive stamps."""

    token = _FEED_TZ.set(timezone)
    try:
        return _run_cycle(
            settings, conn, now_us=now_us, feed_fetcher=feed_fetcher, status_fetcher=status_fetcher
        )
    finally:
        _FEED_TZ.reset(token)


def _run_cycle(
    settings: TrafficSettings,
    conn: trafficstore.Connection,
    *,
    now_us: int,
    feed_fetcher: FeedFetcher | None = None,
    status_fetcher: StatusFetcher | None = None,
) -> CycleSummary:
    """Fetch both sources independently, write ONE poll row, prune, commit — atomically.

    A refusal or failure (fetch or parse) is a recorded outcome on the poll, never an
    exception out of the cycle; previously stored entries and statuses remain what readers
    serve. Commits before returning: insert + prune are one transaction.
    """

    fetch_feed = feed_fetcher or _default_feed_fetcher(settings)
    fetch_status = status_fetcher or _default_status_fetcher(settings)

    feed_state = trafficstore.get_fetch_state(conn, source="feed")
    feed_result = fetch_feed(feed_state.etag if feed_state else None)
    entries: list[ParsedEntry] = []
    feed_body_to_store: str | None = None
    new_etag = feed_result.etag or (feed_state.etag if feed_state else None)
    new_sha = feed_state.body_sha256 if feed_state else None
    if feed_result.status_code is not None and 200 <= feed_result.status_code < 300:
        body = feed_result.body or ""
        body_sha = hashlib.sha256(body.encode()).hexdigest()
        if feed_state is not None and feed_state.body_sha256 == body_sha:
            feed_outcome, feed_error = "unchanged", None
        else:
            parsed = parse_feed(body)
            if parsed is None:
                feed_outcome, feed_error = "failed", "unparseable response"
            else:
                feed_outcome, feed_error = "new", None
                entries = parsed
                feed_body_to_store = body
                new_sha = body_sha
    else:
        feed_outcome, feed_error = _classify(feed_result)

    status_result = fetch_status()
    gate_status: GateStatus | None = None
    if status_result.status_code is not None and 200 <= status_result.status_code < 300:
        parsed_status = parse_gate_status(status_result.body or "")
        if parsed_status is None:
            status_outcome, status_error = "failed", "unparseable response"
        else:
            previous = trafficstore.newest_gate_status(conn)
            if previous is not None and previous.raw == parsed_status.raw:
                status_outcome, status_error = "unchanged", None
            else:
                status_outcome, status_error = "new", None
                gate_status = parsed_status
    else:
        status_outcome, status_error = _classify(status_result)

    before = trafficstore.entry_count(conn)
    poll_id = trafficstore.insert_cycle(
        conn,
        fetched_us=now_us,
        feed_outcome=feed_outcome,
        feed_error=feed_error,
        status_outcome=status_outcome,
        status_error=status_error,
        entries=entries,
        gate_status=gate_status,
        feed_body=feed_body_to_store,
    )
    if feed_outcome in ("new", "unchanged"):
        trafficstore.set_fetch_state(conn, source="feed", etag=new_etag, body_sha256=new_sha)
    pruned = trafficstore.prune(
        conn, older_than_us=now_us - settings.retention_days * 86_400 * 1_000_000
    )
    conn.commit()
    return CycleSummary(
        poll_id=poll_id,
        feed_outcome=feed_outcome,
        feed_error=feed_error,
        status_outcome=status_outcome,
        status_error=status_error,
        new_entries=trafficstore.entry_count(conn) - before if entries else 0,
        pruned=pruned,
    )
