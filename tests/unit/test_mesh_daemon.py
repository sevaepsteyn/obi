"""Unit (feature 123): the mesh daemon core — ownership, sweep, live record, lease, caps.

Everything runs offline through the driver's injectable connection factory over the
2026-08-28 fixtures (the test_mesh_locator idiom). No test opens a serial port or a
socket; the RPC boundary is exercised as plain ``handle()`` calls here — the socket
transport has its own file (test_mesh_daemon_client).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from obi.core import meshstore
from obi.core.audit import AuditTrail
from obi.core.brcaddr import CityModel
from obi.core.config import MeshtasticSettings
from obi.core.meshdaemon import MeshDaemon
from tests.fixtures.brc.origin import ORIGIN

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "meshtastic"

_NOW = 1_787_983_900.0

_DIAL_ONLY_MODEL = CityModel(
    origin=ORIGIN, az12_deg=45.0, orientation_source="plaza-fit", segments=(), anchors=()
)


class _Connection:
    """The locator tests' fake, plus 123's packet subscription."""

    def __init__(self, position_reply: dict[str, Any] | None = None) -> None:
        raw = json.loads((_FIXTURES / "nodes.json").read_text())
        self.my_node_id = str(raw["myNodeId"])
        self.nodes: dict[str, dict[str, Any]] = dict(raw["nodes"])
        self.position_reply = position_reply
        self.position_requests: list[str] = []
        self.closed = 0
        self.callback: Any = None
        self.alive = True  # flip to model a dead library reader thread (Errno 6)

    def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]:
        # Deliberately keeps succeeding even when .alive is False — that IS the bug: the
        # library's node dict stays readable after its reader thread dies.
        return self.my_node_id, self.nodes

    def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
        self.position_requests.append(node_id)
        return self.position_reply

    def subscribe_packets(self, callback: Any) -> None:
        self.callback = callback

    def is_alive(self) -> bool:
        return self.alive

    def close(self) -> None:
        self.closed += 1


def _settings(tmp_path: Path, **overrides: Any) -> MeshtasticSettings:
    # TUMA is heard but positionless in the fixture; AshH carries a fix — so the
    # allowlist covers both the "tracked, no fix" and the "tracked, recordable" shapes.
    # auto_request_s=0: the 125 automatic pass is exercised by its own tests below;
    # everything else pins pre-125 daemon behavior with it off.
    defaults: dict[str, Any] = {
        "port": "/dev/cu.usbmodem-test",
        "positions_db_path": tmp_path / "positions.db",
        "daemon_socket": tmp_path / "daemon.sock",
        "report_only": ["TUMA", "AshH"],
        "auto_request_s": 0,
    }
    defaults.update(overrides)
    return MeshtasticSettings(**defaults)


class _Clock:
    def __init__(self, start: float = _NOW) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _daemon(
    tmp_path: Path,
    *,
    connection: _Connection | None = None,
    settings: MeshtasticSettings | None = None,
    factory: Any = None,
    clock: _Clock | None = None,
) -> tuple[MeshDaemon, _Connection, _Clock]:
    connection = connection or _Connection()
    clock = clock or _Clock()
    daemon = MeshDaemon(
        settings or _settings(tmp_path),
        AuditTrail(tmp_path / "audit.jsonl"),
        connection_factory=factory or (lambda port, timeout: connection),
        model_loader=lambda: _DIAL_ONLY_MODEL,
        now=clock,
        limiter_state_dir=tmp_path / "limiter",
    )
    return daemon, connection, clock


def _audit_lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _recorded_node_ids(settings: MeshtasticSettings) -> list[str]:
    try:
        conn = meshstore.connect_readonly(settings.positions_db_path)
    except meshstore.StoreError:
        return []
    try:
        rows = conn.execute("SELECT node_id FROM position_fix ORDER BY id").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


# --- attach, fence, snapshot (US1) -------------------------------------------------------


def test_attach_reads_table_and_serves_snapshot(tmp_path: Path) -> None:
    daemon, connection, _clock = _daemon(tmp_path)
    daemon._attach()
    status = daemon.status()
    assert status.state == "attached"
    assert status.port == "/dev/cu.usbmodem-test"
    assert status.snapshot_age_s == 0.0
    response = daemon.handle({"id": "1", "op": "snapshot", "source": "test"})
    assert response["ok"] is True
    node_ids = {node["node_id"] for node in response["snapshot"]["nodes"]}
    assert connection.my_node_id in node_ids
    assert response["snapshot"]["taken_at_s"] == _NOW


def test_excluded_port_refuses_and_never_opens(tmp_path: Path) -> None:
    """O2: the 121 fence holds inside the daemon — the PTT device is never opened."""

    opened: list[str] = []

    def factory(port: str, timeout: float) -> Any:
        opened.append(port)
        return _Connection()

    from dataclasses import replace

    settings = replace(_settings(tmp_path), excluded_ports=("/dev/cu.usbmodem-test",))
    daemon, _connection, _clock = _daemon(tmp_path, settings=settings, factory=factory)
    daemon._attach()
    status = daemon.status()
    assert status.state == "reconnecting"
    assert "radio station" in status.detail
    assert opened == []


def test_sweep_failure_tears_down_and_reconnects(tmp_path: Path) -> None:
    connection = _Connection()
    daemon, _c, clock = _daemon(tmp_path, connection=connection)
    daemon._attach()
    assert daemon.status().state == "attached"

    def boom() -> tuple[str, dict[str, Any]]:
        raise OSError("serial gone")

    connection.read_nodes = boom  # type: ignore[method-assign]
    daemon._sweep()
    assert daemon.status().state == "reconnecting"
    assert connection.closed == 1
    # A later maintain tick past the backoff reattaches through the factory.
    connection.read_nodes = lambda: (connection.my_node_id, connection.nodes)  # type: ignore[method-assign]
    clock.now += 10.0
    daemon._next_attach_s = 0.0
    daemon._maintain()
    assert daemon.status().state == "attached"


def test_hard_cap_on_hanging_read_reopens(tmp_path: Path) -> None:
    """O4 daemon-side: a wedged node-table read cannot freeze the daemon."""

    connection = _Connection()
    release = threading.Event()

    def hang() -> tuple[str, dict[str, Any]]:
        release.wait(5.0)
        return connection.my_node_id, connection.nodes

    settings = _settings(tmp_path, snapshot_timeout_s=0.05)
    daemon, _c, _clock = _daemon(tmp_path, connection=connection, settings=settings)
    daemon._attach()
    connection.read_nodes = hang  # type: ignore[method-assign]
    start = time.monotonic()
    daemon._sweep()
    elapsed = time.monotonic() - start
    release.set()
    assert elapsed < 2.0  # 3 × 0.05 s cap (with the 1 s floor), not 5 s of hang
    assert daemon.status().state == "reconnecting"
    assert "hard cap" in daemon.status().detail


def test_single_instance_flock(tmp_path: Path) -> None:
    """O5: a second daemon refuses to run while the first holds the lock."""

    daemon_a, _c1, _clock1 = _daemon(tmp_path)
    daemon_b, _c2, _clock2 = _daemon(tmp_path)
    state_dir = daemon_a._settings.daemon_socket.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    assert daemon_a._acquire_instance_lock(state_dir) is True
    assert daemon_b._acquire_instance_lock(state_dir) is False
    daemon_a._release_instance_lock()
    assert daemon_b._acquire_instance_lock(state_dir) is True
    daemon_b._release_instance_lock()


def test_sweep_and_callbacks_never_transmit(tmp_path: Path) -> None:
    """O3: the receive path is structurally unable to key a transmission."""

    connection = _Connection()

    def raise_on_request(node_id: str, channel_index: int) -> dict[str, Any] | None:
        raise AssertionError("the passive path transmitted")

    connection.request_position = raise_on_request  # type: ignore[method-assign]
    daemon, _c, _clock = _daemon(tmp_path, connection=connection)
    daemon._attach()
    assert connection.callback is not None
    connection.callback({"decoded": {"portnum": "POSITION_APP"}})
    daemon._maintain()  # not due; flag path
    daemon._last_record_s = 0.0
    daemon._record_asap = True
    daemon._maintain()
    assert connection.position_requests == []


def test_rpc_source_reaches_device_command_audit(tmp_path: Path) -> None:
    """O6: the daemon's serial audits say who asked, via the per-request scope."""

    # The daemon's scope factory is injectable; capture what sources it was asked to open.
    sources: list[str] = []
    import contextlib as _ctx

    def scope_factory(source: str) -> Any:
        sources.append(source)
        return _ctx.nullcontext()

    connection = _Connection(position_reply={"latitudeI": 407788431, "longitudeI": -1148232973})
    daemon = MeshDaemon(
        _settings(tmp_path),
        AuditTrail(tmp_path / "audit.jsonl"),
        connection_factory=lambda port, timeout: connection,
        model_loader=lambda: _DIAL_ONLY_MODEL,
        now=_Clock(),
        scope_factory=scope_factory,
    )
    daemon._attach()
    response = daemon.handle(
        {"id": "9", "op": "request_position", "source": "radio", "node_id": "!91378e3a"}
    )
    assert response["ok"] is True
    assert "meshd:radio" in sources


def test_blank_source_is_rejected(tmp_path: Path) -> None:
    daemon, _c, _clock = _daemon(tmp_path)
    daemon._attach()
    response = daemon.handle({"id": "2", "op": "snapshot", "source": "  "})
    assert response["ok"] is False
    assert response["detail"] == "missing source"


def test_unknown_op_is_typed_not_fatal(tmp_path: Path) -> None:
    daemon, _c, _clock = _daemon(tmp_path)
    response = daemon.handle({"id": "3", "op": "reboot", "source": "test"})
    assert response["ok"] is False
    assert "unknown op" in response["detail"]


def test_request_position_round_trip_and_busy_backstop(tmp_path: Path) -> None:
    reply = {"latitudeI": 407788431, "longitudeI": -1148232973, "time": int(_NOW) - 5}
    connection = _Connection(position_reply=reply)
    daemon, _c, _clock = _daemon(tmp_path, connection=connection)
    daemon._attach()
    response = daemon.handle(
        {"id": "4", "op": "request_position", "source": "test", "node_id": "!91378e3a"}
    )
    assert response["ok"] is True
    assert abs(response["position"]["latitude"] - 40.7788431) < 1e-9
    assert connection.position_requests == ["!91378e3a"]
    # The in-flight backstop: a second concurrent request is refused typed.
    daemon._request_in_flight = True
    busy = daemon.handle(
        {"id": "5", "op": "request_position", "source": "test", "node_id": "!91378e3a"}
    )
    assert busy["ok"] is False
    assert "in flight" in busy["detail"]


# --- live recording (US2) ----------------------------------------------------------------


def test_sweep_records_allowlisted_fixes(tmp_path: Path) -> None:
    daemon, _c, _clock = _daemon(tmp_path)
    daemon._attach()  # attach sweeps immediately
    recorded = _recorded_node_ids(daemon._settings)
    # AshH is the allowlisted node WITH a fix; TUMA is heard but positionless; the
    # un-allowlisted nodes (incl. the self node) never reach the store.
    assert set(recorded) == {"!38568530"}


def test_position_packet_triggers_prompt_record(tmp_path: Path) -> None:
    connection = _Connection()
    daemon, _c, clock = _daemon(tmp_path, connection=connection)
    daemon._attach()
    before = len(_recorded_node_ids(daemon._settings))
    # A new fix appears in the table and the packet callback fires (reader thread).
    tuma = connection.nodes["!91378e3a"]
    tuma["position"] = dict(tuma.get("position") or {})
    tuma["position"]["latitudeI"] = 407790000
    tuma["position"]["longitudeI"] = -1192150000
    tuma["position"]["time"] = int(_NOW) + 60
    assert connection.callback is not None
    connection.callback({"decoded": {"portnum": "POSITION_APP"}})
    clock.now += 3.0  # past the record debounce
    daemon._maintain()
    after = _recorded_node_ids(daemon._settings)
    assert len(after) == before + 1


def test_non_position_packets_do_not_flag(tmp_path: Path) -> None:
    daemon, connection, _clock = _daemon(tmp_path)
    daemon._attach()
    assert connection.callback is not None
    connection.callback({"decoded": {"portnum": "NODEINFO_APP"}})
    assert daemon._record_asap is False


def test_sweep_dedupes_unchanged_fixes(tmp_path: Path) -> None:
    daemon, _c, clock = _daemon(tmp_path)
    daemon._attach()
    first = _recorded_node_ids(daemon._settings)
    clock.now += daemon._settings.daemon_sweep_s + 1
    daemon._maintain()  # sweep due; table unchanged
    assert _recorded_node_ids(daemon._settings) == first


# --- standby / resume (US3) --------------------------------------------------------------


def test_standby_releases_port_and_serves_last_known(tmp_path: Path) -> None:
    daemon, connection, _clock = _daemon(tmp_path)
    daemon._attach()
    status = daemon.standby(600, source="cli:mesh")
    assert status.state == "standby"
    assert "on loan" in status.detail
    assert connection.closed == 1
    # Snapshot still answers (last-known), a request is refused typed.
    snap = daemon.handle({"id": "6", "op": "snapshot", "source": "test"})
    assert snap["ok"] is True
    refusal = daemon.handle(
        {"id": "7", "op": "request_position", "source": "test", "node_id": "!91378e3a"}
    )
    assert refusal["ok"] is False
    assert refusal["outcome"] == "no_radio"
    assert "on loan" in refusal["detail"]
    actions = [line["action"] for line in _audit_lines(tmp_path)]
    assert "mesh_daemon_standby" in actions


def test_lease_expiry_reattaches_by_itself(tmp_path: Path) -> None:
    """O7: a forgotten resume self-heals — the direct fix for the 2026-09-02 outage."""

    daemon, connection, clock = _daemon(tmp_path)
    daemon._attach()
    daemon.standby(600, source="cli:mesh")
    assert daemon.status().state == "standby"
    clock.now += 601.0
    daemon._maintain()
    assert daemon.status().state == "attached"
    actions = [line["action"] for line in _audit_lines(tmp_path)]
    assert "mesh_daemon_resume" in actions


def test_second_standby_replaces_lease(tmp_path: Path) -> None:
    daemon, _connection, clock = _daemon(tmp_path)
    daemon._attach()
    daemon.standby(600, source="cli:mesh")
    first_until = daemon.status().lease_until_s
    clock.now += 100.0
    daemon.standby(600, source="cli:mesh")
    second_until = daemon.status().lease_until_s
    assert first_until is not None and second_until is not None
    assert second_until > first_until


def test_resume_early_reattaches(tmp_path: Path) -> None:
    daemon, _connection, _clock = _daemon(tmp_path)
    daemon._attach()
    daemon.standby(600, source="cli:mesh")
    status = daemon.resume(source="cli:mesh")
    assert status.state == "attached"
    assert status.lease_until_s is None


def test_standby_lease_is_clamped(tmp_path: Path) -> None:
    daemon, _connection, clock = _daemon(tmp_path)
    daemon._attach()
    status = daemon.standby(5, source="cli:mesh")
    assert status.lease_until_s is not None
    assert status.lease_until_s - clock.now == 60  # clamped up to the minimum


# --- status mirror -----------------------------------------------------------------------


def test_status_file_mirrors_state(tmp_path: Path) -> None:
    daemon, _connection, _clock = _daemon(tmp_path)
    daemon._attach()
    path = daemon._settings.daemon_socket.parent / "daemon-status.json"
    payload = json.loads(path.read_text())
    assert payload["state"] == "attached"
    assert payload["port"] == "/dev/cu.usbmodem-test"


def test_bounded_propagates_the_ambient_audit_scope(tmp_path: Path) -> None:
    """O6 (found live, 2026-09-02): serial ops run on a worker thread — the ambient audit
    scope must ride along or the daemon's device_command rows silently vanish."""

    from obi.core.audit import audit_scope, current_scope

    daemon, _connection, _clock = _daemon(tmp_path)
    seen: list[str | None] = []

    class _Recorder:  # any non-None object opens the scope; nothing is recorded here
        pass

    def probe() -> None:
        scope = current_scope()
        seen.append(scope[1] if scope is not None else None)

    with audit_scope(_Recorder(), "meshd:test", "session-1"):  # type: ignore[arg-type]
        daemon._bounded(probe, 5.0)
    assert seen == ["meshd:test"]


def test_attach_recovers_from_an_orphaned_connection(tmp_path: Path) -> None:
    """The 2026-09-02 rollout race, made deterministic: a half-finished transition left a
    live connection while the state said reconnecting. _attach must drop the orphan and
    reopen — never fight its own exclusive lock (Errno 35 forever)."""

    open_connections: list[_Connection] = []

    def exclusive_factory(port: str, timeout: float) -> _Connection:
        if any(c.closed == 0 for c in open_connections):
            raise OSError(
                "[Errno 35] Could not exclusively lock port: Resource temporarily unavailable"
            )
        connection = _Connection()
        open_connections.append(connection)
        return connection

    daemon, _unused, _clock = _daemon(tmp_path, factory=exclusive_factory)
    daemon._attach()
    assert daemon.status().state == "attached"
    # Orphan it: the race's aftermath — connection alive, state clobbered.
    with daemon._lock:
        daemon._state = "reconnecting"
    daemon._next_attach_s = 0.0
    daemon._maintain()
    assert daemon.status().state == "attached"
    assert "Errno 35" not in daemon.status().detail


def test_standby_resume_cycles_never_self_contend(tmp_path: Path) -> None:
    """Five borrow cycles against an exclusivity-enforcing factory: with transitions
    serialized, the daemon never trips over its own held port."""

    open_connections: list[_Connection] = []

    def exclusive_factory(port: str, timeout: float) -> _Connection:
        if any(c.closed == 0 for c in open_connections):
            raise OSError("[Errno 35] Could not exclusively lock port")
        connection = _Connection()
        open_connections.append(connection)
        return connection

    daemon, _unused, clock = _daemon(tmp_path, factory=exclusive_factory)
    daemon._attach()
    for _ in range(5):
        daemon.standby(600, source="cli:mesh")
        assert daemon.status().state == "standby"
        daemon._maintain()  # a tick during standby must not attach behind the lease
        assert daemon.status().state == "standby"
        status = daemon.resume(source="cli:mesh")
        assert status.state == "attached", status.detail
        daemon._maintain()
        assert daemon.status().state == "attached"


# --- the automatic freshness pass (feature 125) ------------------------------------------


def _auto_settings(tmp_path: Path, **overrides: Any) -> MeshtasticSettings:
    """125 defaults: track TUMA (positionless in the fixture ⇒ always stale) by id."""

    defaults: dict[str, Any] = {
        "port": "/dev/cu.usbmodem-test",
        "positions_db_path": tmp_path / "positions.db",
        "daemon_socket": tmp_path / "daemon.sock",
        "report_only": ["!91378e3a"],
        "auto_request_s": 300,
    }
    defaults.update(overrides)
    return MeshtasticSettings(**defaults)


def test_auto_pass_requests_a_stale_tracked_node_once_per_interval(tmp_path: Path) -> None:
    reply = {"latitudeI": 407788431, "longitudeI": -1148232973, "time": int(_NOW)}
    connection = _Connection(position_reply=reply)
    daemon, _c, clock = _daemon(tmp_path, connection=connection, settings=_auto_settings(tmp_path))
    daemon._attach()
    assert connection.position_requests == []  # attach alone never auto-requests
    daemon._maintain()
    assert connection.position_requests == ["!91378e3a"]
    # The reply landed in the SAME store the recorder writes.
    assert "!91378e3a" in _recorded_node_ids(daemon._settings)
    rows = [r for r in _audit_lines(tmp_path) if r["action"] == "mesh_position_request"]
    assert [r["outcome"] for r in rows] == ["ok"]
    assert rows[0]["actor"] == "meshd:auto"
    # Within the interval: no re-ask, even across many ticks.
    clock.now += 30.0
    daemon._maintain()
    assert connection.position_requests == ["!91378e3a"]


def test_auto_pass_skips_a_node_whose_store_is_fresh(tmp_path: Path) -> None:
    settings = _auto_settings(tmp_path, report_only=["!38568530"])  # AshH: fix 284 s old
    daemon, connection, clock = _daemon(tmp_path, settings=settings)
    daemon._attach()  # records AshH's fixture fix (age 284 s < 300 s)
    clock.now += 1.0
    daemon._maintain()
    assert connection.position_requests == []


def test_auto_pass_retries_an_unanswered_node_only_after_another_interval(
    tmp_path: Path,
) -> None:
    connection = _Connection(position_reply=None)  # never answers (the tooma shape)
    daemon, _c, clock = _daemon(tmp_path, connection=connection, settings=_auto_settings(tmp_path))
    daemon._attach()
    daemon._maintain()
    assert connection.position_requests == ["!91378e3a"]
    clock.now += 30.0
    daemon._maintain()
    assert connection.position_requests == ["!91378e3a"]  # gated, no churn
    clock.now += 301.0
    daemon._maintain()
    assert connection.position_requests == ["!91378e3a", "!91378e3a"]
    rows = [r for r in _audit_lines(tmp_path) if r["action"] == "mesh_position_request"]
    assert [r["outcome"] for r in rows] == ["timed_out", "timed_out"]


def test_auto_pass_respects_the_shared_hourly_cap(tmp_path: Path) -> None:
    from obi.core.mesh import _RateLimiter

    settings = _auto_settings(tmp_path, hourly_tx_cap=1)
    daemon, connection, clock = _daemon(tmp_path, settings=settings)
    # Another requester (any process) already spent the hour's budget.
    _RateLimiter(settings, tmp_path / "limiter", clock).record_send("!someone-else")
    daemon._attach()
    daemon._maintain()
    assert connection.position_requests == []
    rows = [r for r in _audit_lines(tmp_path) if r["action"] == "mesh_position_request"]
    assert [r["outcome"] for r in rows] == ["denied"]
    assert rows[0]["actor"] == "meshd:auto"


def test_auto_pass_is_silent_during_standby(tmp_path: Path) -> None:
    daemon, connection, clock = _daemon(tmp_path, settings=_auto_settings(tmp_path))
    daemon._attach()
    daemon.standby(600, source="cli:mesh")
    clock.now += 10.0
    daemon._maintain()
    assert connection.position_requests == []


def test_auto_pass_disabled_at_zero(tmp_path: Path) -> None:
    settings = _auto_settings(tmp_path, auto_request_s=0)
    daemon, connection, clock = _daemon(tmp_path, settings=settings)
    daemon._attach()
    clock.now += 3600.0
    daemon._maintain()
    assert connection.position_requests == []


# --- RX-liveness detection (2026-09-02 followup) -----------------------------------------


def test_dead_reader_thread_triggers_reconnect_not_false_attached(tmp_path: Path) -> None:
    """The 91-minute-deaf bug: the library's reader thread dies (node reads still succeed
    on the cached dict), so the daemon must not keep reporting 'attached'."""

    daemon, connection, clock = _daemon(tmp_path)
    daemon._attach()
    assert daemon.status().state == "attached"
    # The serial disconnect: reader thread dead, but read_nodes() still returns.
    connection.alive = False
    clock.now += 1.0
    daemon._maintain()
    assert daemon.status().state == "reconnecting"
    assert "RX" in daemon.status().detail or "reader" in daemon.status().detail
    assert connection.closed == 1  # the dead interface was torn down


def test_recovers_when_the_reader_comes_back(tmp_path: Path) -> None:
    calls = {"n": 0}

    def factory(port: str, timeout: float) -> _Connection:
        calls["n"] += 1
        return _Connection()

    daemon, _first, clock = _daemon(tmp_path, factory=factory)
    daemon._attach()
    # Kill the current connection's reader; next tick reconnects to a fresh, live one.
    with daemon._lock:
        live_conn = daemon._connection
    live_conn.alive = False  # type: ignore[union-attr]
    clock.now += 1.0
    daemon._maintain()
    assert daemon.status().state == "reconnecting"
    clock.now += 10.0
    daemon._next_attach_s = 0.0
    daemon._maintain()
    assert daemon.status().state == "attached"
    assert calls["n"] == 2  # opened a second, live connection


def test_liveness_probe_absent_is_assumed_live(tmp_path: Path) -> None:
    """A connection wrapper without is_alive() (the older shape) must not be read as dead."""

    class _NoProbe(_Connection):
        is_alive = None  # type: ignore[assignment]

    daemon, _c, clock = _daemon(tmp_path, connection=_NoProbe())
    daemon._attach()
    clock.now += 1.0
    daemon._maintain()
    assert daemon.status().state == "attached"
