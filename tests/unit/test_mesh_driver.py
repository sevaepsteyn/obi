"""Unit (feature 111): the Meshtastic driver — parsing, port discovery, typed failures.

Runs entirely offline against the 2026-08-28 live-capture fixtures through the injectable
connection factory; no test ever opens a serial port or transmits. Pins the driver's
total-function contract: every failure is a :class:`MeshFailure`, never an exception.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from obi.core.config import MeshtasticSettings
from obi.core.drivers.meshtastic import (
    MeshDriver,
    MeshFailure,
    MeshOutcome,
    MeshPosition,
    MeshSnapshot,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "meshtastic"


def _fixture_nodes() -> tuple[str, dict[str, dict[str, Any]]]:
    raw = json.loads((_FIXTURES / "nodes.json").read_text())
    return str(raw["myNodeId"]), dict(raw["nodes"])


def _fixture_position() -> dict[str, Any]:
    raw = json.loads((_FIXTURES / "position_response.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


class FakeConnection:
    """A scripted stand-in for the serial interface: node DB + optional position reply."""

    def __init__(
        self,
        *,
        my_node_id: str = "!f4dcd459",
        nodes: dict[str, dict[str, Any]] | None = None,
        position_reply: dict[str, Any] | None = None,
        reply_times_out: bool = False,
    ) -> None:
        my, fixture = _fixture_nodes()
        self.my_node_id = my_node_id if nodes is None else my_node_id
        self.nodes = fixture if nodes is None else nodes
        self.position_reply = position_reply
        self.reply_times_out = reply_times_out
        self.closed = False
        self.position_requests: list[str] = []
        self.channel_indexes: list[int] = []

    def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]:
        return self.my_node_id, self.nodes

    def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
        self.position_requests.append(node_id)
        self.channel_indexes.append(channel_index)
        if self.reply_times_out:
            return None
        return self.position_reply

    def close(self) -> None:
        self.closed = True


def _driver(
    connection: FakeConnection | Exception,
    *,
    settings: MeshtasticSettings | None = None,
    ports: list[str] | Exception | None = None,
) -> MeshDriver:
    def factory(port: str, timeout: float) -> FakeConnection:
        if isinstance(connection, Exception):
            raise connection
        return connection

    def find_ports() -> list[str]:
        if isinstance(ports, Exception):
            raise ports
        return ports if ports is not None else ["/dev/cu.usbmodem11301"]

    return MeshDriver(
        settings or MeshtasticSettings(),
        connection_factory=factory,
        find_ports=find_ports,
    )


# --- node-DB parsing ---------------------------------------------------------------------


def test_snapshot_parses_the_live_capture() -> None:
    conn = FakeConnection()
    driver = _driver(conn)
    snap = driver.snapshot("/dev/cu.usbmodem11301")
    assert isinstance(snap, MeshSnapshot)
    assert snap.my_node_id == "!f4dcd459"
    by_id = {n.node_id: n for n in snap.nodes}
    assert len(by_id) == 5

    tuma = by_id["!91378e3a"]
    assert tuma.short_name == "TUMA"
    assert tuma.long_name == "Tooma 9831"
    assert tuma.position is None
    assert tuma.hops_away == 0
    assert tuma.snr == 13.5
    assert tuma.battery_level is None
    assert tuma.is_self is False

    me = by_id["!f4dcd459"]
    assert me.is_self is True
    assert me.battery_level == 101
    assert me.position is not None
    assert me.position.latitude == pytest.approx(40.778895)
    assert me.position.longitude == pytest.approx(-114.8233351)
    assert me.position.altitude_m == 1196
    assert me.position.fix_time == 1787984606

    tracker = by_id["!38568530"]
    assert tracker.hops_away == 2
    assert tracker.battery_level == 78
    assert tracker.position is not None

    default_named = by_id["!ed210303"]
    assert default_named.long_name == "Meshtastic 0303"
    assert default_named.hw_model == "UNSET"

    assert conn.closed is True


def test_garbage_position_becomes_none_not_coordinates() -> None:
    # (0, 0) is "no fix", never a place in the Gulf of Guinea.
    snap = _driver(FakeConnection()).snapshot("p")
    assert isinstance(snap, MeshSnapshot)
    garbage = next(n for n in snap.nodes if n.node_id == "!bb466561")
    assert garbage.position is None


def test_snapshot_survives_malformed_node_entries() -> None:
    nodes = {
        "!00000001": {"user": "not-a-dict"},
        "!00000002": {"num": 2, "user": {"id": "!00000002"}, "position": "garbage"},
    }
    snap = _driver(FakeConnection(nodes=nodes)).snapshot("p")
    assert isinstance(snap, MeshSnapshot)
    parsed = {n.node_id for n in snap.nodes}
    assert "!00000002" in parsed  # unusable position degrades to None, node survives
    two = next(n for n in snap.nodes if n.node_id == "!00000002")
    assert two.position is None


# --- port discovery ----------------------------------------------------------------------


def test_configured_port_override_wins() -> None:
    driver = _driver(FakeConnection(), settings=MeshtasticSettings(port="/dev/cu.custom"))
    assert driver.resolve_port() == "/dev/cu.custom"


def test_unambiguous_discovery_is_used() -> None:
    driver = _driver(FakeConnection(), ports=["/dev/cu.usbmodem11301"])
    assert driver.resolve_port() == "/dev/cu.usbmodem11301"


def test_no_ports_is_no_radio() -> None:
    result = _driver(FakeConnection(), ports=[]).resolve_port()
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.NO_RADIO


def test_ambiguous_ports_refuse_rather_than_guess() -> None:
    result = _driver(FakeConnection(), ports=["/dev/a", "/dev/b"]).resolve_port()
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.NO_RADIO
    assert "/dev/a" in result.detail and "/dev/b" in result.detail


def test_missing_library_names_the_extra() -> None:
    err = ImportError("No module named 'meshtastic'")
    result = _driver(FakeConnection(), ports=err).resolve_port()
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.NO_RADIO
    assert "uv sync --extra mesh" in result.detail


# --- open/connect failures ---------------------------------------------------------------


def test_busy_port_is_port_busy() -> None:
    err = RuntimeError("could not exclusively lock port /dev/cu.usbmodem11301: Resource busy")
    result = _driver(err).snapshot("/dev/cu.usbmodem11301")
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.PORT_BUSY


def test_other_open_failure_is_error_never_raises() -> None:
    result = _driver(RuntimeError("boom")).snapshot("p")
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.ERROR
    assert "boom" in result.detail


def test_missing_library_at_connect_names_the_extra() -> None:
    result = _driver(ImportError("No module named 'meshtastic'")).snapshot("p")
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.NO_RADIO
    assert "uv sync --extra mesh" in result.detail


# --- position request --------------------------------------------------------------------


def test_position_request_returns_the_reply() -> None:
    conn = FakeConnection(position_reply=_fixture_position())
    result = _driver(conn).request_position("p", "!91378e3a", deadline_s=5.0)
    assert isinstance(result, MeshPosition)
    assert result.latitude == pytest.approx(40.7788198)
    assert result.longitude == pytest.approx(-114.8232908)
    assert conn.position_requests == ["!91378e3a"]
    assert conn.channel_indexes == [0]  # the default: the primary channel
    assert conn.closed is True


def test_position_request_goes_on_the_configured_channel() -> None:
    # The 2026-08-30 field lesson: a request on the community channel (position sharing
    # off in its shared settings) times out silently; the tracker answers on the private
    # channel. The configured slot must reach sendPosition.
    conn = FakeConnection(position_reply=_fixture_position())
    driver = _driver(conn, settings=MeshtasticSettings(request_channel_index=1))
    result = driver.request_position("p", "!91378e3a", deadline_s=5.0)
    assert isinstance(result, MeshPosition)
    assert conn.channel_indexes == [1]


def test_position_request_timeout_is_typed() -> None:
    conn = FakeConnection(reply_times_out=True)
    result = _driver(conn).request_position("p", "!91378e3a", deadline_s=5.0)
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.TIMED_OUT
    assert conn.closed is True


def test_position_request_garbage_reply_is_error() -> None:
    conn = FakeConnection(position_reply={"latitudeI": 0, "longitudeI": 0})
    result = _driver(conn).request_position("p", "!91378e3a", deadline_s=5.0)
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.ERROR


# --- 121: port safety — the mesh driver must never open the radio station's device --------
#
# Contract: specs/121-mesh-hardening/contracts/mesh-port-safety.md (G1-G8). The harm these
# pin is physical: opening the station's FTDI asserts DTR+RTS, both of which key the
# transmitter (radio/ptt.py, measured on air), and the mesh path has no de-key step.


class _Port:
    """A pyserial ListPortInfo stand-in: only `device` and `vid` are read by discovery."""

    def __init__(self, device: str, vid: int | None) -> None:
        self.device = device
        self.vid = vid


def _discover(ports: list[_Port], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run the REAL discovery function over a faked comports()."""

    list_ports = pytest.importorskip("serial.tools.list_ports")  # the optional radio/mesh extra
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    from obi.core.drivers.meshtastic import _default_find_ports

    return _default_find_ports()


def test_discovery_ignores_the_ftdi_when_no_mesh_radio_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G1: the exact 2026-08-31 shape — RAK unplugged, walkie-talkie attached.

    meshtastic.util.findPorts would return the FTDI here (its vendor filter matches nothing,
    so it falls back to 'every non-blacklisted port'), and resolve_port would have accepted
    it as the sole unambiguous candidate.
    """

    found = _discover([_Port("/dev/cu.usbserial-FBC3793547DF", 0x0403)], monkeypatch)
    assert found == []


def test_discovery_keeps_only_mesh_vendor_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    found = _discover(
        [
            _Port("/dev/cu.usbserial-FBC3793547DF", 0x0403),  # FTDI: the PTT half
            _Port("/dev/cu.usbmodem111301", 0x239A),  # RAK4631
            _Port("/dev/cu.Bluetooth-Incoming-Port", None),  # no vid at all
            _Port("/dev/cu.esp", 0x303A),  # Espressif
        ],
        monkeypatch,
    )
    assert found == ["/dev/cu.esp", "/dev/cu.usbmodem111301"]


def test_discovered_excluded_port_is_refused() -> None:
    """G2: even as the SOLE candidate, a claimed device is never returned."""

    settings = MeshtasticSettings(excluded_ports=("/dev/cu.usbserial-FBC3793547DF",))
    result = _driver(
        FakeConnection(), ports=["/dev/cu.usbserial-FBC3793547DF"], settings=settings
    ).resolve_port()
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.NO_RADIO
    assert "radio station" in result.detail


def test_configured_excluded_port_is_refused() -> None:
    """G3: an operator typo in [meshtastic] port must not be able to key the transmitter."""

    settings = MeshtasticSettings(
        port="/dev/cu.usbserial-FBC3793547DF",
        excluded_ports=("/dev/cu.usbserial-FBC3793547DF",),
    )
    result = _driver(FakeConnection(), settings=settings).resolve_port()
    assert isinstance(result, MeshFailure)
    assert "radio station" in result.detail


def test_refusal_names_the_station_not_a_missing_radio() -> None:
    """G5: distinguishable from 'no mesh radio attached' — different operator action."""

    settings = MeshtasticSettings(excluded_ports=("/dev/cu.usbserial-FBC3793547DF",))
    refused = _driver(
        FakeConnection(), ports=["/dev/cu.usbserial-FBC3793547DF"], settings=settings
    ).resolve_port()
    absent = _driver(FakeConnection(), ports=[]).resolve_port()
    assert isinstance(refused, MeshFailure) and isinstance(absent, MeshFailure)
    assert refused.detail != absent.detail
    assert "key the transmitter" in refused.detail
    assert "no Meshtastic USB device" in absent.detail


def test_excluded_port_never_reaches_the_connection_factory() -> None:
    """G4: the harm is the open() itself, so refusal must precede any factory call."""

    opened: list[str] = []

    def exploding_factory(port: str, timeout: float) -> FakeConnection:
        opened.append(port)
        raise AssertionError(f"the driver opened a claimed device: {port}")

    driver = MeshDriver(
        MeshtasticSettings(excluded_ports=("/dev/cu.usbserial-FBC3793547DF",)),
        connection_factory=exploding_factory,
        find_ports=lambda: ["/dev/cu.usbserial-FBC3793547DF"],
    )
    port = driver.resolve_port()
    assert isinstance(port, MeshFailure)
    assert opened == []


def test_exclusion_does_not_swallow_ambiguity() -> None:
    """G8: filtering must not turn 'two candidates' into a silent pick."""

    settings = MeshtasticSettings(excluded_ports=("/dev/cu.usbserial-FBC3793547DF",))
    result = _driver(
        FakeConnection(), ports=["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"], settings=settings
    ).resolve_port()
    assert isinstance(result, MeshFailure)
    assert "set [meshtastic] port" in result.detail


def test_empty_exclusion_leaves_behaviour_unchanged() -> None:
    """FR-006: an installation with no [radio] section behaves exactly as before."""

    driver = _driver(FakeConnection(), ports=["/dev/cu.usbmodem111301"])
    assert driver.resolve_port() == "/dev/cu.usbmodem111301"


# --- 121: the node-table read deadline is configured, not hardcoded -----------------------


def test_snapshot_uses_the_configured_deadline() -> None:
    """The old hardcoded 10.0 default was passed by neither caller, so it was invisible to
    the operator and absent from the documented worst case."""

    seen: list[float] = []

    def recording_factory(port: str, timeout: float) -> FakeConnection:
        seen.append(timeout)
        return FakeConnection()

    driver = MeshDriver(
        MeshtasticSettings(snapshot_timeout_s=3.5),
        connection_factory=recording_factory,
        find_ports=lambda: ["/dev/cu.usbmodem111301"],
    )
    driver.snapshot("/dev/cu.usbmodem111301")
    assert seen == [3.5]


def test_snapshot_explicit_timeout_still_wins() -> None:
    seen: list[float] = []

    def recording_factory(port: str, timeout: float) -> FakeConnection:
        seen.append(timeout)
        return FakeConnection()

    driver = MeshDriver(
        MeshtasticSettings(snapshot_timeout_s=3.5),
        connection_factory=recording_factory,
        find_ports=lambda: ["/dev/cu.usbmodem111301"],
    )
    driver.snapshot("/dev/cu.usbmodem111301", timeout=1.0)
    assert seen == [1.0]


# --- 121: an explicit refusal is not a timeout --------------------------------------------


def test_routing_refusal_is_reported_as_refused() -> None:
    """The far end answered and declined. Before 121 this was indistinguishable from silence
    AND cost the full deadline, because the library's own handler calls sys.exit() on the
    reader thread and nothing ever unblocks the wait."""

    from obi.core.drivers.meshtastic import _RoutingRefusal

    class RefusingConnection(FakeConnection):
        def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
            raise _RoutingRefusal("NO_RESPONSE")

    driver = _driver(RefusingConnection(), settings=MeshtasticSettings())
    result = driver.request_position("/dev/cu.usbmodem111301", "!91378e3a", deadline_s=12)
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.REFUSED
    assert "declined" in result.detail
    assert "NO_RESPONSE" in result.detail


def test_silent_far_end_still_times_out() -> None:
    """SC-007: the real-timeout path keeps its existing outcome and wording."""

    driver = _driver(FakeConnection(reply_times_out=True))
    result = driver.request_position("/dev/cu.usbmodem111301", "!91378e3a", deadline_s=12)
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.TIMED_OUT
    assert "no position reply" in result.detail


# --- RX-liveness of a held interface (125-followup, 2026-09-02) --------------------------


def test_iface_alive_reads_thread_and_connected_event() -> None:
    import threading

    from obi.core.drivers.meshtastic import _iface_alive

    class _Thread:
        def __init__(self, alive: bool) -> None:
            self._alive = alive

        def is_alive(self) -> bool:
            return self._alive

    class _Iface:
        def __init__(self, thread_alive: bool, connected: bool) -> None:
            self._rxThread = _Thread(thread_alive)
            self.isConnected = threading.Event()
            if connected:
                self.isConnected.set()

    assert _iface_alive(_Iface(thread_alive=True, connected=True)) is True
    # The observed failure: reader thread dead → not alive, even though nodes still read.
    assert _iface_alive(_Iface(thread_alive=False, connected=True)) is False
    # Disconnect clears the event.
    assert _iface_alive(_Iface(thread_alive=True, connected=False)) is False
    # Unknown shape (library rename) degrades to "assumed alive", never a false disconnect.
    assert _iface_alive(object()) is True
