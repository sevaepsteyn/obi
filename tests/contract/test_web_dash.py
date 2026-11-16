"""Contract tests: GET /api/dash.json (feature 095, contracts/dash-surfaces.md).

Real TLS server + a real board written through the production dash model. The route is camp 1
at its dumbest — a local FILE read: no device I/O, no evaluation, absent-anything is HTTP 200
with a hint naming the fixing command.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx

from obi.core import dash
from obi.core.config import DashSettings
from obi.web.server import create_server

NOW_US = 1_787_000_000_000_000


def _board() -> dash.Board:
    tile = dash.Tile(
        signal="fleet",
        state="red",
        items=[
            dash.Item(
                key="tamarack-gw", label="tamarack-gw", state="red", detail="down 14m (172.18.89.1)"
            )
        ],
        cadence_s=60,
        data_age_s=4.0,
    )
    return dash.Board(generated_at_us=NOW_US, state="red", tiles=[tile], notes=["seeded"])


def _serve(tmp_path: Path, settings: DashSettings | None) -> Iterator[str]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-08-25T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        dash_settings=settings,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def test_serves_the_persisted_board_verbatim(tmp_path: Path) -> None:
    state_dir = tmp_path / "dash"
    board = _board()
    dash.write_board(state_dir, board)
    for base in _serve(tmp_path, DashSettings(state_dir=state_dir)):
        response = httpx.get(f"{base}/api/dash.json", verify=False)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        payload = response.json()
        assert payload["store"] == "ok"
        assert payload["hint"] is None
        assert payload["board"] == board.to_json()  # verbatim — the route adds no judgment


def test_absent_config_is_a_state_not_an_error(tmp_path: Path) -> None:
    for base in _serve(tmp_path, None):
        payload = httpx.get(f"{base}/api/dash.json", verify=False).json()
        assert payload["store"] == "absent"
        assert "[dash]" in payload["hint"]


def test_missing_board_names_the_fixing_command(tmp_path: Path) -> None:
    for base in _serve(tmp_path, DashSettings(state_dir=tmp_path / "dash")):
        payload = httpx.get(f"{base}/api/dash.json", verify=False).json()
        assert payload["store"] == "absent"
        assert "obi dash run" in payload["hint"]


def test_head_and_post_semantics(tmp_path: Path) -> None:
    state_dir = tmp_path / "dash"
    dash.write_board(state_dir, _board())
    for base in _serve(tmp_path, DashSettings(state_dir=state_dir)):
        head = httpx.head(f"{base}/api/dash.json", verify=False)
        assert head.status_code == 200
        assert head.content == b""
        post = httpx.post(f"{base}/api/dash.json", verify=False)
        assert post.status_code == 405  # GET/HEAD only — a POST here is a client bug, not a 404


def test_widget_module_is_served(tmp_path: Path) -> None:
    # The one new allowlist entry: /widgets/dash.js is a real served file (⇒ one restart live).
    for base in _serve(tmp_path, None):
        response = httpx.get(f"{base}/widgets/dash.js", verify=False)
        assert response.status_code == 200
        assert "export async function mount" in response.text
