"""The mesh daemon (feature 123): the ONE process that owns the Meshtastic serial port.

Before 123, five independent code paths opened the exclusive serial device ad hoc — the
dash tile, the recorder cron, and lookups from every conversational surface — which meant
port-lock collisions and, decisively, "pause the monitoring crons" as the only way to run
a direct CLI experiment. That pause's failed restore cost seven hours of mesh
observability on 2026-09-02. This daemon removes the collision class: it opens the radio
once, holds it for its lifetime, hears the packet stream live (fixes recorded to the 117
store as they arrive), and serves every consumer over a Unix-socket JSON-lines RPC
(``contracts/daemon-rpc.md``). Consumers speak :class:`obi.core.meshclient.MeshDaemonClient`.

Safety posture is unchanged from 111/121: the 121 port fence runs on every attach (an
excluded/ambiguous port refuses), the only transmitting op is a position request performed
strictly on an attributed RPC, and every serial action still lands a ``device_command``
audit row — now under a ``meshd:<requesting source>`` scope so the trail says who asked.

Every wait is bounded on both sides of the seam (contract O4, the incident lesson): serial
operations run under a hard cap of 3× their deadline, after which the interface is torn
down and reopened (the 122 deterministic-teardown pattern) rather than trusted.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import logging
import os
import socketserver
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from obi.core import meshstore
from obi.core.audit import AuditTrail, timed_device_command
from obi.core.brcaddr import ModelLoader, no_model
from obi.core.config import MeshtasticSettings, default_mesh_state_path
from obi.core.drivers.meshtastic import (
    ConnectionFactory,
    MeshConnection,
    MeshDriver,
    MeshFailure,
    MeshNode,
    MeshOutcome,
    MeshPosition,
    MeshSnapshot,
    _open_serial,
    perform_position_request,
    snapshot_from_nodes,
)
from obi.core.mesh import (
    _RateLimiter,
    _squeeze,
    record_fixes,
)
from obi.core.models import AuditOutcome, AuditRecord, _utcnow

logger = logging.getLogger("obi")

_LOCK_FILE = "daemon.lock"
_STATUS_FILE = "daemon-status.json"

#: Standby lease clamp (data-model.md): a minute at least, a day at most.
_LEASE_MIN_S = 60
_LEASE_MAX_S = 86_400

#: Position-request deadline clamp (data-model.md).
_DEADLINE_MIN_S = 1.0
_DEADLINE_MAX_S = 120.0

#: Reconnect backoff: first retry quickly, then ease off — capped so a replugged radio is
#: back within the SC-006 two-minute bound.
_BACKOFF_START_S = 5.0
_BACKOFF_MAX_S = 60.0

#: The maintain loop's tick; also bounds callback-triggered record latency.
_TICK_S = 1.0

#: Debounce for packet-triggered recording: table reads are memory-cheap but a packet
#: burst should not turn into a record pass per packet.
_RECORD_DEBOUNCE_S = 2.0

ScopeFactory = Callable[[str], AbstractContextManager[None]]


@dataclass(frozen=True)
class DaemonStatus:
    """The daemon's relationship to the radio right now (data-model.md)."""

    state: str  # "attached" | "standby" | "reconnecting"
    since_s: float
    port: str | None
    detail: str
    lease_until_s: float | None
    lease_source: str | None
    snapshot_age_s: float | None
    pid: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "since_s": self.since_s,
            "port": self.port,
            "detail": self.detail,
            "lease_until_s": self.lease_until_s,
            "lease_source": self.lease_source,
            "snapshot_age_s": self.snapshot_age_s,
            "pid": self.pid,
        }


def serialize_snapshot(snapshot: MeshSnapshot, taken_at_s: float) -> dict[str, Any]:
    """A :class:`MeshSnapshot` as wire JSON — a 1:1 field round-trip, nothing re-derived."""

    return {
        "port": snapshot.port,
        "my_node_id": snapshot.my_node_id,
        "taken_at_s": taken_at_s,
        "nodes": [
            {
                "node_id": node.node_id,
                "short_name": node.short_name,
                "long_name": node.long_name,
                "hw_model": node.hw_model,
                "last_heard": node.last_heard,
                "hops_away": node.hops_away,
                "snr": node.snr,
                "battery_level": node.battery_level,
                "is_self": node.is_self,
                "position": (
                    None
                    if node.position is None
                    else {
                        "latitude": node.position.latitude,
                        "longitude": node.position.longitude,
                        "altitude_m": node.position.altitude_m,
                        "fix_time": node.position.fix_time,
                    }
                ),
            }
            for node in snapshot.nodes
        ],
    }


def _failure_wire(failure: MeshFailure, request_id: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "outcome": failure.outcome.value,
        "detail": failure.detail,
    }


class _HardCapExceeded(Exception):
    """A serial operation blew through its hard cap — the interface is not to be trusted."""


class MeshDaemon:
    """Owns the serial port; serves snapshot/status/request/standby/resume over RPC.

    Total at the RPC boundary (caller input never crashes it) and bounded everywhere: the
    engines' "never raises, never hangs" discipline, applied to a process that must survive
    unplugs, wedged libraries, and abandoned borrows without an operator.
    """

    def __init__(
        self,
        settings: MeshtasticSettings,
        audit: AuditTrail,
        *,
        connection_factory: ConnectionFactory | None = None,
        find_ports: Callable[[], list[str]] | None = None,
        model_loader: ModelLoader | None = None,
        now: Callable[[], float] | None = None,
        scope_factory: ScopeFactory | None = None,
        limiter_state_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._factory: ConnectionFactory = connection_factory or _open_serial
        # The 121 fence lives in resolve_port/_claimed; the daemon reuses it verbatim (O2).
        self._resolver = MeshDriver(
            settings, connection_factory=self._factory, find_ports=find_ports
        )
        self._model_loader = model_loader or no_model
        self._now = now or time.time
        self._scope_factory = scope_factory or (lambda _source: contextlib.nullcontext())
        # 125: the SAME durable budget every requester shares (ratelimit.json) — the auto
        # pass spends from it like a person's request used to.
        self._limiter = _RateLimiter(
            settings, limiter_state_dir or default_mesh_state_path(), self._now
        )
        self._auto_attempted: dict[str, float] = {}

        self._lock = threading.Lock()  # guards all mutable state below
        self._serial_lock = threading.Lock()  # serializes ops against the connection
        # Serializes attach/standby/resume against the maintain loop. Reentrant because
        # resume() attaches inline. Without it, resume's attach and the maintain tick's
        # attach raced: both opened the port, the loser's Errno 35 clobbered the winner's
        # "attached" state, and the daemon then fought its OWN held connection forever —
        # observed live on the 2026-09-02 rollout, first standby→resume cycle.
        self._transition_lock = threading.RLock()
        self._connection: MeshConnection | None = None
        self._port: str | None = None
        self._snapshot: MeshSnapshot | None = None
        self._snapshot_taken_s: float | None = None
        self._state = "reconnecting"
        self._state_since = self._now()
        self._state_detail = "starting"
        self._lease_until_s: float | None = None
        self._lease_source: str | None = None
        self._request_in_flight = False
        self._stop = threading.Event()
        self._record_asap = False
        self._last_record_s = 0.0
        self._last_sweep_s = 0.0
        self._next_attach_s = 0.0
        self._backoff_s = _BACKOFF_START_S
        self._lock_handle: BinaryIO | None = None

    # --- lifecycle -----------------------------------------------------------------------

    def run(self) -> int:
        """Own the port until stopped. Returns an exit code (1 = another instance runs)."""

        state_dir = self._settings.daemon_socket.parent
        state_dir.mkdir(parents=True, exist_ok=True)
        if not self._acquire_instance_lock(state_dir):
            logger.error("mesh daemon: another instance already holds %s", state_dir / _LOCK_FILE)
            return 1
        server = self._start_server()
        logger.info("mesh daemon: serving on %s", self._settings.daemon_socket)
        try:
            while not self._stop.wait(_TICK_S):
                self._maintain()
        finally:
            server.shutdown()
            server.server_close()
            with contextlib.suppress(OSError):
                self._settings.daemon_socket.unlink()
            self._teardown("stopping")
            self._release_instance_lock()
        logger.info("mesh daemon: stopped")
        return 0

    def stop(self) -> None:
        self._stop.set()

    def _acquire_instance_lock(self, state_dir: Path) -> bool:
        handle = (state_dir / _LOCK_FILE).open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock_handle = handle
        return True

    def _release_instance_lock(self) -> None:
        if self._lock_handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        self._lock_handle.close()
        self._lock_handle = None

    def _start_server(self) -> _RpcServer:
        socket_path = self._settings.daemon_socket
        with contextlib.suppress(OSError):
            socket_path.unlink()  # stale socket from a dead instance; the flock proved it
        server = _RpcServer(str(socket_path), _RpcHandler)
        server.daemon_ref = self
        thread = threading.Thread(target=server.serve_forever, name="meshd-rpc", daemon=True)
        thread.start()
        return server

    # --- the maintain loop ---------------------------------------------------------------

    def _maintain(self) -> None:
        """One tick: lease expiry, reconnect, sweep, callback-triggered recording."""

        now = self._now()
        with self._lock:
            state = self._state
            lease_until = self._lease_until_s
        if state == "standby":
            if lease_until is not None and now >= lease_until:
                self.resume(source="lease-expiry")
            return
        if state == "reconnecting":
            if now >= self._next_attach_s:
                self._attach()
            return
        # attached: verify the radio is still being HEARD before trusting it. A dead
        # library reader thread keeps node-table reads succeeding on stale data, so this
        # is the only thing that catches a silent RX loss (2026-09-02: 91 min deaf while
        # reporting "attached"). Cheap (a thread.is_alive() check), so run it every tick.
        if not self._connection_alive():
            self._teardown("serial reader stopped — RX lost")
            self._set_state("reconnecting", "mesh radio disconnected (RX reader stopped)")
            self._schedule_retry()
            return
        due = (now - self._last_sweep_s) >= self._settings.daemon_sweep_s
        asap = self._record_asap and (now - self._last_record_s) >= _RECORD_DEBOUNCE_S
        if due or asap:
            self._sweep()
        self._auto_refresh()

    def _connection_alive(self) -> bool:
        """Is the held connection still receiving? A fake/wrapper without ``is_alive``
        (older connect-per-query shape) is assumed live — only the daemon's persistent
        wrapper reports it."""

        with self._lock:
            connection = self._connection
        if connection is None:
            return False
        probe = getattr(connection, "is_alive", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a liveness probe must never crash the loop
            return False

    def _attach(self) -> None:
        with self._transition_lock:
            with self._lock:
                if self._connection is not None and self._state == "attached":
                    return  # a racing caller already attached; churn helps nobody
            # An orphaned connection (a half-finished transition) would make our own open
            # fail Errno 35 forever — drop it before opening, never fight it.
            self._teardown("reattaching")
            port = self._resolver.resolve_port()
            if isinstance(port, MeshFailure):
                self._set_state("reconnecting", port.detail)
                self._schedule_retry()
                return
            cap = max(1.0, self._settings.snapshot_timeout_s * 3)
            try:
                with (
                    self._scope_factory("meshd"),
                    timed_device_command(port, "meshtastic-serial", "open (mesh daemon)"),
                ):
                    connection = self._bounded(
                        lambda: self._factory(port, self._settings.request_timeout_s), cap
                    )
            except Exception as exc:  # noqa: BLE001 - attach failure is a state, not a crash
                self._set_state("reconnecting", str(exc) or type(exc).__name__)
                self._schedule_retry()
                return
            subscribe = getattr(connection, "subscribe_packets", None)
            if callable(subscribe):
                with contextlib.suppress(Exception):
                    subscribe(self._on_packet)
            with self._lock:
                self._connection = connection
                self._port = port
                self._backoff_s = _BACKOFF_START_S
            self._set_state("attached", f"attached on {port}")
            self._sweep()

    def _schedule_retry(self) -> None:
        self._next_attach_s = self._now() + self._backoff_s
        self._backoff_s = min(_BACKOFF_MAX_S, self._backoff_s * 2)

    def _teardown(self, reason: str) -> None:
        """Deterministically drop the connection (122): bounded close, never trusted."""

        with self._lock:
            connection, self._connection = self._connection, None
        if connection is None:
            return
        logger.info("mesh daemon: closing serial interface (%s)", reason)
        with contextlib.suppress(Exception):
            self._bounded(lambda: _close_and_none(connection), 5.0)

    def _sweep(self) -> None:
        """Read the held interface's node table (a memory read) and record fixes."""

        with self._lock:
            connection, port = self._connection, self._port
        if connection is None or port is None:
            return
        cap = max(1.0, self._settings.snapshot_timeout_s * 3)
        try:
            with self._serial_lock:
                my_node_id, raw_nodes = self._bounded(connection.read_nodes, cap)
        except Exception as exc:  # noqa: BLE001 - a failed read is a reconnect, not a crash
            self._teardown(f"node-table read failed: {exc}")
            self._set_state("reconnecting", str(exc) or type(exc).__name__)
            self._schedule_retry()
            return
        snapshot = snapshot_from_nodes(port, my_node_id, raw_nodes)
        now = self._now()
        with self._lock:
            self._snapshot = snapshot
            self._snapshot_taken_s = now
            self._record_asap = False
            self._last_sweep_s = now
            self._last_record_s = now
        recorded, tracked, _error = record_fixes(
            self._settings, snapshot.nodes, model_loader=self._model_loader, now=self._now
        )
        if recorded:
            logger.info(
                "mesh daemon: recorded %d new fix(es), %d allowlisted node(s) with a fix",
                recorded,
                tracked,
            )
        self._write_status_file()

    def _on_packet(self, packet: dict[str, Any]) -> None:
        """Library receive callback (runs on the reader thread): flag, never work here."""

        decoded = packet.get("decoded")
        portnum = decoded.get("portnum") if isinstance(decoded, dict) else None
        if portnum != "POSITION_APP":
            return
        with self._lock:
            self._record_asap = True

    # --- state ---------------------------------------------------------------------------

    def _set_state(self, state: str, detail: str) -> None:
        with self._lock:
            if self._state != state:
                self._state_since = self._now()
            self._state = state
            self._state_detail = detail
        logger.info("mesh daemon: %s — %s", state, detail)
        self._write_status_file()

    def status(self) -> DaemonStatus:
        with self._lock:
            taken = self._snapshot_taken_s
            return DaemonStatus(
                state=self._state,
                since_s=self._state_since,
                port=self._port,
                detail=self._state_detail,
                lease_until_s=self._lease_until_s,
                lease_source=self._lease_source,
                snapshot_age_s=None if taken is None else max(0.0, self._now() - taken),
                pid=os.getpid(),
            )

    def _write_status_file(self) -> None:
        """Mirror status to disk so forensics (and daemon-down reads) have it."""

        path = self._settings.daemon_socket.parent / _STATUS_FILE
        payload = self.status().to_wire()
        payload["written_at_s"] = self._now()
        try:
            scratch = path.with_suffix(".json.tmp")
            scratch.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(scratch, path)
        except OSError:
            pass  # a status mirror must never take the daemon down

    # --- standby / resume ----------------------------------------------------------------

    def standby(self, lease_s: int, *, source: str) -> DaemonStatus:
        """Release the port on a self-expiring lease (FR-008). Idempotent-ish: a second
        standby replaces the lease (audited) rather than erroring — the operator's latest
        intent wins."""

        with self._transition_lock:
            lease = max(_LEASE_MIN_S, min(_LEASE_MAX_S, int(lease_s)))
            until = self._now() + lease
            self._teardown(f"standby requested by {source}")
            with self._lock:
                self._lease_until_s = until
                self._lease_source = source
            self._set_state("standby", f"radio on loan to {source} until {_clock_text(until)}")
            self._record_transition(
                "mesh_daemon_standby", source, {"lease_s": lease, "until_s": until}
            )
            return self.status()

    def resume(self, *, source: str) -> DaemonStatus:
        with self._transition_lock:
            with self._lock:
                self._lease_until_s = None
                self._lease_source = None
            self._set_state("reconnecting", f"resuming after standby ({source})")
            self._record_transition("mesh_daemon_resume", source, {})
            self._next_attach_s = 0.0
            self._backoff_s = _BACKOFF_START_S
            self._attach()
            return self.status()

    def _record_transition(self, action: str, source: str, params: dict[str, object]) -> None:
        with contextlib.suppress(Exception):
            self._audit.record(
                AuditRecord(
                    timestamp=_utcnow(),
                    action=action,
                    target=self._port or "mesh-daemon",
                    reason=f"requested by {source}",
                    outcome=AuditOutcome.OK,
                    params=params,
                    detail=None,
                    actor=source,
                )
            )

    # --- RPC dispatch --------------------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """One RPC request → one response dict. Total: caller input never raises out."""

        request_id = str(request.get("id") or "")
        op = str(request.get("op") or "")
        source = str(request.get("source") or "").strip()
        if not source:
            return _failure_wire(MeshFailure(MeshOutcome.ERROR, "missing source"), request_id)
        try:
            with self._scope_factory(f"meshd:{source}"):
                return self._dispatch(request_id, op, source, request)
        except Exception as exc:  # noqa: BLE001 - the RPC boundary is total
            logger.exception("mesh daemon: unhandled error in op %r", op)
            return _failure_wire(
                MeshFailure(MeshOutcome.ERROR, str(exc) or type(exc).__name__), request_id
            )

    def _dispatch(
        self, request_id: str, op: str, source: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        if op == "status":
            return {"id": request_id, "ok": True, "status": self.status().to_wire()}
        if op == "snapshot":
            with self._lock:
                snapshot, taken = self._snapshot, self._snapshot_taken_s
            if snapshot is None or taken is None:
                detail = self.status().detail or "no node-table read yet"
                return _failure_wire(MeshFailure(MeshOutcome.NO_RADIO, detail), request_id)
            return {
                "id": request_id,
                "ok": True,
                "snapshot": serialize_snapshot(snapshot, taken),
            }
        if op == "request_position":
            return self._handle_request_position(request_id, request)
        if op == "standby":
            lease_raw = request.get("lease_s")
            lease = (
                int(lease_raw)
                if isinstance(lease_raw, (int, float)) and not isinstance(lease_raw, bool)
                else self._settings.standby_lease_s
            )
            status = self.standby(lease, source=source)
            return {"id": request_id, "ok": True, "status": status.to_wire()}
        if op == "resume":
            status = self.resume(source=source)
            return {"id": request_id, "ok": True, "status": status.to_wire()}
        return _failure_wire(MeshFailure(MeshOutcome.ERROR, f"unknown op {op!r}"), request_id)

    def _handle_request_position(self, request_id: str, request: dict[str, Any]) -> dict[str, Any]:
        node_id = str(request.get("node_id") or "")
        if not node_id:
            return _failure_wire(MeshFailure(MeshOutcome.ERROR, "missing node_id"), request_id)
        deadline_raw = request.get("deadline_s")
        deadline = (
            float(deadline_raw)
            if isinstance(deadline_raw, (int, float)) and not isinstance(deadline_raw, bool)
            else self._settings.request_timeout_s
        )
        deadline = max(_DEADLINE_MIN_S, min(_DEADLINE_MAX_S, deadline))
        result = self._perform_request(node_id, deadline)
        if isinstance(result, MeshFailure):
            return _failure_wire(result, request_id)
        return {
            "id": request_id,
            "ok": True,
            "position": {
                "latitude": result.latitude,
                "longitude": result.longitude,
                "altitude_m": result.altitude_m,
                "fix_time": result.fix_time,
            },
        }

    def _perform_request(self, node_id: str, deadline_s: float) -> MeshPosition | MeshFailure:
        """THE transmit path: one position request, in-flight-guarded and hard-capped.

        Shared by the RPC op and the 125 automatic freshness pass — there is exactly one
        way a transmission happens in this process. A reply flags the prompt re-record.
        """

        with self._lock:
            state, detail = self._state, self._state_detail
            connection, port = self._connection, self._port
            if state == "attached" and connection is not None and port is not None:
                if self._request_in_flight:
                    return MeshFailure(
                        MeshOutcome.ERROR, "busy: another position request is in flight"
                    )
                self._request_in_flight = True
            else:
                return MeshFailure(MeshOutcome.NO_RADIO, detail)
        try:
            with self._serial_lock:
                result: MeshPosition | MeshFailure = self._bounded(
                    lambda: perform_position_request(
                        connection,
                        node_id,
                        self._settings.request_channel_index,
                        deadline_s=deadline_s,
                        port=port,
                    ),
                    deadline_s * 3,
                )
        except _HardCapExceeded as exc:
            self._teardown(str(exc))
            self._set_state("reconnecting", str(exc))
            self._schedule_retry()
            return MeshFailure(MeshOutcome.ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 - total at the transmit boundary
            return MeshFailure(MeshOutcome.ERROR, str(exc) or type(exc).__name__)
        finally:
            with self._lock:
                self._request_in_flight = False
        if not isinstance(result, MeshFailure):
            # A reply is the freshest fix the mesh gives — make it queryable promptly.
            with self._lock:
                self._record_asap = True
        return result

    # --- the automatic freshness pass (feature 125) ----------------------------------------

    def _tracked_node_ids(self) -> list[tuple[str, MeshNode | None]]:
        """(node id, table node or None) per allowlisted entry that resolves to an id.

        Resolution mirrors the recorder's allowlist matching: an entry names a node by
        squeezed id/short/long against the current snapshot; a literal ``!id`` entry
        resolves even when the node is absent from the table (the 117 eviction shape).
        """

        with self._lock:
            snapshot = self._snapshot
        nodes = snapshot.nodes if snapshot is not None else ()
        resolved: list[tuple[str, MeshNode | None]] = []
        for entry in self._settings.report_only:
            squeezed = _squeeze(entry)
            if not squeezed:
                continue
            match = next(
                (
                    node
                    for node in nodes
                    if squeezed
                    in {_squeeze(node.node_id), _squeeze(node.short_name), _squeeze(node.long_name)}
                ),
                None,
            )
            if match is not None:
                resolved.append((match.node_id, match))
            elif entry.startswith("!"):
                resolved.append((entry, None))
        return resolved

    def _stored_fix_age_s(self, node_id: str) -> float | None:
        """Age of the newest recorded fix for ``node_id``; ``None`` = nothing recorded."""

        try:
            conn = meshstore.connect_readonly(self._settings.positions_db_path)
        except meshstore.StoreError:
            return None
        try:
            fix = meshstore.last_known(conn, node_id)
        except meshstore.StoreError:
            return None
        finally:
            with contextlib.suppress(Exception):
                conn.close()
        if fix is None:
            return None
        return max(0.0, self._now() - fix.effective_fix_s)

    def _auto_refresh(self) -> None:
        """Keep tracked nodes fresh on the operator's schedule (125, FR-001..004).

        Per tracked node: when the newest RECORDED fix is older than ``auto_request_s``
        (or absent), send the one rate-limited request — through the same in-flight
        guard, the same durable limiter, and the same audit action a person's request
        used to produce, with the ``meshd:auto`` actor saying nobody asked. Attempts
        (answered or not) are gated to one per node per interval.
        """

        interval = self._settings.auto_request_s
        if interval <= 0:
            return
        with self._lock:
            if self._state != "attached":
                return
        now = self._now()
        for node_id, node in self._tracked_node_ids():
            attempted = self._auto_attempted.get(node_id)
            if attempted is not None and now - attempted < interval:
                continue
            age = self._stored_fix_age_s(node_id)
            if age is not None and age <= interval:
                continue
            self._auto_attempted[node_id] = now
            display = (
                self._settings.display_names.get(node_id)
                or (node.long_name if node is not None else "")
                or node_id
            )
            denial = self._limiter.denial(node_id)
            if denial is not None:
                self._audit_auto_request(
                    node_id, display, AuditOutcome.DENIED, detail=denial, stored_age_s=age
                )
                continue
            self._limiter.record_send(node_id)
            with self._scope_factory("meshd:auto"):
                result = self._perform_request(node_id, self._settings.request_timeout_s)
            if isinstance(result, MeshFailure):
                outcome = (
                    AuditOutcome.TIMED_OUT
                    if result.outcome is MeshOutcome.TIMED_OUT
                    else AuditOutcome.ERROR
                )
                self._audit_auto_request(
                    node_id, display, outcome, detail=result.detail, stored_age_s=age
                )
                continue
            self._audit_auto_request(node_id, display, AuditOutcome.OK, stored_age_s=age)
            # Record the reply directly (the sweep's dedupe makes the overlap harmless).
            reply_node = (
                node
                if node is not None
                else MeshNode(node_id=node_id, short_name="", long_name=display)
            )
            record_fixes(
                self._settings,
                (
                    MeshNode(
                        node_id=reply_node.node_id,
                        short_name=reply_node.short_name,
                        long_name=reply_node.long_name,
                        hw_model=reply_node.hw_model,
                        last_heard=int(now),
                        hops_away=reply_node.hops_away,
                        snr=reply_node.snr,
                        battery_level=reply_node.battery_level,
                        position=result,
                    ),
                ),
                model_loader=self._model_loader,
                now=self._now,
            )

    def _audit_auto_request(
        self,
        node_id: str,
        display: str,
        outcome: AuditOutcome,
        *,
        detail: str | None = None,
        stored_age_s: float | None = None,
    ) -> None:
        """The engine's ``mesh_position_request`` action shape, actor ``meshd:auto``."""

        params: dict[str, object] = {"node": display}
        if stored_age_s is not None:
            params["stored_age_s"] = int(stored_age_s)
        with contextlib.suppress(Exception):
            self._audit.record(
                AuditRecord(
                    timestamp=_utcnow(),
                    action="mesh_position_request",
                    target=node_id,
                    reason="automatic freshness pass",
                    outcome=outcome,
                    params=params,
                    detail=detail,
                    actor="meshd:auto",
                )
            )

    # --- bounded execution ---------------------------------------------------------------

    def _bounded(self, fn: Callable[[], Any], cap_s: float) -> Any:
        """Run ``fn`` with a hard wall-clock cap (O4). On overrun the worker is abandoned
        (daemon thread) and told to close anything it eventually produced — the interface
        gets torn down by the caller, never waited on."""

        box: dict[str, Any] = {}
        abandoned = threading.Event()
        # The ambient audit scope is a contextvar and does not cross threads on its own —
        # without this copy, a device_command performed in the worker records nothing and
        # the trail loses the serial op entirely (found live, 2026-09-02 rollout: the
        # daemon's position-request row was missing while the open, wrapped on the calling
        # thread, was recorded fine). Contract O6.
        ctx = contextvars.copy_context()

        def runner() -> None:
            try:
                value = ctx.run(fn)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
                box["error"] = exc
                return
            if abandoned.is_set():
                with contextlib.suppress(Exception):
                    close = getattr(value, "close", None)
                    if callable(close):
                        close()
                return
            box["value"] = value

        worker = threading.Thread(target=runner, name="meshd-serial-op", daemon=True)
        worker.start()
        worker.join(cap_s)
        if worker.is_alive():
            abandoned.set()
            raise _HardCapExceeded(
                f"serial operation exceeded its {cap_s:.0f}s hard cap — reopening the interface"
            )
        if "error" in box:
            raise box["error"]
        return box["value"]


def _close_and_none(connection: MeshConnection) -> None:
    connection.close()


def _clock_text(epoch_s: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch_s))


class _RpcServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Threaded Unix-socket server; one caller's op never blocks another's reads."""

    daemon_threads = True
    allow_reuse_address = True
    daemon_ref: MeshDaemon  # set right after construction


class _RpcHandler(socketserver.StreamRequestHandler):
    """JSON-lines request/response per contracts/daemon-rpc.md. Malformed line ⇒ one
    error response, connection closed."""

    timeout = 130.0  # idle read cap: past every legal op deadline, far from unbounded

    def handle(self) -> None:
        daemon: MeshDaemon = self.server.daemon_ref  # type: ignore[attr-defined]
        while True:
            try:
                line = self.rfile.readline()
            except OSError:
                return
            if not line:
                return
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                self._reply({"id": "", "ok": False, "outcome": "error", "detail": "malformed JSON"})
                return
            if not isinstance(request, dict):
                self._reply({"id": "", "ok": False, "outcome": "error", "detail": "malformed JSON"})
                return
            self._reply(daemon.handle(request))

    def _reply(self, response: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()


def run_daemon(
    settings: MeshtasticSettings,
    audit: AuditTrail,
    *,
    scope_factory: ScopeFactory | None = None,
    install_signals: bool = True,
    model_loader: ModelLoader | None = None,
) -> int:
    """CLI entry: run a daemon until SIGTERM/SIGINT. Returns the process exit code."""

    daemon = MeshDaemon(settings, audit, scope_factory=scope_factory, model_loader=model_loader)
    if install_signals:
        import signal

        def _stop(_signum: int, _frame: Any) -> None:
            daemon.stop()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    return daemon.run()
