"""Unit (feature 123): client↔daemon over a real Unix socket — deadlines, mapping, O4/O8.

The daemon core runs with the locator tests' fake connection; the RPC server thread and
the client speak the actual JSON-lines transport over a socket in tmp_path. Deadline
tests use a black-hole socket that accepts and never answers.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from obi.core.audit import AuditTrail
from obi.core.brcaddr import CityModel
from obi.core.config import MeshtasticSettings
from obi.core.drivers.meshtastic import MeshFailure, MeshOutcome, MeshPosition, MeshSnapshot
from obi.core.meshclient import MeshDaemonClient
from obi.core.meshdaemon import MeshDaemon, _RpcHandler, _RpcServer
from tests.fixtures.brc.origin import ORIGIN

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "meshtastic"

_NOW = 1_787_983_900.0

_DIAL_ONLY_MODEL = CityModel(
    origin=ORIGIN, az12_deg=45.0, orientation_source="plaza-fit", segments=(), anchors=()
)


class _Connection:
    def __init__(self, position_reply: dict[str, Any] | None = None) -> None:
        raw = json.loads((_FIXTURES / "nodes.json").read_text())
        self.my_node_id = str(raw["myNodeId"])
        self.nodes: dict[str, dict[str, Any]] = dict(raw["nodes"])
        self.position_reply = position_reply

    def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]:
        return self.my_node_id, self.nodes

    def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
        return self.position_reply

    def close(self) -> None:
        pass


@pytest.fixture()
def socket_dir() -> Iterator[Path]:
    """A SHORT directory for the Unix socket: macOS caps AF_UNIX paths at ~104 bytes and
    pytest's tmp_path (test name and all) routinely blows past it."""

    path = Path(tempfile.mkdtemp(prefix="meshd-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _settings(tmp_path: Path, socket_dir: Path) -> MeshtasticSettings:
    return MeshtasticSettings(
        port="/dev/cu.usbmodem-test",
        positions_db_path=tmp_path / "positions.db",
        daemon_socket=socket_dir / "daemon.sock",
        report_only=["TUMA", "AshH"],
    )


@pytest.fixture()
def served_daemon(
    tmp_path: Path, socket_dir: Path
) -> Iterator[tuple[MeshDaemon, MeshtasticSettings]]:
    """An attached daemon core with its RPC server live on a short-path socket."""

    settings = _settings(tmp_path, socket_dir)
    connection = _Connection(
        position_reply={"latitudeI": 407788431, "longitudeI": -1148232973, "time": int(_NOW)}
    )
    daemon = MeshDaemon(
        settings,
        AuditTrail(tmp_path / "audit.jsonl"),
        connection_factory=lambda port, timeout: connection,
        model_loader=lambda: _DIAL_ONLY_MODEL,
        now=lambda: _NOW,
    )
    daemon._attach()
    server = _RpcServer(str(settings.daemon_socket), _RpcHandler)
    server.daemon_ref = daemon
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield daemon, settings
    finally:
        server.shutdown()
        server.server_close()


def test_snapshot_round_trip(served_daemon: tuple[MeshDaemon, MeshtasticSettings]) -> None:
    _daemon, settings = served_daemon
    client = MeshDaemonClient(settings, source="test")
    port = client.resolve_port()
    assert port == "/dev/cu.usbmodem-test"
    snapshot = client.snapshot(port)
    assert isinstance(snapshot, MeshSnapshot)
    assert snapshot.my_node_id == "!f4dcd459"
    by_id = {node.node_id: node for node in snapshot.nodes}
    # Field-for-field round trip: names, heard metadata, battery, position, self flag.
    tuma = by_id["!91378e3a"]
    assert tuma.short_name == "TUMA"
    assert tuma.last_heard == 1_787_983_799
    ash = by_id["!38568530"]
    assert ash.position is not None
    self_node = by_id["!f4dcd459"]
    assert self_node.is_self is True


def test_concurrent_snapshots_all_answer(
    served_daemon: tuple[MeshDaemon, MeshtasticSettings],
) -> None:
    _daemon, settings = served_daemon
    results: list[Any] = []

    def lookup() -> None:
        client = MeshDaemonClient(settings, source="test")
        results.append(client.snapshot("/dev/cu.usbmodem-test"))

    threads = [threading.Thread(target=lookup) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)
    assert len(results) == 6
    assert all(isinstance(result, MeshSnapshot) for result in results)


def test_request_position_round_trip(
    served_daemon: tuple[MeshDaemon, MeshtasticSettings],
) -> None:
    _daemon, settings = served_daemon
    client = MeshDaemonClient(settings, source="test")
    position = client.request_position("/dev/cu.usbmodem-test", "!91378e3a", deadline_s=12.0)
    assert isinstance(position, MeshPosition)
    assert abs(position.latitude - 40.7788431) < 1e-9


def test_timed_out_and_refused_map_to_typed_failures(
    served_daemon: tuple[MeshDaemon, MeshtasticSettings],
) -> None:
    daemon, settings = served_daemon
    client = MeshDaemonClient(settings, source="test")
    # None reply ⇒ TIMED_OUT (the driver-classification contract carried over RPC).
    with daemon._lock:
        connection = daemon._connection
    assert connection is not None
    connection.position_reply = None  # type: ignore[attr-defined]
    result = client.request_position("/dev/cu.usbmodem-test", "!91378e3a", deadline_s=2.0)
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.TIMED_OUT


def test_standby_resume_over_the_wire(
    served_daemon: tuple[MeshDaemon, MeshtasticSettings],
) -> None:
    _daemon, settings = served_daemon
    client = MeshDaemonClient(settings, source="test")
    status = client.standby(600)
    assert isinstance(status, dict)
    assert status["state"] == "standby"
    # resolve_port still resolves in standby — snapshots stay answerable (honest, aged) —
    # while a request gets the daemon's typed "on loan" refusal.
    assert client.resolve_port() == "/dev/cu.usbmodem-test"
    refusal = client.request_position("/dev/cu.usbmodem-test", "!91378e3a", deadline_s=2.0)
    assert isinstance(refusal, MeshFailure)
    assert refusal.outcome is MeshOutcome.NO_RADIO
    resumed = client.resume()
    assert isinstance(resumed, dict)
    assert resumed["state"] == "attached"


def test_dead_socket_is_daemon_down_not_a_hang(tmp_path: Path, socket_dir: Path) -> None:
    """O8: nothing listening ⇒ typed DAEMON_DOWN, immediately."""

    client = MeshDaemonClient(_settings(tmp_path, socket_dir), source="test")
    start = time.monotonic()
    result = client.resolve_port()
    elapsed = time.monotonic() - start
    assert isinstance(result, MeshFailure)
    assert result.outcome is MeshOutcome.DAEMON_DOWN
    assert elapsed < 2.0


def test_black_hole_socket_hits_the_deadline(tmp_path: Path, socket_dir: Path) -> None:
    """O4 client-side: a server that accepts and never answers cannot hang a consumer."""

    settings = _settings(tmp_path, socket_dir)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(settings.daemon_socket))
    listener.listen(1)
    try:
        client = MeshDaemonClient(settings, source="test", connect_timeout_s=0.5)
        # Shrink the control read deadline via a tiny op deadline path: status uses the
        # 5 s control deadline, so bound the whole call and assert the typed outcome.
        start = time.monotonic()
        result = client.status()
        elapsed = time.monotonic() - start
        assert isinstance(result, MeshFailure)
        assert result.outcome is MeshOutcome.DAEMON_DOWN
        assert elapsed < 7.0  # the 5 s read deadline, not forever
    finally:
        listener.close()


def test_blank_source_rejected_over_the_wire(
    served_daemon: tuple[MeshDaemon, MeshtasticSettings],
) -> None:
    _daemon, settings = served_daemon
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5.0)
        sock.connect(str(settings.daemon_socket))
        sock.sendall(b'{"id": "x", "op": "status", "source": ""}\n')
        reply = json.loads(sock.makefile().readline())
    assert reply["ok"] is False
    assert reply["detail"] == "missing source"


def test_malformed_json_gets_one_error_then_close(
    served_daemon: tuple[MeshDaemon, MeshtasticSettings],
) -> None:
    _daemon, settings = served_daemon
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5.0)
        sock.connect(str(settings.daemon_socket))
        sock.sendall(b"this is not json\n")
        stream = sock.makefile()
        reply = json.loads(stream.readline())
        assert reply["ok"] is False
        assert stream.readline() == ""  # server closed the connection
