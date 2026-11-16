"""Contract tests: GET /api/logs.json — the first local-data API route (feature 038).

Real behavioral tests against the real TLS server and a real store file built with the
production ingest code (constitution II: fixtures from real behavior, captured read-only).
Every assertion pins a sentence of specs/038-web-data-widgets/contracts/logs-api.md.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from obi.core import logstore
from obi.core.config import LogsSettings
from obi.core.models import LogEvent
from obi.web.server import create_server

TZ = ZoneInfo("America/Los_Angeles")


def _event(
    minutes_ago: int,
    *,
    host: str = "core-gw-1",
    severity: str | None = "info",
    message: str = "link up",
    collector: str = "core",
) -> LogEvent:
    return LogEvent(
        ts=datetime.now(TZ) - timedelta(minutes=minutes_ago),
        host=host,
        topics=f"wireless,{severity}" if severity else "wireless",
        message=message,
        subsystem="wireless",
        severity=severity,
        raw_line=f"raw -{minutes_ago}m",
        collectors=[collector],
    )


def _seed_store(db_path: Path) -> None:
    conn = logstore.connect(db_path)
    events = [
        _event(50, severity="debug", message="debug noise"),
        _event(40, severity="info", message="login ok"),
        _event(30, severity="warning", message="signal drop", host="tamarack-gw"),
        _event(20, severity="error", message="link down", host="tamarack-gw"),
        _event(10, severity=None, message="uisp outage note", collector="uisp"),
        _event(5, severity="info", message="client join", collector="unifi", host="yew-ap"),
        _event(0, severity="critical", message="power fail"),
    ]
    logstore.ingest_events(conn, [(e, f"k{i}") for i, e in enumerate(events)])
    conn.commit()
    conn.close()


def _serve(tmp_path: Path, logs_settings: LogsSettings | None) -> Iterator[tuple[str, Path]]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        logs_settings=logs_settings,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    db_path = tmp_path / "logs.db"
    _seed_store(db_path)
    settings = LogsSettings(db_path=db_path, log_tz="America/Los_Angeles")
    yield from _serve(tmp_path, settings)


@pytest.fixture()
def storeless(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    settings = LogsSettings(db_path=tmp_path / "never-created.db")
    yield from _serve(tmp_path, settings)


def _get(base: str, query: str = "") -> httpx.Response:
    return httpx.get(f"{base}/api/logs.json{query}", verify=False, timeout=5.0)


# --- initial mode ------------------------------------------------------------------------------


def test_initial_page_is_ascending_with_a_cursor(served: tuple[str, Path]) -> None:
    base, _tmp = served
    response = _get(base)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["store"] == "ok" and payload["mode"] == "initial"
    timestamps = [e["timestamp"] for e in payload["entries"]]
    assert timestamps == sorted(timestamps)  # ascending — a feed appends
    assert payload["entries"][-1]["message"] == "power fail"  # newest last
    assert isinstance(payload["cursor"], int) and payload["cursor"] >= len(timestamps)
    assert payload["hint"] is None


def test_entry_shape_matches_the_cli_serializer(served: tuple[str, Path]) -> None:
    """One dict shape for `obi logs query --json` and the web — no drift."""

    base, _tmp = served
    entry = _get(base).json()["entries"][-1]
    assert set(entry) == {
        "timestamp", "host", "subsystem", "topics", "severity", "message", "collectors"}


def test_null_severity_rows_are_served(served: tuple[str, Path]) -> None:
    base, _tmp = served
    severities = [e["severity"] for e in _get(base).json()["entries"]]
    assert None in severities  # rendered neutrally client-side, never dropped


def test_filters_combine(served: tuple[str, Path]) -> None:
    base, _tmp = served
    payload = _get(base, "?device=tamarack&severity=warning").json()
    assert [(e["host"], e["severity"]) for e in payload["entries"]] == [
        ("tamarack-gw", "warning"), ("tamarack-gw", "error")]


def test_source_filter_matches_collectors(served: tuple[str, Path]) -> None:
    base, _tmp = served
    assert [e["message"] for e in _get(base, "?source=unifi").json()["entries"]] == [
        "client join"]
    assert [e["message"] for e in _get(base, "?source=uisp").json()["entries"]] == [
        "uisp outage note"]


def test_source_syslog_returns_only_device_syslog(served: tuple[str, Path]) -> None:
    """feature 050: the reserved `syslog` family = device-syslog rows only (collector = a hostname),
    never the `unifi`/`uisp` controller rows — same selection as the LOGS widget's syslog option."""

    base, _tmp = served
    entries = _get(base, "?source=syslog").json()["entries"]
    messages = {e["message"] for e in entries}
    assert messages == {"debug noise", "login ok", "signal drop", "link down", "power fail"}
    assert "client join" not in messages and "uisp outage note" not in messages
    # every returned row carries a hostname collector, no controller tag
    collectors = {c for e in entries for c in e["collectors"]}
    assert collectors == {"core"} and "unifi" not in collectors and "uisp" not in collectors


def test_free_text_is_literal(served: tuple[str, Path]) -> None:
    base, _tmp = served
    assert [e["message"] for e in _get(base, "?q=link+down").json()["entries"]] == ["link down"]
    assert _get(base, '?q=link+AND+(').json()["entries"] == []  # FTS syntax neutralized


def test_window_bounds_the_initial_page(served: tuple[str, Path]) -> None:
    base, _tmp = served
    shallow = _get(base, "?window=15m").json()["entries"]
    assert {e["message"] for e in shallow} == {"uisp outage note", "client join", "power fail"}


def test_limit_keeps_the_newest(served: tuple[str, Path]) -> None:
    base, _tmp = served
    entries = _get(base, "?limit=2").json()["entries"]
    assert [e["message"] for e in entries] == ["client join", "power fail"]  # newest 2, ascending


# --- tail mode ---------------------------------------------------------------------------------


def test_tail_returns_only_the_delta(served: tuple[str, Path]) -> None:
    base, tmp = served
    first = _get(base).json()
    cursor = first["cursor"]
    empty = _get(base, f"?after={cursor}").json()
    assert empty["mode"] == "tail"
    assert empty["entries"] == []
    assert empty["cursor"] == cursor  # echoed when nothing new

    conn = logstore.connect(tmp / "logs.db")
    logstore.ingest_events(conn, [(_event(0, message="fresh line"), "fresh-1")])
    conn.commit()
    conn.close()

    delta = _get(base, f"?after={cursor}").json()
    assert [e["message"] for e in delta["entries"]] == ["fresh line"]
    assert delta["cursor"] == cursor + 1
    assert _get(base, f"?after={delta['cursor']}").json()["entries"] == []


def test_tail_respects_filters(served: tuple[str, Path]) -> None:
    base, _tmp = served
    filtered = _get(base, "?after=0&severity=critical").json()
    assert [e["message"] for e in filtered["entries"]] == ["power fail"]
    # cursor still advances to the last MATCHED id — polling stays cheap under filters
    assert isinstance(filtered["cursor"], int)


def test_writer_coexistence(served: tuple[str, Path]) -> None:
    """A browser read never blocks the ingest: write while the route is being used."""

    base, tmp = served
    assert _get(base).status_code == 200
    conn = logstore.connect(tmp / "logs.db")  # the ingest's read-write connection
    logstore.ingest_events(conn, [(_event(0, message="written mid-read"), "coexist-1")])
    conn.commit()
    assert _get(base, "?q=written+mid-read").json()["entries"] != []
    conn.close()


# --- validation --------------------------------------------------------------------------------


def test_bad_params_are_400_with_a_reason(served: tuple[str, Path]) -> None:
    base, _tmp = served
    for query in ("?severity=loud", "?window=nope", "?limit=0", "?limit=-3",
                  "?limit=99999", "?after=abc"):
        response = _get(base, query)
        assert response.status_code == 400, query
        assert response.json()["error"], query


def test_unknown_params_are_ignored(served: tuple[str, Path]) -> None:
    base, _tmp = served
    assert _get(base, "?frobnicate=1&limit=3").status_code == 200


def test_route_is_get_head_only(served: tuple[str, Path]) -> None:
    base, _tmp = served
    assert httpx.post(f"{base}/api/logs.json", verify=False, timeout=5.0).status_code == 405
    head = httpx.head(f"{base}/api/logs.json", verify=False, timeout=5.0)
    assert head.status_code == 200 and head.content == b""


def test_other_api_paths_stay_404(served: tuple[str, Path]) -> None:
    base, _tmp = served
    for path in ("/api/other.json", "/api/", "/api/logs", "/api/logs.json/../secret"):
        assert httpx.get(f"{base}{path}", verify=False, timeout=5.0).status_code == 404, path


# --- absent store ------------------------------------------------------------------------------


def test_absent_store_is_a_state_not_an_error(storeless: tuple[str, Path]) -> None:
    base, _tmp = storeless
    payload = _get(base).json()
    assert payload["store"] == "absent"
    assert payload["entries"] == [] and payload["cursor"] is None
    assert "obi logs ingest" in payload["hint"]


def test_unwired_logs_settings_also_answer(tmp_path: Path) -> None:
    """A server built without logs_settings (no [logs] config) still answers the route."""

    for base, _tmp in _serve(tmp_path, None):
        payload = _get(base).json()
        assert payload["store"] == "absent"
        assert "[logs]" in payload["hint"]


# --- GET /api/watch.json (feature 040) ---------------------------------------------------------


def _write_finding(watch_dir: Path, *, fid: str, device: str, severity: str,
                   minutes_ago: int, summary: str) -> None:
    from obi.core.watch import WatchFinding
    from obi.core.watchstore import WatchStore

    now = datetime.now(UTC)
    store = WatchStore(watch_dir)
    store.write_finding(WatchFinding(
        id=fid,
        created_at=now - timedelta(minutes=minutes_ago),
        device=device,
        window_start=now - timedelta(minutes=minutes_ago + 5),
        window_end=now - timedelta(minutes=minutes_ago),
        severity=severity,
        confidence="high",
        summary=summary,
        description="d",
        evidence=[],
        source="log",
    ))


def _serve_with(tmp_path: Path, **kwargs: object) -> Iterator[tuple[str, Path]]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export_path, state_dir=tmp_path / "state",
        **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def test_watch_route_serves_findings_newest_first(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    _write_finding(watch_dir, fid="a", device="core-gw-1", severity="warning",
                   minutes_ago=90, summary="older")
    _write_finding(watch_dir, fid="b", device="tamarack-gw", severity="critical",
                   minutes_ago=5, summary="newer")
    for base, _tmp in _serve_with(tmp_path, watch_dir=watch_dir):
        payload = httpx.get(f"{base}/api/watch.json", verify=False, timeout=5.0).json()
        assert payload["store"] == "ok"
        assert [f["summary"] for f in payload["findings"]] == ["newer", "older"]
        # filters: device substring + exact severity + window
        by_dev = httpx.get(f"{base}/api/watch.json?device=tamarack",
                           verify=False, timeout=5.0).json()
        assert [f["id"] for f in by_dev["findings"]] == ["b"]
        by_sev = httpx.get(f"{base}/api/watch.json?severity=warning",
                           verify=False, timeout=5.0).json()
        assert [f["id"] for f in by_sev["findings"]] == ["a"]
        recent = httpx.get(f"{base}/api/watch.json?window=30m",
                           verify=False, timeout=5.0).json()
        assert [f["id"] for f in recent["findings"]] == ["b"]


def test_watch_route_renders_timestamps_in_log_tz(tmp_path: Path) -> None:
    """069: the served created_at/window carry the operator's log_tz offset, so the browser (which
    strips the offset and shows the wall-clock) reads local time, not UTC."""
    from obi.core.watch import WatchFinding
    from obi.core.watchstore import WatchStore

    watch_dir = tmp_path / "watch"
    detected = datetime(2026, 7, 13, 19, 4, 1, tzinfo=UTC)  # 12:04:01 PDT
    WatchStore(watch_dir).write_finding(WatchFinding(
        id="tz", created_at=detected, device="chestnut-st-housing-505-05",
        window_start=detected, window_end=detected, severity="warning", confidence="high",
        summary="outage cleared", description="d", evidence=[], source="llm",
    ))
    settings = LogsSettings(db_path=tmp_path / "logs.db", log_tz="America/Los_Angeles")
    # the fixed instant pins a hand-checked PDT wall clock, so ask for a window that still reaches
    # back to it rather than letting the route's 24h default age the fixture out.
    window = f"{max(1, (datetime.now(UTC) - detected).days + 1)}d"
    for base, _tmp in _serve_with(tmp_path, watch_dir=watch_dir, logs_settings=settings):
        payload = httpx.get(f"{base}/api/watch.json?window={window}",
                            verify=False, timeout=5.0).json()
        created = payload["findings"][0]["created_at"]
        assert created == "2026-07-13T12:04:01-07:00"  # local, not 19:04:01+00:00


def test_watch_route_validates_and_degrades(tmp_path: Path) -> None:
    for base, _tmp in _serve_with(tmp_path, watch_dir=tmp_path / "never"):
        absent = httpx.get(f"{base}/api/watch.json", verify=False, timeout=5.0).json()
        assert absent["store"] == "absent" and "watch" in absent["hint"]
        assert httpx.get(f"{base}/api/watch.json?window=x",
                         verify=False, timeout=5.0).status_code == 400
        assert httpx.get(f"{base}/api/watch.json?limit=0",
                         verify=False, timeout=5.0).status_code == 400
        assert httpx.post(f"{base}/api/watch.json",
                          verify=False, timeout=5.0).status_code == 405


# --- GET /api/inventory.json (feature 040) ------------------------------------------------------


def test_inventory_route_serves_the_saved_roster(tmp_path: Path) -> None:
    from obi.core.inventory import InventoryEntry
    from obi.core.models import DiscoveryState
    from tests.fakes import save_inventory

    inventory_path = tmp_path / "inventory.json"
    save_inventory(inventory_path, [
        InventoryEntry("172.25.110.1", "core-gw-1", DiscoveryState.IDENTIFIED, provenance="seed"),
    ])
    for base, _tmp in _serve_with(tmp_path, inventory_path=inventory_path):
        payload = httpx.get(f"{base}/api/inventory.json", verify=False, timeout=5.0).json()
        assert payload["store"] == "ok"
        assert [e["identity"] for e in payload["inventory"]["entries"]] == ["core-gw-1"]


def test_inventory_route_absent_roster_hints_discover(tmp_path: Path) -> None:
    for base, _tmp in _serve_with(tmp_path, inventory_path=tmp_path / "never.json"):
        payload = httpx.get(f"{base}/api/inventory.json", verify=False, timeout=5.0).json()
        assert payload["store"] == "absent"
        assert "obi discover" in payload["hint"]


# --- GET /api/metrics.json (feature 041) --------------------------------------------------------


def _seed_metrics(db_path: Path) -> None:
    from obi.core import metricstore
    from obi.core.metricstore import StoredSample

    conn = metricstore.connect(db_path)
    base = datetime.now(UTC) - timedelta(hours=2)
    rows = []
    for i in range(8):
        ts = base + timedelta(minutes=5 * i)
        rows.append(StoredSample(
            ts_us=int(ts.timestamp() * 1_000_000), ts_iso=ts.isoformat(),
            device="core-gw-1", address="172.25.110.1", profile="interfaces",
            metric="ifHCInOctets", ifindex="1", ifname="ether1",
            kind="counter64", value=1_000_000 * i,
        ))
    metricstore.insert_samples(conn, rows)
    conn.commit()
    conn.close()


def test_metrics_route_coverage_and_series(tmp_path: Path) -> None:
    from obi.core.config import SnmpHistorySettings

    db_path = tmp_path / "metrics.db"
    _seed_metrics(db_path)
    settings = SnmpHistorySettings(db_path=db_path)
    for base, _tmp in _serve_with(tmp_path, metrics_settings=settings):
        coverage = httpx.get(f"{base}/api/metrics.json", verify=False, timeout=5.0).json()
        assert coverage["store"] == "ok" and coverage["mode"] == "coverage"
        assert [d["device"] for d in coverage["devices"]] == ["core-gw-1"]
        # Names only, by design: the counted view (`total_readings`, per-device `readings`,
        # `oldest`/`newest`) needs a full-table scan per figure — ~60 s on the live store, for
        # numbers no browser code reads. Regression guard; `obi metrics status` keeps the counts.
        assert "total_readings" not in coverage and "oldest" not in coverage
        assert all(set(d) == {"device"} for d in coverage["devices"])

        series = httpx.get(
            f"{base}/api/metrics.json?target=core-gw-1&metric=ifHCInOctets&window=6h",
            verify=False, timeout=5.0,
        ).json()
        assert series["mode"] == "series" and series["metric"] == "ifHCInOctets"
        assert [s["ifname"] for s in series["series"]] == ["ether1"]
        the_series = series["series"][0]
        assert the_series["is_rate"] is True and the_series["unit"] == "bytes/s"
        rates = [b["avg"] for b in the_series["buckets"] if b["n"] > 0]
        # 1 MB per 5 min ≈ 3333 B/s — the store's reset-safe rate math, served verbatim
        assert rates and all(3000 < r < 3700 for r in rates)

        assert httpx.get(f"{base}/api/metrics.json?target=x",
                         verify=False, timeout=5.0).status_code == 400  # metric required
        assert httpx.get(f"{base}/api/metrics.json?target=x&metric=m&window=zz",
                         verify=False, timeout=5.0).status_code == 400


def test_metrics_route_absent_store(tmp_path: Path) -> None:
    for base, _tmp in _serve_with(tmp_path):
        payload = httpx.get(f"{base}/api/metrics.json", verify=False, timeout=5.0).json()
        assert payload["store"] == "absent" and "snmp.history" in payload["hint"]


# --- GET /api/configs.json (feature 041) --------------------------------------------------------


def _configs_config(tmp_path: Path):
    from obi.core import engine
    from obi.core.audit import AuditTrail
    from obi.core.config import Config, DiscoveryScope
    from obi.core.drivers.routeros_config import ExportResult
    from obi.core.inventory import Inventory, InventoryEntry
    from obi.core.inventory import save as save_inv
    from obi.core.models import CaptureOutcome, DiscoveryState
    from tests.fakes import config_backup_settings
    from tests.fakes import settings as mikrotik_settings

    inv_path = tmp_path / "inventory.json"
    save_inv(Inventory(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        entries=[InventoryEntry("172.25.110.1", "core-gw-1", DiscoveryState.IDENTIFIED,
                                present_in_latest=True)],
    ), inv_path)
    config = Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        routeros=mikrotik_settings(),
        configs=config_backup_settings(tmp_path),
        inventory_path=inv_path,
        audit_path=tmp_path / "audit.jsonl",
    )

    class _Driver:
        def __init__(self, content: str) -> None:
            self.content = content

        def fetch_export(self, address: str, *, timeout: float) -> ExportResult:
            return ExportResult(address, CaptureOutcome.CAPTURED_CHANGED, content=self.content)

    audit = AuditTrail(config.audit_path)
    engine.configs_ingest(
        config, audit=audit,
        driver=_Driver("/ip address\nadd address=172.25.110.1/24\n"),
        now=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
    )
    engine.configs_ingest(
        config, audit=audit,
        driver=_Driver("/ip address\nadd address=172.25.110.1/24\nadd address=10.9.9.9/24\n"),
        now=datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
    )
    return config


def test_configs_route_status_history_diff(tmp_path: Path) -> None:
    config = _configs_config(tmp_path)
    for base, _tmp in _serve_with(tmp_path, configs_app_config=config):
        status = httpx.get(f"{base}/api/configs.json", verify=False, timeout=5.0).json()
        assert status["store"] == "ok" and status["mode"] == "status"
        assert status["totals"]["backed_up"] == 1
        assert status["rows"][0]["identity"] == "core-gw-1"

        history = httpx.get(f"{base}/api/configs.json?device=core-gw-1",
                            verify=False, timeout=5.0).json()
        assert history["mode"] == "history" and history["found"] is True
        assert len(history["entries"]) == 2  # two ingests, two versions

        diff = httpx.get(f"{base}/api/configs.json?device=core-gw-1&view=diff",
                         verify=False, timeout=5.0).json()
        assert diff["mode"] == "diff" and diff["changed"] is True
        assert "+add address=10.9.9.9/24" in diff["diff"]

        # intent-only audit records fired for the web reads too (the 026 rule)
        records = [json.loads(line) for line in config.audit_path.read_text().splitlines()]
        assert any(r["action"] == "config_status" and "web CFG" in r["reason"]
                   for r in records)


def test_configs_route_tees_into_the_queryable_store_as_web(tmp_path: Path) -> None:
    """feature 061: CFG web reads land in the queryable audit store tagged source="web".

    Regression for a Principle V drift — the route wrote only the JSONL trail (asserted above);
    without an ambient web audit scope the tee into the SQLite store no-op'd, so config reads were
    invisible to `obi audit`/AUD and untagged, unlike the /api/router.json route. Before the fix
    this asserts zero.
    """

    import dataclasses

    from obi.core import auditstore
    from obi.core.config import AuditSettings

    audit_db = tmp_path / "audit.db"
    config = dataclasses.replace(_configs_config(tmp_path),
                                 audit=AuditSettings(db_path=audit_db))
    for base, _tmp in _serve_with(tmp_path, configs_app_config=config):
        assert httpx.get(f"{base}/api/configs.json", verify=False, timeout=5.0).status_code == 200
        assert httpx.get(f"{base}/api/configs.json?device=core-gw-1",
                         verify=False, timeout=5.0).status_code == 200

    # A missing db means nothing ever teed (the pre-fix behavior): assert cleanly rather than let
    # connect_readonly raise, so a future regression fails with a legible message.
    if audit_db.exists():
        conn = auditstore.connect_readonly(audit_db)
        try:
            web_events, total = auditstore.query_events(conn, sources=["web"], limit=100)
        finally:
            conn.close()
    else:
        web_events, total = [], 0
    assert total >= 2, "CFG web reads did not tee into the queryable audit store as source=web"
    assert web_events and all(e["source"] == "web" and e["kind"] == "action" for e in web_events)
    summaries = " ".join(e["summary"] for e in web_events)
    assert "config_status" in summaries and "config_history" in summaries


def test_configs_route_absent_config(tmp_path: Path) -> None:
    for base, _tmp in _serve_with(tmp_path):
        payload = httpx.get(f"{base}/api/configs.json", verify=False, timeout=5.0).json()
        assert payload["store"] == "absent" and "obi configs ingest" in payload["hint"]


def test_configs_route_show_full_config_at_version(tmp_path: Path) -> None:
    """feature 049: view=show returns the full stored config as of a version (the CFG full-config
    view), read-only from the local 026 git store — the current version and an older one."""
    config = _configs_config(tmp_path)
    for base, _tmp in _serve_with(tmp_path, configs_app_config=config):
        # current: the whole config, including the second address added in the newer version
        current = httpx.get(f"{base}/api/configs.json?device=core-gw-1&view=show",
                            verify=False, timeout=5.0).json()
        assert current["store"] == "ok" and current["mode"] == "show"
        assert current["found"] is True
        assert "add address=172.25.110.1/24" in current["content"]
        assert "add address=10.9.9.9/24" in current["content"]  # a full file, not a diff
        assert current["captured_at"] and not current["content"].startswith("+")

        # as of the older version (its commit sha from history) — the second address is absent
        history = httpx.get(f"{base}/api/configs.json?device=core-gw-1",
                            verify=False, timeout=5.0).json()
        older_sha = history["entries"][1]["commit"]  # newest-first; [1] is the first capture
        older = httpx.get(f"{base}/api/configs.json?device=core-gw-1&view=show&at={older_sha}",
                          verify=False, timeout=5.0).json()
        assert older["found"] is True
        assert "add address=172.25.110.1/24" in older["content"]
        assert "10.9.9.9" not in older["content"]

        # unknown device degrades to an honest not-found payload (HTTP 200), never an error
        missing = httpx.get(f"{base}/api/configs.json?device=nope&view=show",
                            verify=False, timeout=5.0)
        assert missing.status_code == 200
        body = missing.json()
        assert body["mode"] == "show" and body["found"] is False
        assert not body["content"] and body["detail"]  # empty content + an honest reason

        # the show read is intent-audited (device + at=), never the config content (the 026 rule)
        records = [json.loads(line) for line in config.audit_path.read_text().splitlines()]
        show_records = [r for r in records if r["action"] == "config_show"]
        assert show_records and all("web CFG" in r["reason"] for r in show_records)
        assert all("add address" not in json.dumps(r) for r in show_records)
