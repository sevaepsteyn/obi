"""The local-data API layer (feature 038): read-only JSON routes over LOCAL stores.

This module is the web server's second camp-1 extension point (the first is the static
allowlist): routes here answer browser requests by reading files that already live on the
operator's machine — never by contacting a device or controller. Serving these routes is
the same class of act as serving ``links.json``; the server's offline-by-construction
character (022/033) is unchanged, so no constitution carve-out applies.

Routes: ``GET /api/logs.json`` over the feature-013 log store (038,
``specs/038-web-data-widgets/contracts/logs-api.md``); ``GET /api/watch.json`` over the
014 watch reports store and ``GET /api/inventory.json`` over the saved discovery roster
(040, ``specs/040-web-tool-boards/contracts/snapshots-and-routes.md``). Future local-store
routes (metrics history, configs — 041) join this module; device/controller-touching tools
instead ride the cron-written ``/snapshots/*.json`` files in the allowlist.

Layering (constitution III): this module parses parameters, calls :mod:`obi.core.logstore`
read seams, and serializes with the same dict shape ``obi logs query --json`` uses. All
query semantics live in the core. Store connections are per-request and read-only
(``mode=ro``) — a browser can never block or disturb the ingest crons. Heavy imports are
deferred so the static-only server stays light (the ``server.py`` discipline).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler
    from pathlib import Path

    from obi.core.config import (
        AuditSettings,
        Config,
        DashSettings,
        LogsSettings,
        RadioSettings,
        SnmpHistorySettings,
    )

_WINDOW_RE = re.compile(r"^(\d+)([mhd])$")
_WINDOW_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
_DEFAULT_WINDOW = "24h"


def _parse_window(raw: str) -> timedelta:
    match = _WINDOW_RE.match(raw)
    if match is None:
        raise _BadRequest(f"window must look like 15m / 6h / 7d (got {raw!r})")
    return timedelta(**{_WINDOW_UNITS[match.group(2)]: int(match.group(1))})


class _BadRequest(Exception):
    """A malformed known parameter — becomes the 400 body's one-line reason."""


# feature 136: an injectable clock. The static-demo export renders window-relative routes
# (metrics series, health ages) as of the DATASET's window end, not the build time; the live
# server never sets it, so live behaviour is unchanged.
_NOW_OVERRIDE: datetime | None = None


def _now(tz: tzinfo | None = None) -> datetime:
    if _NOW_OVERRIDE is not None:
        return _NOW_OVERRIDE.astimezone(tz)
    return datetime.now(tz)


@contextmanager
def frozen_now(when: datetime) -> Iterator[None]:
    """Every handler in this module sees ``when`` as now (export-time rendering only)."""

    global _NOW_OVERRIDE
    previous, _NOW_OVERRIDE = _NOW_OVERRIDE, when
    try:
        yield
    finally:
        _NOW_OVERRIDE = previous


@dataclass(frozen=True)
class _LogsQuery:
    source: str | None
    device: str | None
    min_severity: str | None
    contains: str | None
    since: datetime | None
    limit: int
    after: int | None  # presence selects tail mode


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _parse_query(raw_query: str, settings: LogsSettings) -> _LogsQuery:
    """Validate the request per contracts/logs-api.md: malformed known params raise
    :class:`_BadRequest`; unknown params are ignored (additive evolution)."""

    from obi.core.logstore import SEVERITY_ORDER

    params = parse_qs(raw_query, keep_blank_values=False)

    severity = _first(params, "severity")
    if severity is not None and severity.lower() not in SEVERITY_ORDER:
        raise _BadRequest(
            f"severity must be a floor among {', '.join(SEVERITY_ORDER)} (got {severity!r})"
        )

    raw_limit = _first(params, "limit")
    limit = settings.default_limit
    if raw_limit is not None:
        if not raw_limit.isdigit() or int(raw_limit) < 1:
            raise _BadRequest(f"limit must be a positive integer (got {raw_limit!r})")
        limit = int(raw_limit)
        if limit > settings.max_limit:
            raise _BadRequest(f"limit exceeds the cap of {settings.max_limit}")

    raw_after = _first(params, "after")
    after: int | None = None
    if raw_after is not None:
        if not raw_after.isdigit():
            raise _BadRequest(f"after must be a non-negative integer cursor (got {raw_after!r})")
        after = int(raw_after)

    since: datetime | None = None
    if after is None:  # the window bounds the initial page only; the tail is id-bounded
        raw_window = _first(params, "window") or _DEFAULT_WINDOW
        since = _now(settings.tzinfo()) - _parse_window(raw_window)

    return _LogsQuery(
        source=_first(params, "source"),
        device=_first(params, "device"),
        min_severity=severity.lower() if severity else None,
        contains=_first(params, "q"),
        since=since,
        limit=limit,
        after=after,
    )


def _send_json(
    handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any], *, head_only: bool
) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    if not head_only:
        handler.wfile.write(body)


def _absent(reason_hint: str) -> dict[str, Any]:
    # An empty store is a state, not an error (spec edge case): HTTP 200, hint names the fix.
    return {"store": "absent", "mode": "initial", "entries": [], "cursor": None,
            "hint": reason_hint}


def handle_dash_request(
    handler: BaseHTTPRequestHandler, settings: DashSettings | None, *, head_only: bool = False
) -> None:
    """Serve one ``GET/HEAD /api/dash.json`` (feature 095, contracts/dash-surfaces.md).

    Deliberately dumb (camp 1, the local-file idiom): serves the last cycle's persisted
    ``board.json`` verbatim — no device I/O, no evaluation, no freshness judgment (the client
    renders a board older than 3 minutes as stale everywhere). Absent config/board is a state,
    not an error: HTTP 200 with the hint naming the fixing command.
    """

    from obi.core import dash as dash_mod

    if settings is None:
        _send_json(
            handler,
            200,
            {
                "store": "absent",
                "hint": "no [dash] configuration — see docs/guide/dashboard.md § Status dashboard",
            },
            head_only=head_only,
        )
        return
    board = dash_mod.load_board(settings.state_dir)
    if board is None:
        _send_json(
            handler,
            200,
            {"store": "absent", "hint": "no dashboard snapshot yet — run: obi dash run (cron)"},
            head_only=head_only,
        )
        return
    _send_json(
        handler,
        200,
        {"store": "ok", "hint": None, "board": board.to_json()},
        head_only=head_only,
    )


def handle_logs_request(
    handler: BaseHTTPRequestHandler, settings: LogsSettings | None, *, head_only: bool = False
) -> None:
    """Serve one ``GET/HEAD /api/logs.json``. The caller has already routed the path."""

    # Deferred: the store module loads only when the route is actually used. sqlite3 stays
    # quarantined to logstore (its module contract) — failures surface as StoreError.
    from obi.core import logstore
    from obi.core.serialize import log_event_to_dict

    if settings is None:
        _send_json(handler, 200, _absent(
            "no [logs] configuration — see docs/guide/logs.md"), head_only=head_only)
        return

    try:
        query = _parse_query(urlsplit(handler.path).query, settings)
    except _BadRequest as exc:
        _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)
        return

    if not settings.db_path.exists():
        _send_json(handler, 200, _absent(
            "no log store yet — run: obi logs ingest (see docs/guide/logs.md § Device syslog)"),
            head_only=head_only)
        return

    try:
        conn = logstore.connect_readonly(settings.db_path)
    except logstore.StoreError as exc:
        _send_json(handler, 200, _absent(f"log store unreadable: {exc}"), head_only=head_only)
        return
    try:
        if query.after is not None:
            rows = logstore.query_since(
                conn,
                after_id=query.after,
                limit=query.limit,
                host=query.device,
                min_severity=query.min_severity,
                contains=query.contains,
                source=query.source,
            )
            entries = [log_event_to_dict(event) for _id, event in rows]
            cursor: int | None = rows[-1][0] if rows else query.after
            mode = "tail"
        else:
            events = logstore.query_events(
                conn,
                host=query.device,
                since=query.since,
                min_severity=query.min_severity,
                contains=query.contains,
                source=query.source,
                limit=query.limit,
            )
            # query_events is newest-first; a feed appends — deliver ascending.
            entries = [log_event_to_dict(event) for event in reversed(events)]
            cursor = logstore.latest_cursor(conn)
            mode = "initial"
    except logstore.StoreError as exc:
        _send_json(handler, 200, _absent(f"log store unreadable: {exc}"), head_only=head_only)
        return
    finally:
        conn.close()

    _send_json(
        handler,
        200,
        {"store": "ok", "mode": mode, "entries": entries, "cursor": cursor, "hint": None},
        head_only=head_only,
    )


# --- GET /api/radio.json (feature 088): the radio transcript feed, local store only ------------


def handle_radio_request(
    handler: BaseHTTPRequestHandler,
    settings: RadioSettings | None,
    *,
    head_only: bool = False,
) -> None:
    """Serve one ``GET/HEAD /api/radio.json`` per specs/088-radio/contracts/radio-api.md.

    The logs-api tail idiom over the feature-088 transmissions feed: initial mode returns
    the newest ≤limit within ``window`` ascending; ``after=<id>`` tails on the monotonic
    rowid cursor. ``self`` rows (obi's own audio echo) are excluded unless explicitly
    requested; ``stage`` is the served config's rollout stage so the board can badge it.
    """

    from obi.core import radiostore

    if settings is None:
        _send_json(
            handler,
            200,
            {**_absent("no [radio] configuration — see docs/setup/radio.md"),
             "stage": None},
            head_only=head_only,
        )
        return

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    try:
        window = _parse_window(_first(params, "window") or _DEFAULT_WINDOW)
        raw_limit = _first(params, "limit")
        limit = 200
        if raw_limit is not None:
            limit = int(raw_limit)
            if limit < 1:
                raise _BadRequest(f"limit must be >= 1 (got {limit})")
        raw_after = _first(params, "after")
        after: int | None = None
        if raw_after is not None:
            try:
                after = int(raw_after)
            except ValueError as exc:
                raise _BadRequest(f"after must be an integer id (got {raw_after!r})") from exc
        kind = _first(params, "kind")
        if kind is not None and kind not in radiostore.TRANSMISSION_KINDS:
            raise _BadRequest(f"kind must be rx, obi, or self (got {kind!r})")
        contains = _first(params, "q")
    except _BadRequest as exc:
        _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)
        return
    except ValueError as exc:
        _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)
        return

    stage = settings.stage()
    if not settings.db_path.exists():
        _send_json(
            handler,
            200,
            {**_absent("no radio traffic yet — run: obi radio start"), "stage": stage},
            head_only=head_only,
        )
        return
    try:
        conn = radiostore.connect_readonly(settings.db_path)
    except radiostore.StoreError as exc:
        _send_json(
            handler,
            200,
            {**_absent(f"radio store unreadable: {exc}"), "stage": stage},
            head_only=head_only,
        )
        return
    try:
        if after is not None:
            rows = radiostore.query_since(
                conn, after_id=after, limit=limit, include_self=(kind == "self")
            )
            if kind is not None:
                rows = [r for r in rows if r.kind == kind]
            cursor: int | None = rows[-1].id if rows else after
            mode = "tail"
        else:
            since_us = int((_now(UTC) - window).timestamp() * 1_000_000)
            rows = radiostore.query_window(
                conn, limit=limit, since_us=since_us, kind=kind, contains=contains
            )
            cursor = radiostore.latest_cursor(conn)
            mode = "initial"
    except radiostore.StoreError as exc:
        _send_json(
            handler,
            200,
            {**_absent(f"radio store unreadable: {exc}"), "stage": stage},
            head_only=head_only,
        )
        return
    finally:
        conn.close()

    entries = [
        {
            "id": row.id,
            "timestamp": row.ts_iso,
            "kind": row.kind,
            "caller": row.caller,
            "transcript": row.transcript,
            "duration_ms": row.duration_ms,
            "peak_dbfs": row.peak_dbfs,
            "gate": row.stt_gate,
            "trigger": row.trigger_reason if row.trigger_fired else None,
            "call_id": row.call_id,
            "outbound_kind": row.outbound_kind,
            "disposition": row.disposition,
            "has_clip": row.clip_path is not None,
            "speaker": row.speaker,
            "speaker_confidence": row.speaker_confidence,
        }
        for row in rows
    ]
    _send_json(
        handler,
        200,
        {
            "store": "ok",
            "mode": mode,
            "stage": stage,
            "entries": entries,
            "cursor": cursor,
            "hint": None,
        },
        head_only=head_only,
    )


def handle_radio_clip_request(
    handler: BaseHTTPRequestHandler,
    settings: RadioSettings | None,
    *,
    head_only: bool = False,
) -> None:
    """Serve one retained clip: ``GET /api/radio-clip.wav?id=<transmission id>``.

    Replay of the channel record on the operator's LAN (constitution 1.13.0 — the same
    boundary restatement voice got in 1.7.0). The clip path comes from the STORE row,
    never from the request, and is resolved strictly inside the clips directory — a
    tampered store value cannot escape it.
    """

    from obi.core import radiostore

    if settings is None:
        handler.send_error(404, "radio not configured")
        return
    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    raw_id = _first(params, "id")
    try:
        clip_id = int(raw_id) if raw_id is not None else None
    except ValueError:
        clip_id = None
    if clip_id is None:
        _send_json(handler, 400, {"error": "id must be a transmission id"}, head_only=head_only)
        return
    if not settings.db_path.exists():
        handler.send_error(404, "no radio store")
        return
    try:
        conn = radiostore.connect_readonly(settings.db_path)
        try:
            row = conn.execute(
                "SELECT clip_path FROM transmissions WHERE id = ?", (clip_id,)
            ).fetchone()
        finally:
            conn.close()
    except radiostore.StoreError:
        handler.send_error(404, "radio store unreadable")
        return
    if row is None or row[0] is None:
        handler.send_error(404, "no clip for that transmission")
        return
    clips_root = settings.clips_dir.resolve()
    clip = (clips_root / str(row[0])).resolve()
    if not clip.is_relative_to(clips_root) or not clip.is_file():
        handler.send_error(404, "no clip for that transmission")
        return
    try:
        body = clip.read_bytes()
    except OSError:
        handler.send_error(404, "no clip for that transmission")
        return
    # Normalize for PLAYBACK only — the stored clip stays the faithful record of what went
    # out (or came in). Received traffic is recorded off the radio's speaker and arrives
    # slammed; obi's own audio is scaled down for the radio's mic input, so replayed raw
    # the two differ by ~19 dB and obi sounds broken. See tx.normalize_wav.
    from obi.radio.tx import normalize_wav

    body = normalize_wav(body)
    handler.send_response(200)
    handler.send_header("Content-Type", "audio/wav")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    if not head_only:
        handler.wfile.write(body)


# --- GET /api/watch.json (feature 040): the watcher's findings, local reports dir only ---------


def handle_watch_request(
    handler: BaseHTTPRequestHandler,
    watch_dir: Path | None,
    logs_settings: LogsSettings | None = None,
    *,
    head_only: bool = False,
) -> None:
    """Serve one ``GET/HEAD /api/watch.json``: newest-first findings from the 014 store.

    Timestamps are emitted in the operator's ``log_tz`` (069) so the browser — which prints the
    bare wall-clock — shows local time; ``None`` settings fall back to UTC.
    """

    from datetime import UTC

    from obi.core.watch import finding_to_dict
    from obi.core.watchstore import WatchStore

    display_tz = logs_settings.tzinfo() if logs_settings is not None else UTC

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    try:
        window = _parse_window(_first(params, "window") or _DEFAULT_WINDOW)
        raw_limit = _first(params, "limit")
        limit = 50
        if raw_limit is not None:
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 500:
                raise _BadRequest(f"limit must be 1..500 (got {raw_limit!r})")
            limit = int(raw_limit)
    except _BadRequest as exc:
        _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)
        return

    store = WatchStore(watch_dir) if watch_dir is not None else None
    if store is None or not store.reports_dir.exists():
        _send_json(handler, 200, {
            "store": "absent", "findings": [],
            "hint": "no watch findings yet — the watcher writes them (obi watch run, "
                    "hourly on the operator cron)",
        }, head_only=head_only)
        return

    findings = store.list_findings(
        since=_now(UTC) - window,
        severity=_first(params, "severity"),  # the store's exact-match vocabulary
        device=_first(params, "device"),
        limit=limit,
    )
    _send_json(handler, 200, {
        "store": "ok",
        "findings": [finding_to_dict(finding, display_tz=display_tz) for finding in findings],
        "hint": None,
    }, head_only=head_only)


# --- GET /api/inventory.json (feature 040): the saved discovery roster ---------------------------


def handle_inventory_request(
    handler: BaseHTTPRequestHandler, inventory_path: Path | None, *, head_only: bool = False
) -> None:
    """Serve one ``GET/HEAD /api/inventory.json``: the roster file, whole (it is small)."""

    from obi.core.inventory import load, to_dict

    loaded = load(inventory_path) if inventory_path is not None else None
    if loaded is None:
        _send_json(handler, 200, {
            "store": "absent", "inventory": None,
            "hint": "no inventory yet — run: obi discover",
        }, head_only=head_only)
        return
    _send_json(
        handler,
        200,
        {"store": "ok", "inventory": to_dict(loaded), "hint": None},
        head_only=head_only,
    )


# --- GET /api/metrics.json (feature 041): the 021 history store, local SQLite only -------------


def handle_metrics_request(
    handler: BaseHTTPRequestHandler,
    settings: SnmpHistorySettings | None,
    *,
    head_only: bool = False,
) -> None:
    """Coverage mode (no ``target``) populates the MET picker; series mode (``target`` +
    ``metric`` [+ ``interface``, ``window``]) is the CLI history payload, verbatim."""

    from obi.core import metricstore
    from obi.core.serialize import metrics_devices_to_dict, metrics_history_to_dict

    if settings is None or not settings.db_path.exists():
        _send_json(handler, 200, {
            "store": "absent",
            "hint": "no metrics history yet — configure [snmp.history] and run: "
                    "obi metrics ingest (cron)",
        }, head_only=head_only)
        return

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    target = _first(params, "target")

    try:
        conn = metricstore.connect_readonly(settings.db_path)
    except metricstore.StoreError as exc:
        _send_json(handler, 200, {"store": "absent", "hint": f"metrics store unreadable: {exc}"},
                   head_only=head_only)
        return
    try:
        if target is None:
            # Names only — `metricstore.devices()`, not `coverage()`: the counted view scans the
            # whole store (~60s live, growing with retention) to compute totals the picker throws
            # away. `obi metrics status` still reports counts.
            payload = metrics_devices_to_dict(
                metricstore.devices(conn),
                db_path=settings.db_path,
                retain_days=settings.retain_days,
            )
            payload.update({"store": "ok", "mode": "coverage", "hint": None})
            _send_json(handler, 200, payload, head_only=head_only)
            return

        metric = _first(params, "metric")
        if not metric:
            _send_json(handler, 400, {"error": "series mode needs a metric"},
                       head_only=head_only)
            return
        try:
            window = _parse_window(_first(params, "window") or _DEFAULT_WINDOW)
        except _BadRequest as exc:
            _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)
            return
        until_dt = _now(UTC)
        since_dt = until_dt - window
        since_us = int(since_dt.timestamp() * 1_000_000)
        until_us = int(until_dt.timestamp() * 1_000_000)
        series_list = metricstore.query_series(
            conn,
            device=target,
            metric=metric,
            since_us=since_us,
            until_us=until_us,
            ifname=_first(params, "interface"),
            buckets=settings.query_buckets,
        )
        bucket_seconds = max(
            1, (until_us - since_us) // max(1, settings.query_buckets) // 1_000_000
        )
        payload = metrics_history_to_dict(
            target, metric, since_dt.isoformat(), until_dt.isoformat(),
            bucket_seconds, series_list,
        )
        payload.update({"store": "ok", "mode": "series", "hint": None})
        _send_json(handler, 200, payload, head_only=head_only)
    finally:
        conn.close()


# --- GET /api/configs.json (feature 041): the 026 backup repo, local git reads only -------------


def handle_configs_request(
    handler: BaseHTTPRequestHandler, app_config: Config | None, *, head_only: bool = False
) -> None:
    """Status mode (no ``device``), history mode (``device``), diff mode (``device`` +
    ``view=diff`` [+ ``from``/``to``]) — each the CLI's --json shape, intent-audited by the
    engine reads exactly like the CLI (the 026 rule: intent only, never config content)."""

    from obi.core.audit import AuditTrail, audit_scope, recorder_from_config
    from obi.core.engine import config_diff, config_history, config_show, config_status
    from obi.core.serialize import (
        config_diff_to_dict,
        config_history_to_dict,
        config_show_to_dict,
        config_status_to_dict,
    )

    if app_config is None or app_config.configs is None:
        _send_json(handler, 200, {
            "store": "absent",
            "hint": "config backups not configured — add a [configs] section and run: "
                    "obi configs ingest (cron)",
        }, head_only=head_only)
        return

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    device = _first(params, "device")
    audit = AuditTrail(app_config.audit_path)
    reason = "web CFG board read"

    # feature 061: run the intent-audited engine reads inside a web audit scope so each config_*
    # intent tees into the queryable audit store tagged source="web" (Principle V), the same way
    # /api/router.json (047) does. Without the scope the records land in the JSONL trail only and
    # never reach the queryable store — the drift this fixes. A disabled recorder opens nothing.
    with audit_scope(recorder_from_config(app_config), source="web", session_id=None):
        if device is None:
            report = config_status(app_config, audit=audit, reason=reason)
            payload = config_status_to_dict(report)
            payload.update({"store": "ok", "mode": "status", "hint": None})
        elif _first(params, "view") == "diff":
            diff_result = config_diff(
                app_config,
                device,
                from_ref=_first(params, "from"),
                to_ref=_first(params, "to") or "current",
                audit=audit,
                reason=reason,
            )
            payload = config_diff_to_dict(diff_result)
            payload.update({"store": "ok", "mode": "diff", "hint": None})
        elif _first(params, "view") == "show":
            # feature 049: the full stored config as of a version — the CFG widget's full-config
            # view. Read-only, offline (local 026 git via config_show), intent-audited (never
            # content); an unknown device/version degrades to found:false (the data_api idiom).
            show_result = config_show(
                app_config,
                device,
                at=_first(params, "at") or "current",
                audit=audit,
                reason=reason,
            )
            payload = config_show_to_dict(show_result)
            payload.update({"store": "ok", "mode": "show", "hint": None})
        else:
            raw_limit = _first(params, "limit")
            limit = 20
            if raw_limit is not None:
                if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 200:
                    _send_json(handler, 400, {"error": f"limit must be 1..200 (got {raw_limit!r})"},
                               head_only=head_only)
                    return
                limit = int(raw_limit)
            history_result = config_history(
                app_config, device, limit=limit, audit=audit, reason=reason
            )
            payload = config_history_to_dict(history_result)
            payload.update({"store": "ok", "mode": "history", "hint": None})
        _send_json(handler, 200, payload, head_only=head_only)


# --- GET /api/router.json (feature 047): one read-only live RouterOS drill-down ------------------


def handle_router_request(
    handler: BaseHTTPRequestHandler, app_config: Config | None, *, head_only: bool = False
) -> None:
    """The web surface's one live device-read route (contracts/router-detail-api.md).

    Performs a READ-ONLY live query (``/system/resource`` + ``/interface`` + ``/ip/address``) of a
    single RouterOS device on operator request, reusing the core ``router_detail`` rails (roster
    resolution, read-only ``print`` reads, redaction, audit, timeout + record cap). A missing
    ``device`` is a 400; every other outcome — no driver configured, unknown device, unreachable —
    is a 200 with an honest ``store``/``read.outcome`` (the data_api idiom). LAN-only, read-only
    (constitution 1.8.0).
    """

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    device = _first(params, "device")
    if device is None:
        _send_json(handler, 400, {"error": "device is required"}, head_only=head_only)
        return

    def _absent_router(hint: str) -> dict[str, Any]:
        return {
            "store": "absent",
            "hint": hint,
            "device": None,
            "read": {
                "outcome": "unknown_device",
                "detail": hint,
                "read_at": _now(UTC).isoformat(),
            },
            "interfaces": [],
            "addresses": [],
            "truncated": False,
        }

    if app_config is None or app_config.routeros is None:
        _send_json(
            handler,
            200,
            _absent_router(
                "RouterOS not configured — add a [routeros] section to query a router live"
            ),
            head_only=head_only,
        )
        return

    from obi.core.audit import AuditTrail, audit_scope, recorder_from_config
    from obi.core.drivers.mikrotik import MikrotikDriver
    from obi.core.engine import _known_secrets, router_detail
    from obi.core.inventory import load as load_inventory
    from obi.core.serialize import router_detail_to_dict

    inventory = load_inventory(app_config.inventory_path)
    if inventory is None:
        _send_json(
            handler,
            200,
            _absent_router("no saved inventory yet — run: obi discover"),
            head_only=head_only,
        )
        return

    driver = MikrotikDriver(app_config.routeros)
    live = app_config.routeros_live
    audit = AuditTrail(app_config.audit_path)
    with audit_scope(recorder_from_config(app_config), source="web", session_id=None):
        detail = router_detail(
            inventory,
            driver,
            device,
            timeout=live.timeout_s or app_config.timeout,
            max_records=live.max_records,
            audit=audit,
            extra_secrets=_known_secrets(app_config),
        )
    payload = router_detail_to_dict(detail)
    payload.update({"store": "ok", "hint": None})
    _send_json(handler, 200, payload, head_only=head_only)


# --- GET /api/phy.json (feature 075): one read-only live RouterOS PHYSICAL drill-down ------------


def _phy_metrics_facts(
    app_config: Config, metrics_settings: SnmpHistorySettings | None, identity: str | None
) -> dict[str, Any]:
    """The independent metrics envelope: interface error-counter increases from the local 021
    store (``metricstore.counter_increases``), joined to THIS device by identity (D12) and filtered
    by the ignore list. Detection params reuse ``[watch.metrics]`` when configured, else its
    defaults (D3). Never degrades the live view — a missing/unreadable store is ``store:"absent"``.
    """

    import sqlite3

    from obi.core import metricstore
    from obi.core.config import WatchMetricsSettings

    absent = {"store": "absent", "window_seconds": 0, "error_increases": []}
    if metrics_settings is None or not metrics_settings.db_path.exists():
        return absent

    watch_metrics = app_config.watch.metrics or WatchMetricsSettings()
    until = _now(UTC)
    since = until - timedelta(seconds=watch_metrics.window_seconds)
    try:
        conn = metricstore.connect_readonly(metrics_settings.db_path)
        try:
            increases = metricstore.counter_increases(
                conn,
                metrics=watch_metrics.counters,
                since_us=int(since.timestamp() * 1_000_000),
                until_us=int(until.timestamp() * 1_000_000),
                min_increase=watch_metrics.threshold,
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return absent  # store locked, mid-write, or lacks the samples table — degrade, never crash

    ignore = [pattern.lower() for pattern in watch_metrics.ignore_interfaces]
    facts = [
        {"ifname": inc.ifname, "metric": inc.metric, "increase": inc.increase}
        for inc in increases
        if (identity is None or inc.device == identity)
        and not any(pattern in inc.ifname.lower() for pattern in ignore)
    ]
    return {
        "store": "ok",
        "window_seconds": watch_metrics.window_seconds,
        "error_increases": facts,
    }


def handle_phy_request(
    handler: BaseHTTPRequestHandler,
    app_config: Config | None,
    metrics_settings: SnmpHistorySettings | None,
    *,
    head_only: bool = False,
) -> None:
    """The web surface's second live device-read route (contracts/phy-api.md).

    Performs a READ-ONLY live PHYSICAL query of a single RouterOS device on operator request,
    reusing the core ``router_phy`` rails (roster resolution, read-only ``print`` + ``monitor …
    once`` reads, redaction, audit, timeout + record cap), and composes an independent ``metrics``
    envelope from the local 021 store. A missing ``device`` is a 400; every other outcome — no
    driver configured, no inventory, unknown device, unreachable — is a 200 with an honest
    ``store``/``read.outcome`` (the data_api idiom). LAN-only, read-only (constitution 1.11.0).
    """

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    device = _first(params, "device")
    if device is None:
        _send_json(handler, 400, {"error": "device is required"}, head_only=head_only)
        return

    def _absent_phy(hint: str) -> dict[str, Any]:
        return {
            "store": "absent",
            "hint": hint,
            "device": None,
            "read": {
                "outcome": "unknown_device",
                "detail": hint,
                "read_at": _now(UTC).isoformat(),
            },
            "ports": [],
            "sections": {},
            "truncated": False,
            "metrics": {"store": "absent", "window_seconds": 0, "error_increases": []},
        }

    if app_config is None or app_config.routeros is None:
        _send_json(
            handler,
            200,
            _absent_phy(
                "RouterOS not configured — add a [routeros] section to query a router live"
            ),
            head_only=head_only,
        )
        return

    from obi.core.audit import AuditTrail, audit_scope, recorder_from_config
    from obi.core.drivers.mikrotik import MikrotikDriver
    from obi.core.engine import _known_secrets, router_phy
    from obi.core.inventory import load as load_inventory
    from obi.core.serialize import router_phy_to_dict

    inventory = load_inventory(app_config.inventory_path)
    if inventory is None:
        _send_json(
            handler,
            200,
            _absent_phy("no saved inventory yet — run: obi discover"),
            head_only=head_only,
        )
        return

    driver = MikrotikDriver(app_config.routeros)
    live = app_config.routeros_live
    audit = AuditTrail(app_config.audit_path)
    with audit_scope(recorder_from_config(app_config), source="web", session_id=None):
        phy = router_phy(
            inventory,
            driver,
            device,
            timeout=live.timeout_s or app_config.timeout,
            max_records=live.max_records,
            audit=audit,
            extra_secrets=_known_secrets(app_config),
        )
    payload = router_phy_to_dict(phy)
    identity = phy.device.identity if phy.device else None
    payload["metrics"] = _phy_metrics_facts(app_config, metrics_settings, identity)
    payload.update({"store": "ok", "hint": None})
    _send_json(handler, 200, payload, head_only=head_only)


# --- GET /api/audit.json (feature 042): the audit event store -----------------------------------


def _audit_when_param(raw: str | None) -> datetime | None:
    from obi.core.logstore import _RELATIVE_RE, parse_when

    if not raw:
        return None
    parsed = parse_when(raw, UTC)
    if parsed is not None and _NOW_OVERRIDE is not None and _RELATIVE_RE.fullmatch(raw.lower()):
        # A relative window ("24h") is relative to the frozen clock (feature 136 export).
        parsed += _now(UTC) - datetime.now(UTC)
    return parsed


def handle_audit_request(
    handler: BaseHTTPRequestHandler,
    audit_settings: AuditSettings | None,
    *,
    head_only: bool = False,
) -> None:
    """Serve one ``GET/HEAD /api/audit.json`` per contracts/audit-web-api.md.

    ``view`` selects the shape: ``list`` (default, filterable event rows), ``detail``
    (one event's full payload), ``trace`` (a session in causal order), ``stats``
    (aggregates + token series). Read-only over the local store; per the FR-012
    exemption these reads are never themselves audited. An absent store is a state,
    not an error (HTTP 200 with ``store.present = false``).
    """

    from obi.core import auditstore

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    view = _first(params, "view") or "list"
    if view not in ("list", "detail", "trace", "stats"):
        _send_json(handler, 400, {"error": f"unknown view {view!r}"}, head_only=head_only)
        return

    db_path = audit_settings.db_path if audit_settings is not None else None
    present = db_path is not None and db_path.exists()
    store_info = {
        "present": present,
        "enabled": bool(audit_settings.enabled) if audit_settings is not None else False,
    }
    if not present:
        _send_json(handler, 200, {
            "events": [], "total": 0, "store": store_info,
            "hint": "no audit events yet — any chat turn, watch cycle, or ingest records them",
        }, head_only=head_only)
        return
    assert db_path is not None  # narrowed by `present`

    try:
        if view == "detail":
            raw_id = _first(params, "id")
            if raw_id is None or not raw_id.isdigit():
                raise _BadRequest(f"detail needs a numeric id (got {raw_id!r})")
            conn = auditstore.connect_readonly(db_path)
            try:
                event = auditstore.get_event(conn, int(raw_id))
            finally:
                conn.close()
            if event is None:
                _send_json(handler, 404, {"error": f"no audit event with id {raw_id}"},
                           head_only=head_only)
                return
            _send_json(handler, 200, {"event": event, "store": store_info},
                       head_only=head_only)
            return

        if view == "trace":
            session = _first(params, "session")
            if not session:
                raise _BadRequest("trace needs a session parameter")
            conn = auditstore.connect_readonly(db_path)
            try:
                events = auditstore.session_trace(conn, session)
            finally:
                conn.close()
            _send_json(handler, 200, {"session": session, "events": events,
                                      "store": store_info}, head_only=head_only)
            return

        if view == "stats":
            raw_buckets = _first(params, "buckets")
            buckets = 48
            if raw_buckets is not None:
                if not raw_buckets.isdigit() or not 1 <= int(raw_buckets) <= 500:
                    raise _BadRequest(f"buckets must be 1..500 (got {raw_buckets!r})")
                buckets = int(raw_buckets)
            conn = auditstore.connect_readonly(db_path)
            try:
                result = auditstore.stats(
                    conn,
                    since=_audit_when_param(_first(params, "since") or "24h"),
                    until=_audit_when_param(_first(params, "until")),
                    buckets=buckets,
                )
            finally:
                conn.close()
            result["store"] = store_info
            _send_json(handler, 200, result, head_only=head_only)
            return

        # view == "list"
        raw_limit = _first(params, "limit")
        limit = 100
        if raw_limit is not None:
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 1000:
                raise _BadRequest(f"limit must be 1..1000 (got {raw_limit!r})")
            limit = int(raw_limit)
        kinds_raw = _first(params, "kind")
        kinds = [k for k in (kinds_raw or "").split(",") if k] or None
        if kinds and any(k not in auditstore.KINDS for k in kinds):
            raise _BadRequest(f"kind must be among {', '.join(auditstore.KINDS)}")
        outcome = _first(params, "outcome")
        if outcome is not None and outcome not in ("ok", "error"):
            raise _BadRequest(f"outcome must be ok or error (got {outcome!r})")
        sources_raw = _first(params, "source")
        sources = [s for s in (sources_raw or "").split(",") if s] or None
        conn = auditstore.connect_readonly(db_path)
        try:
            events, total = auditstore.query_events(
                conn,
                since=_audit_when_param(_first(params, "since")),
                until=_audit_when_param(_first(params, "until")),
                kinds=kinds,
                sources=sources,
                session_id=_first(params, "session"),
                target=_first(params, "target"),
                outcome=outcome,
                text=_first(params, "text"),
                limit=limit,
            )
        finally:
            conn.close()
        _send_json(handler, 200, {"events": events, "total": total, "store": store_info,
                                  "hint": None}, head_only=head_only)
    except _BadRequest as exc:
        _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)


# --- GET /api/health.json (feature 096): fleet hardware health, local SQLite only --------------

_HEALTH_RANK = {"down": 0, "warn": 1, "stale": 2, "ok": 3}
# A status/fan breach is a hard hardware failure — ranked (and colored) "down".
_HEALTH_HARD_CLASSES = {"status", "fan"}
_HEALTH_ABSENT_NOTE = (
    "no health readings yet — run: obi metrics ingest (RouterOS) and "
    "obi health probe (collector VMs)"
)


def handle_health_request(
    handler: BaseHTTPRequestHandler, app_config: Config | None, *, head_only: bool = False
) -> None:
    """Fleet mode: newest reading per health series → per-device worst-first summaries.
    ``?device=`` mode: that device's readings + recent bucketed series for the HEALTH tab.

    Local store only — never a device. Thresholds come from ``[watch.health]`` (or the
    shipped defaults) through the SAME core evaluation the watch pass runs, so the board
    and the alerts cannot disagree (feature 096, contracts/health-api.md)."""

    from obi.core import health as health_core
    from obi.core import metricstore
    from obi.core.config import default_metrics_db_path
    from obi.core.serialize import health_breach_to_dict

    snmp = app_config.snmp if app_config is not None else None
    history = snmp.history if snmp is not None else None
    db_path = history.db_path if history is not None else default_metrics_db_path()
    if not db_path.exists():
        _send_json(
            handler,
            200,
            {"store": "absent", "devices": [], "hint": _HEALTH_ABSENT_NOTE},
            head_only=head_only,
        )
        return

    params = parse_qs(urlsplit(handler.path).query, keep_blank_values=False)
    device = _first(params, "device")
    now = _now(UTC)
    now_us = int(now.timestamp() * 1_000_000)
    thresholds = health_core.thresholds_from_settings(
        app_config.watch.health if app_config is not None else None
    )

    try:
        conn = metricstore.connect_readonly(db_path)
    except metricstore.StoreError as exc:
        _send_json(
            handler,
            200,
            {"store": "absent", "devices": [], "hint": f"health store unreadable: {exc}"},
            head_only=head_only,
        )
        return
    try:
        fleet = health_core.fleet_status(conn, now_us=now_us, device=device)

        if device is not None:
            if not fleet:
                _send_json(
                    handler,
                    200,
                    {"store": "ok", "device": device, "readings": [], "series": [],
                     "breaches": [], "note": "no health samples"},
                    head_only=head_only,
                )
                return
            dev = fleet[0]
            try:
                window = _parse_window(_first(params, "window") or "24h")
            except _BadRequest as exc:
                _send_json(handler, 400, {"error": str(exc)}, head_only=head_only)
                return
            since_us = now_us - int(window.total_seconds() * 1_000_000)
            breaches, _tracked = health_core.evaluate_device(dev, thresholds)
            series_payload = []
            for metric in sorted({r.metric for r in dev.readings}):
                for series in metricstore.query_series(
                    conn, device=device, metric=metric,
                    since_us=since_us, until_us=now_us, buckets=60,
                ):
                    series_payload.append({
                        "metric": series.metric,
                        "ifname": series.ifname,
                        "unit": series.unit,
                        "buckets": [
                            {"start": b.start_iso, "avg": b.avg, "min": b.min,
                             "max": b.max, "n": b.n}
                            for b in series.buckets
                        ],
                    })
            _send_json(
                handler,
                200,
                {
                    "store": "ok",
                    "device": dev.device,
                    "kind": dev.kind,
                    "stale": dev.stale,
                    "age_seconds": max(0, (now_us - dev.newest_us) // 1_000_000),
                    "readings": [
                        {"metric": r.metric, "instance": r.instance, "label": r.label,
                         "value": r.value, "unit": r.unit}
                        for r in dev.readings
                    ],
                    "breaches": [health_breach_to_dict(b) for b in breaches],
                    "series": series_payload,
                    "note": None,
                },
                head_only=head_only,
            )
            return

        devices_payload = []
        for dev in fleet:
            breaches, _tracked = health_core.evaluate_device(dev, thresholds)
            summary = health_core.summarize(dev)
            if breaches:
                hard = any(b.klass in _HEALTH_HARD_CLASSES for b in breaches)
                worst = "down" if hard else "warn"
            elif dev.stale:
                worst = "stale"
            else:
                worst = "ok"
            summary_dict: dict[str, object] = {
                key: value
                for key, value in {
                    "temperature_c": summary.temperature_c,
                    "cpu_percent": summary.cpu_percent,
                    "memory_percent": summary.memory_percent,
                    "rx_power_min_dbm": summary.rx_power_min_dbm,
                    "disk_max_percent": summary.disk_max_percent,
                }.items()
                if value is not None  # absence stays absence (FR-003) — no fabricated keys
            }
            if summary.state_alerts:
                summary_dict["state_alerts"] = summary.state_alerts
            devices_payload.append({
                "device": dev.device,
                "address": dev.address,
                "kind": dev.kind,
                "worst": worst,
                "stale": dev.stale,
                "age_seconds": max(0, (now_us - dev.newest_us) // 1_000_000),
                "summary": summary_dict,
                "breaches": [health_breach_to_dict(b) for b in breaches],
            })
        devices_payload.sort(key=lambda d: (_HEALTH_RANK[str(d["worst"])], d["device"]))
        _send_json(
            handler,
            200,
            {"store": "ok", "generated_at": now.isoformat(), "devices": devices_payload,
             "note": None},
            head_only=head_only,
        )
    finally:
        conn.close()
