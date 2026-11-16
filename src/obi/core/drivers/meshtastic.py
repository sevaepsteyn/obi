"""Meshtastic mesh-node access for mesh_locate (feature 111) — read-plus-query, never a write.

Like lighting (:mod:`obi.core.drivers.wled`), this is a SEPARATE adapter class OUTSIDE the
read-only network-vendor capability family in ``drivers/base.py`` — no network device gains
anything from it. It is the ONLY place permitted to import the ``meshtastic`` library (an
opt-in extra: ``uv sync --extra mesh``), and it stays total in the family's sense: every
method returns a typed outcome and never raises.

Two capabilities only. ``snapshot`` reads the attached radio's mesh node DB — pure read.
``request_position`` is the feature's ONE transmitting call: a single position query to a
single node, sent only by the engine's rate-limited request path, with a hard local deadline
(never the library's 300 s default). The serial port is exclusive-access, so every method is
connect-per-query: open, act, ``close()`` in a ``finally`` — obi never holds the port between
questions.

The connection factory and port discovery are injectable so tests run offline against the
2026-08-28 live-capture fixtures; the real factory wraps ``SerialInterface`` and discovery is
the library's USB vendor-id allowlist (0x239A covers the RAK4631) — accepted only when it
finds exactly one port ("guessing between serial adapters is how a wrong device gets keyed",
``radio/devices.py``).
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from obi.core.audit import timed_device_command
from obi.core.config import MeshtasticSettings

_EXTRA_HINT = "mesh support is not installed — run `uv sync --extra mesh`"


class MeshOutcome(StrEnum):
    """Typed driver outcomes (the ``linux_health`` shape); ``OK`` never rides a failure."""

    OK = "ok"
    NO_RADIO = "no_radio"
    PORT_BUSY = "port_busy"
    TIMED_OUT = "timed_out"
    #: 121: the far end ANSWERED and declined — a routing response, not silence. Distinct
    #: from TIMED_OUT because the operator action differs (the node is reachable but will not
    #: report) and because it returns immediately instead of burning the whole deadline.
    REFUSED = "refused"
    #: 123: the mesh daemon (the one process that owns the serial port) is not reachable.
    #: Distinct from NO_RADIO because the operator action differs: the radio may be fine —
    #: it is obi's own service that needs starting.
    DAEMON_DOWN = "daemon_down"
    ERROR = "error"


class _RoutingRefusal(Exception):
    """The destination answered our position request with a routing error.

    Raised by the connection wrapper and mapped to :attr:`MeshOutcome.REFUSED`. It exists as
    an exception rather than a return value so the reply-shaped return type stays honest:
    there is no position to hand back, and ``None`` already means "nothing arrived".
    """



@dataclass(frozen=True)
class MeshFailure:
    """A typed non-answer: what went wrong, in prose a surface can hand to a caller."""

    outcome: MeshOutcome
    detail: str


@dataclass(frozen=True)
class MeshPosition:
    """One usable fix. The driver never constructs one from garbage — (0, 0), non-finite,
    or out-of-range coordinates become ``None`` upstream, not a place in the Gulf of Guinea."""

    latitude: float
    longitude: float
    altitude_m: int | None = None
    fix_time: int | None = None


@dataclass(frozen=True)
class MeshNode:
    """One mesh participant as the attached radio knows it."""

    node_id: str
    short_name: str
    long_name: str
    hw_model: str | None = None
    last_heard: int | None = None
    hops_away: int | None = None
    snr: float | None = None
    battery_level: int | None = None
    position: MeshPosition | None = None
    is_self: bool = False


@dataclass(frozen=True)
class MeshSnapshot:
    """One read of the attached radio's node DB."""

    port: str
    my_node_id: str
    nodes: tuple[MeshNode, ...]


class MeshConnection(Protocol):
    """What the driver needs from an open interface; the real one wraps ``SerialInterface``.

    ``subscribe_packets`` (123) is optional in practice: the daemon looks it up with
    ``getattr`` so connect-per-query callers and existing test fakes never need it. The
    real wrapper implements it by subscribing to the library's receive topic.
    """

    def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]: ...

    def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None: ...

    def subscribe_packets(self, callback: Callable[[dict[str, Any]], None]) -> None: ...

    def is_alive(self) -> bool: ...

    def close(self) -> None: ...


#: (port, timeout_s) → an open connection. Raises on failure; the driver classifies.
ConnectionFactory = Callable[[str, float], MeshConnection]


class MeshAccess(Protocol):
    """The consumer-facing mesh surface (123): what every engine needs from "the radio".

    Two implementations: :class:`MeshDriver` (direct serial — daemon-side only, contract
    O1) and ``MeshDaemonClient`` (RPC to the daemon — everything else). Structural, so
    existing tests keep injecting plain drivers/fakes.
    """

    def resolve_port(self) -> str | MeshFailure: ...

    def snapshot(
        self, port: str, *, timeout: float | None = None
    ) -> MeshSnapshot | MeshFailure: ...

    def request_position(
        self, port: str, node_id: str, *, deadline_s: float
    ) -> MeshPosition | MeshFailure: ...


def _parse_position(raw: object) -> MeshPosition | None:
    """A usable fix from a node-DB/reply position dict, else ``None`` — never raises."""

    if not isinstance(raw, dict):
        return None
    lat_i, lon_i = raw.get("latitudeI"), raw.get("longitudeI")
    if isinstance(lat_i, int) and isinstance(lon_i, int):
        latitude, longitude = lat_i * 1e-7, lon_i * 1e-7
    else:
        lat_raw, lon_raw = raw.get("latitude"), raw.get("longitude")
        if not isinstance(lat_raw, (int, float)) or not isinstance(lon_raw, (int, float)):
            return None
        latitude, longitude = float(lat_raw), float(lon_raw)
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if latitude == 0.0 and longitude == 0.0:  # the firmware's "no fix" sentinel
        return None
    if abs(latitude) > 90.0 or abs(longitude) > 180.0:
        return None
    altitude = raw.get("altitude")
    fix_time = raw.get("time")
    return MeshPosition(
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude if isinstance(altitude, int) else None,
        fix_time=fix_time if isinstance(fix_time, int) else None,
    )


def _parse_node(node_id: str, raw: object, my_node_id: str) -> MeshNode | None:
    """One node-DB entry → :class:`MeshNode`; unusable fields degrade to ``None``, an
    unusable ENTRY (no id at all) drops — corrupt data is never repaired."""

    if not isinstance(raw, dict):
        return None
    user = raw.get("user")
    user_dict = user if isinstance(user, dict) else {}
    short = str(user_dict.get("shortName") or "").strip()
    long_name = str(user_dict.get("longName") or "").strip()
    hw_model = user_dict.get("hwModel")
    last_heard = raw.get("lastHeard")
    hops = raw.get("hopsAway")
    snr = raw.get("snr")
    metrics = raw.get("deviceMetrics")
    metrics_dict = metrics if isinstance(metrics, dict) else {}
    battery = metrics_dict.get("batteryLevel")
    return MeshNode(
        node_id=node_id,
        short_name=short,
        long_name=long_name,
        hw_model=str(hw_model) if isinstance(hw_model, str) and hw_model else None,
        last_heard=last_heard if isinstance(last_heard, int) else None,
        hops_away=hops if isinstance(hops, int) else None,
        snr=float(snr) if isinstance(snr, (int, float)) else None,
        battery_level=battery if isinstance(battery, int) else None,
        position=_parse_position(raw.get("position")),
        is_self=node_id == my_node_id,
    )


def snapshot_from_nodes(
    port: str, my_node_id: str, raw_nodes: dict[str, dict[str, Any]]
) -> MeshSnapshot:
    """One node-table read → :class:`MeshSnapshot`; unusable entries drop (``_parse_node``)."""

    nodes = tuple(
        node
        for node_id, raw in raw_nodes.items()
        if (node := _parse_node(node_id, raw, my_node_id)) is not None
    )
    return MeshSnapshot(port=port, my_node_id=my_node_id, nodes=nodes)


def perform_position_request(
    connection: MeshConnection,
    node_id: str,
    channel_index: int,
    *,
    deadline_s: float,
    port: str,
) -> MeshPosition | MeshFailure:
    """One position request over an ALREADY-OPEN connection, classified like the driver's.

    Exists for the daemon (123), which holds one persistent connection instead of the
    driver's connect-per-query — same audit shape, same outcome taxonomy, still exactly one
    transmission with no retry.
    """

    try:
        with timed_device_command(port, "meshtastic-serial", f"position request {node_id}"):
            reply = connection.request_position(node_id, channel_index)
    except _RoutingRefusal as refusal:
        return MeshFailure(
            MeshOutcome.REFUSED,
            f"{node_id} declined the position request ({refusal})",
        )
    except Exception as exc:  # noqa: BLE001 - total: every failure is a typed outcome
        return _classify(exc)
    if reply is None:
        return MeshFailure(
            MeshOutcome.TIMED_OUT,
            f"no position reply from {node_id} within {deadline_s:.0f}s",
        )
    position = _parse_position(reply)
    if position is None:
        return MeshFailure(MeshOutcome.ERROR, f"{node_id} answered without usable coordinates")
    return position


def _classify(exc: BaseException) -> MeshFailure:
    """An open/connect/read exception → a typed failure with an honest message."""

    if isinstance(exc, ImportError):
        return MeshFailure(MeshOutcome.NO_RADIO, _EXTRA_HINT)
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if "busy" in lowered or "exclusiv" in lowered or "in use" in lowered:
        return MeshFailure(
            MeshOutcome.PORT_BUSY,
            f"the mesh radio's serial port is in use by another program ({text})",
        )
    return MeshFailure(MeshOutcome.ERROR, text)


#: USB vendor ids that identify a Meshtastic radio: 0x239A (Adafruit/RAK nRF52 boards, which
#: covers the RAK4631) and 0x303A (Espressif). Kept HERE rather than imported from the vendor
#: library so a library upgrade cannot silently change which devices obi will open.
_MESH_VENDOR_IDS = frozenset({0x239A, 0x303A})


def _default_find_ports() -> list[str]:
    """Serial devices that ARE Meshtastic radios, by USB vendor id (lazy import).

    Deliberately not ``meshtastic.util.findPorts``. That helper filters by the same vendor
    ids but, **when the filter matches nothing, falls back to returning every port whose vid
    is not on a short blacklist** — and FTDI's 0x0403 is on neither list. With the mesh radio
    unplugged it therefore returned the radio station's PTT device, which
    :meth:`MeshDriver.resolve_port` then accepted as the sole unambiguous candidate. Opening
    that device keys the transmitter (:mod:`obi.radio.ptt`). Feature 121, contract G1.

    A strict filter is also the honest one: this module's docstring has always claimed
    discovery was a vendor-id allowlist. Now it is.
    """

    from serial.tools import list_ports

    return sorted(
        str(port.device)
        for port in list_ports.comports()
        if port.vid is not None and port.vid in _MESH_VENDOR_IDS
    )


def _iface_alive(iface: Any) -> bool:
    """Is a ``SerialInterface`` still RECEIVING? (125-followup, 2026-09-02.)

    The library's "stream reader" thread dies on a serial disconnect (macOS Errno 6,
    a cable/power glitch) and clears ``iface.isConnected`` — but ``iface.nodes`` keeps
    returning its cached dict, so a SUCCESSFUL node-table read is NOT proof the radio is
    still heard. An undetected reader-thread death left the station deaf to the whole mesh
    for 91 minutes while the daemon still reported "attached". Both signals are checked so
    a library rename of either degrades safely (unknown ⇒ assumed alive, never a false
    disconnect); the daemon polls this every tick.
    """

    thread = getattr(iface, "_rxThread", None)
    if thread is not None and hasattr(thread, "is_alive") and not thread.is_alive():
        return False
    connected = getattr(iface, "isConnected", None)
    if connected is not None and hasattr(connected, "is_set"):
        return bool(connected.is_set())
    return True


def _open_serial(port: str, timeout: float) -> MeshConnection:
    """The real connection: a ``SerialInterface`` wrapper (lazy import — opt-in extra)."""

    import meshtastic.serial_interface
    from meshtastic.mesh_interface import MeshInterface as _MeshInterface

    iface = meshtastic.serial_interface.SerialInterface(
        devPath=port, timeout=max(1, int(timeout))
    )

    class _Serial:
        def subscribe_packets(self, callback: Callable[[dict[str, Any]], None]) -> None:
            # 123: pypubsub holds listeners WEAKLY — a subscription with no strong
            # reference is silently garbage-collected and the daemon would stop hearing
            # packets with no error. The wrapper keeps every listener alive for the
            # connection's lifetime.
            from pubsub import pub  # the meshtastic library's own event bus

            def _on_receive(packet: Any = None, interface: Any = None) -> None:
                if interface is not iface or not isinstance(packet, dict):
                    return
                callback(packet)

            keep = getattr(self, "_keep_listeners", None)
            if keep is None:
                keep = []
                self._keep_listeners = keep
            keep.append(_on_receive)
            pub.subscribe(_on_receive, "meshtastic.receive")

        def is_alive(self) -> bool:
            return _iface_alive(iface)
        def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]:
            my = ""
            info = iface.getMyNodeInfo()
            if isinstance(info, dict):
                user = info.get("user")
                if isinstance(user, dict):
                    my = str(user.get("id") or "")
            nodes = iface.nodes if isinstance(iface.nodes, dict) else {}
            return my, {str(k): v for k, v in nodes.items() if isinstance(v, dict)}

        def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
            # sendPosition(wantResponse=True) blocks in waitForPosition() up to the
            # interface timeout and raises MeshInterfaceError when nothing came back;
            # the reply itself lands in the node DB, so read it back from there.
            # channel_index picks the channel the tracker answers position on — a request
            # on the wrong channel times out silently (field 2026-08-30).
            #
            # 121: we substitute our own response handler first. The library's
            # onResponsePosition calls our_exit() -> sys.exit() when the response is a
            # ROUTING_APP error. That runs on the interface's "stream reader" daemon thread,
            # so it kills the reader silently rather than the process — nothing then ever
            # sets receivedPosition, waitForPosition burns the FULL deadline, and the caller
            # is told "timed out" when the far end actually answered and declined.
            # sendPosition reads self.onResponsePosition at call time, so an instance
            # attribute shadows the method cleanly.
            refusal: list[str] = []

            def on_response(packet: Any) -> None:
                decoded = packet.get("decoded") if isinstance(packet, dict) else None
                decoded = decoded if isinstance(decoded, dict) else {}
                portnum = decoded.get("portnum")
                if portnum == "POSITION_APP":
                    iface._acknowledgment.receivedPosition = True
                    return
                if portnum == "ROUTING_APP":
                    routing = decoded.get("routing")
                    reason = ""
                    if isinstance(routing, dict):
                        reason = str(routing.get("errorReason") or "")
                    refusal.append(reason or "routing error")
                    # Unblock the wait immediately: the answer has arrived, it is just not a
                    # position. Waiting out the deadline would report silence for a reply.
                    iface._acknowledgment.receivedPosition = True

            iface.onResponsePosition = on_response
            try:
                iface.sendPosition(
                    destinationId=node_id, wantResponse=True, channelIndex=channel_index
                )
            except _MeshInterface.MeshInterfaceError:
                if refusal:
                    raise _RoutingRefusal(refusal[0]) from None
                return None
            if refusal:
                raise _RoutingRefusal(refusal[0])
            nodes = iface.nodes if isinstance(iface.nodes, dict) else {}
            entry = nodes.get(node_id)
            position = entry.get("position") if isinstance(entry, dict) else None
            return position if isinstance(position, dict) else {}

        def close(self) -> None:
            iface.close()

    return _Serial()


class MeshDriver:
    """Total, typed-outcome access to the attached Meshtastic radio (never raises)."""

    def __init__(
        self,
        settings: MeshtasticSettings,
        *,
        connection_factory: ConnectionFactory | None = None,
        find_ports: Callable[[], list[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._factory = connection_factory or _open_serial
        self._find_ports = find_ports or _default_find_ports

    def _claimed(self, port: str) -> MeshFailure | None:
        """A refusal when ``port`` belongs to another obi subsystem, else ``None``.

        The one guard that must run on EVERY branch, configured or discovered (contract
        G2/G3): an operator typo must not be able to key the transmitter either. It runs
        before any candidate reaches the connection factory (G4), because the harm here is
        the ``open()`` itself, not anything obi subsequently sends.
        """

        if port in self._settings.excluded_ports:
            return MeshFailure(
                MeshOutcome.NO_RADIO,
                f"{port} belongs to the radio station (opening it would key the "
                "transmitter) — the mesh radio is elsewhere or absent",
            )
        return None

    def resolve_port(self) -> str | MeshFailure:
        """The serial device to use: config override first, else UNAMBIGUOUS USB discovery.

        Both branches pass through :meth:`_claimed`, so no path can hand back a device the
        radio station owns (feature 121, contracts/mesh-port-safety.md).
        """

        if self._settings.port is not None:
            return self._claimed(self._settings.port) or self._settings.port
        try:
            ports = self._find_ports()
        except Exception as exc:  # noqa: BLE001 - total: discovery failure is a typed outcome
            return _classify(exc)
        if not ports:
            return MeshFailure(
                MeshOutcome.NO_RADIO, "no Meshtastic USB device found on this machine"
            )
        if len(ports) > 1:
            return MeshFailure(
                MeshOutcome.NO_RADIO,
                "several Meshtastic-looking USB devices found ("
                + ", ".join(sorted(ports))
                + ") — set [meshtastic] port to pick one",
            )
        return self._claimed(ports[0]) or ports[0]

    def snapshot(
        self, port: str, *, timeout: float | None = None
    ) -> MeshSnapshot | MeshFailure:
        """One read of the node DB. Pure read — transmits nothing.

        ``timeout`` defaults to ``[meshtastic] snapshot_timeout_s`` rather than a literal, so
        the deadline cannot be silently omitted the way the old hardcoded ``10.0`` parameter
        default was by BOTH callers — which is how the read's cost stayed out of the operator's
        worst-case arithmetic until feature 121. Same shape as ``request_channel_index``: the
        driver holds the settings, so it does not need to be told.
        """

        timeout = self._settings.snapshot_timeout_s if timeout is None else timeout
        try:
            # The context manager records the command (success or error) and re-raises;
            # classification into a typed outcome happens out here — the driver stays total.
            with timed_device_command(port, "meshtastic-serial", "read node db"):
                connection = self._factory(port, timeout)
                try:
                    my_node_id, raw_nodes = connection.read_nodes()
                finally:
                    self._close_quietly(connection)
        except Exception as exc:  # noqa: BLE001 - total: every failure is a typed outcome
            return _classify(exc)
        return snapshot_from_nodes(port, my_node_id, raw_nodes)

    def request_position(
        self, port: str, node_id: str, *, deadline_s: float
    ) -> MeshPosition | MeshFailure:
        """THE one transmitting call: one position query to one node, hard local deadline.

        Callers (the engine's rate-limited request path only) have already consulted the
        durable limiter; this method sends exactly once and never retries — a retry is a
        second transmission, and asking again is the caller's decision.
        """

        try:
            with timed_device_command(port, "meshtastic-serial", f"position request {node_id}"):
                connection = self._factory(port, deadline_s)
                try:
                    reply = connection.request_position(
                        node_id, self._settings.request_channel_index
                    )
                finally:
                    self._close_quietly(connection)
        except _RoutingRefusal as refusal:
            # Must precede the generic handler: this is an ANSWER, not a failure to reach.
            return MeshFailure(
                MeshOutcome.REFUSED,
                f"{node_id} declined the position request ({refusal})",
            )
        except Exception as exc:  # noqa: BLE001 - total: every failure is a typed outcome
            return _classify(exc)
        if reply is None:
            return MeshFailure(
                MeshOutcome.TIMED_OUT,
                f"no position reply from {node_id} within {deadline_s:.0f}s",
            )
        position = _parse_position(reply)
        if position is None:
            return MeshFailure(
                MeshOutcome.ERROR, f"{node_id} answered without usable coordinates"
            )
        return position

    @staticmethod
    def _close_quietly(connection: MeshConnection) -> None:
        # The port must never take the answer down with it.
        with contextlib.suppress(Exception):
            connection.close()
