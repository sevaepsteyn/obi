"""The mesh daemon's RPC client (feature 123): how every consumer reaches the radio.

Speaks the :class:`~obi.core.drivers.meshtastic.MeshAccess` surface — ``resolve_port`` /
``snapshot`` / ``request_position`` with the driver's exact return types — so the locate
engine, the recorder path, and the dash collector swapped to the daemon without a line of
call-site logic changing (contract O1/O3 in ``specs/123-mesh-daemon``).

Total and bounded, both directions: every call carries a connect deadline (1 s) and a
per-op read deadline, and every failure — daemon not running, socket gone, malformed
reply — returns a typed :class:`MeshFailure` (``DAEMON_DOWN``) that the engines already
know how to render honestly. Nothing here ever raises to a caller and nothing waits
unbounded (contract O4/O8; the 2026-09-02 incident lesson).

Attribution rides every request: the ``source`` field defaults to the ambient audit
scope's source (the same string the consumer's own audit events carry), so the daemon's
``device_command`` rows can say who asked (contract O6).
"""

from __future__ import annotations

import json
import socket
import uuid
from typing import Any

from obi.core.audit import current_scope
from obi.core.config import MeshtasticSettings
from obi.core.drivers.meshtastic import (
    MeshFailure,
    MeshNode,
    MeshOutcome,
    MeshPosition,
    MeshSnapshot,
)

#: Client-side deadlines (contracts/daemon-rpc.md).
_CONNECT_TIMEOUT_S = 1.0
_CONTROL_READ_TIMEOUT_S = 5.0
_REQUEST_READ_MARGIN_S = 5.0

_MAX_REPLY_BYTES = 4 * 1024 * 1024  # a 120-node table is ~100 KB; 4 MB is "gone wrong"


def _down(detail: str) -> MeshFailure:
    return MeshFailure(MeshOutcome.DAEMON_DOWN, detail)


def _parse_wire_position(raw: object) -> MeshPosition | None:
    if not isinstance(raw, dict):
        return None
    latitude, longitude = raw.get("latitude"), raw.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    altitude = raw.get("altitude_m")
    fix_time = raw.get("fix_time")
    return MeshPosition(
        latitude=float(latitude),
        longitude=float(longitude),
        altitude_m=altitude if isinstance(altitude, int) else None,
        fix_time=fix_time if isinstance(fix_time, int) else None,
    )


def _parse_wire_node(raw: object) -> MeshNode | None:
    if not isinstance(raw, dict):
        return None
    node_id = str(raw.get("node_id") or "")
    if not node_id:
        return None
    last_heard = raw.get("last_heard")
    hops = raw.get("hops_away")
    snr = raw.get("snr")
    battery = raw.get("battery_level")
    hw_model = raw.get("hw_model")
    return MeshNode(
        node_id=node_id,
        short_name=str(raw.get("short_name") or ""),
        long_name=str(raw.get("long_name") or ""),
        hw_model=str(hw_model) if isinstance(hw_model, str) and hw_model else None,
        last_heard=last_heard if isinstance(last_heard, int) else None,
        hops_away=hops if isinstance(hops, int) else None,
        snr=float(snr) if isinstance(snr, (int, float)) else None,
        battery_level=battery if isinstance(battery, int) else None,
        position=_parse_wire_position(raw.get("position")),
        is_self=bool(raw.get("is_self")),
    )


class MeshDaemonClient:
    """RPC access to the mesh daemon, in the driver's shape. Never raises, never hangs."""

    def __init__(
        self,
        settings: MeshtasticSettings,
        *,
        source: str | None = None,
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
    ) -> None:
        self._settings = settings
        self._source_override = source
        self._connect_timeout_s = connect_timeout_s
        #: The last status the daemon reported to this client (read by the dash collector
        #: to say "on loan" honestly without a second RPC). Data, not a promise.
        self.last_status: dict[str, Any] | None = None

    # --- the MeshAccess surface ----------------------------------------------------------

    def resolve_port(self) -> str | MeshFailure:
        """Daemon status → the port string, or a typed failure the engines can render.

        ``standby`` still resolves: snapshots keep answering from last-known data and a
        position request gets the daemon's own typed "on loan" refusal — richer honesty
        than refusing here would give.
        """

        result = self._call("status", read_timeout_s=_CONTROL_READ_TIMEOUT_S)
        if isinstance(result, MeshFailure):
            return result
        status = result.get("status")
        if not isinstance(status, dict):
            return _down("the mesh daemon sent an unusable status")
        self.last_status = status
        state = status.get("state")
        detail = str(status.get("detail") or state or "unknown state")
        port = status.get("port")
        if state in ("attached", "standby") and isinstance(port, str) and port:
            return port
        return MeshFailure(MeshOutcome.NO_RADIO, detail)

    def snapshot(
        self, port: str, *, timeout: float | None = None
    ) -> MeshSnapshot | MeshFailure:
        result = self._call("snapshot", read_timeout_s=_CONTROL_READ_TIMEOUT_S)
        if isinstance(result, MeshFailure):
            return result
        raw = result.get("snapshot")
        if not isinstance(raw, dict):
            return _down("the mesh daemon sent an unusable snapshot")
        raw_nodes = raw.get("nodes")
        entries = raw_nodes if isinstance(raw_nodes, list) else []
        nodes = tuple(
            node for entry in entries if (node := _parse_wire_node(entry)) is not None
        )
        return MeshSnapshot(
            port=str(raw.get("port") or port),
            my_node_id=str(raw.get("my_node_id") or ""),
            nodes=nodes,
        )

    def request_position(
        self, port: str, node_id: str, *, deadline_s: float
    ) -> MeshPosition | MeshFailure:
        result = self._call(
            "request_position",
            read_timeout_s=deadline_s + _REQUEST_READ_MARGIN_S,
            node_id=node_id,
            deadline_s=deadline_s,
        )
        if isinstance(result, MeshFailure):
            return result
        position = _parse_wire_position(result.get("position"))
        if position is None:
            return _down("the mesh daemon sent an unusable position")
        return position

    # --- daemon controls (CLI + dash detail) ---------------------------------------------

    def status(self) -> dict[str, Any] | MeshFailure:
        result = self._call("status", read_timeout_s=_CONTROL_READ_TIMEOUT_S)
        if isinstance(result, MeshFailure):
            return result
        status = result.get("status")
        if not isinstance(status, dict):
            return _down("the mesh daemon sent an unusable status")
        self.last_status = status
        return status

    def standby(self, lease_s: int) -> dict[str, Any] | MeshFailure:
        result = self._call(
            "standby", read_timeout_s=_CONTROL_READ_TIMEOUT_S + 5.0, lease_s=lease_s
        )
        if isinstance(result, MeshFailure):
            return result
        status = result.get("status")
        return status if isinstance(status, dict) else _down("unusable standby reply")

    def resume(self) -> dict[str, Any] | MeshFailure:
        # Resume reattaches inline (open + config download) before answering.
        result = self._call("resume", read_timeout_s=_CONTROL_READ_TIMEOUT_S + 60.0)
        if isinstance(result, MeshFailure):
            return result
        status = result.get("status")
        return status if isinstance(status, dict) else _down("unusable resume reply")

    # --- the wire ------------------------------------------------------------------------

    def _source(self) -> str:
        if self._source_override:
            return self._source_override
        scope = current_scope()
        return scope[1] if scope is not None else "local"

    def _call(
        self, op: str, *, read_timeout_s: float, **args: Any
    ) -> dict[str, Any] | MeshFailure:
        request = {"id": uuid.uuid4().hex[:12], "op": op, "source": self._source(), **args}
        path = str(self._settings.daemon_socket)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._connect_timeout_s)
                sock.connect(path)
                sock.settimeout(read_timeout_s)
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                buffer = b""
                while b"\n" not in buffer:
                    chunk = sock.recv(65536)
                    if not chunk:
                        return _down("the mesh daemon closed the connection mid-reply")
                    buffer += chunk
                    if len(buffer) > _MAX_REPLY_BYTES:
                        return _down("the mesh daemon sent an oversized reply")
        except OSError as exc:
            return _down(f"the mesh daemon isn't reachable at {path} ({exc})")
        try:
            response = json.loads(buffer.split(b"\n", 1)[0])
        except ValueError:
            return _down("the mesh daemon sent malformed JSON")
        if not isinstance(response, dict):
            return _down("the mesh daemon sent malformed JSON")
        if response.get("ok") is not True:
            outcome_raw = str(response.get("outcome") or "error")
            try:
                outcome = MeshOutcome(outcome_raw)
            except ValueError:
                outcome = MeshOutcome.ERROR
            return MeshFailure(outcome, str(response.get("detail") or "daemon error"))
        return response
