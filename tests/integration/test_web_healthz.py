"""The web server's ``/healthz`` liveness route and the ``probe_web`` client (feature 081).

One in-process TLS server on an ephemeral port (the 022 web-server test pattern); the probe is the
exact path ``obi web status`` uses to tell "answering" from "listening but broken".
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from obi.core import service as svc
from obi.web.server import create_server


@pytest.fixture
def served(tmp_path: Path) -> Iterator[ThreadingHTTPServer]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export_path, state_dir=tmp_path / "state"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def test_healthz_get_returns_ok(served: ThreadingHTTPServer) -> None:
    port = served.server_address[1]
    resp = httpx.get(f"https://127.0.0.1:{port}/healthz", verify=False, timeout=5.0)
    assert resp.status_code == 200
    assert resp.text == "ok"
    assert resp.headers["content-type"].startswith("text/plain")


def test_healthz_head_ok(served: ThreadingHTTPServer) -> None:
    port = served.server_address[1]
    resp = httpx.head(f"https://127.0.0.1:{port}/healthz", verify=False, timeout=5.0)
    assert resp.status_code == 200


def test_healthz_rejects_post(served: ThreadingHTTPServer) -> None:
    port = served.server_address[1]
    resp = httpx.post(f"https://127.0.0.1:{port}/healthz", verify=False, timeout=5.0)
    assert resp.status_code == 405


def test_probe_web_healthy_against_real_server(served: ThreadingHTTPServer) -> None:
    port = served.server_address[1]
    live = svc.probe_web("127.0.0.1", port)
    assert live.reachable and live.status == 200 and live.healthy


def test_probe_web_refused_when_nothing_listening() -> None:
    live = svc.probe_web("127.0.0.1", _free_port(), timeout=1.0)
    assert not live.reachable and not live.healthy
