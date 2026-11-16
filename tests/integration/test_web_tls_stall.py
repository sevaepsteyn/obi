"""Integration: a stalled or garbage client must never freeze the web listener (feature 084).

Live failure 2026-08-19 (playa NOC): `obi web serve` alive and idle, every route timing out.
The listening socket was TLS-wrapped with handshake-on-connect, so accept() ran each client's
handshake on the single listener thread — one WiFi client that TCP-connected and went silent
mid-handshake blocked accept() forever. 084 defers the handshake to the worker thread
(``do_handshake_on_connect=False``) and bounds it with the handler's per-connection timeout.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from obi.web.server import create_server


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[str]:
    export = tmp_path / "links.json"
    export.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00", "links": []}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export, state_dir=tmp_path / "state"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_silent_client_does_not_block_other_requests(served: str) -> None:
    """The 2026-08-19 outage shape: connect, send nothing, and the next client must be served.

    Pre-084 this deadlocks — the silent connection is accepted first and the listener thread
    blocks in its TLS handshake, so the real request below times out.
    """

    host, port = served.split(":")
    stalled = socket.create_connection((host, int(port)), timeout=10)
    try:
        response = httpx.get(f"https://{served}/", verify=False, timeout=5.0)
        assert response.status_code == 200
    finally:
        stalled.close()


def test_plaintext_garbage_is_dropped_and_service_continues(served: str) -> None:
    """A plain-HTTP probe against the TLS port (routine scanner noise) raises SSLError in the
    worker; the quiet handle_error drops it in one line and the server keeps answering."""

    host, port = served.split(":")
    probe = socket.create_connection((host, int(port)), timeout=10)
    try:
        probe.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    finally:
        probe.close()
    response = httpx.get(f"https://{served}/links.json", verify=False, timeout=5.0)
    assert response.status_code == 200
