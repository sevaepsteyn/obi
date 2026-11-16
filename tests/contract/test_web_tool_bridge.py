"""Contract tests: POST /api/tool — the web tool bridge (feature 048).

Real behavioral tests against the real TLS server. Each pins a sentence of
specs/048-web-net-tools/contracts/web-tool-bridge.md. No device is contacted: the allowlisted
tools resolve their source from the (empty, in a tmp XDG dir) inventory and return their own text
before any connect — proving the bridge dispatches to the real obi tool without a second
implementation and without touching the network (constitution I/II).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from obi.core.config import ChatSettings, Config, Secret
from obi.core.models import DiscoveryScope
from obi.web.server import create_server
from obi.web.tool_api import WEB_TOOL_NAMES, validate_web_tools
from tests.fakes import settings as mikrotik_settings


def _config(tmp_path: Path, *, routeros: bool) -> Config:
    return Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]) if routeros else None,
        routeros=mikrotik_settings() if routeros else None,
        # [logs] presence is irrelevant to the bridge; a logs-only config still has no driver.
        logs=None if routeros else _LogsMarker(tmp_path),
        chat=ChatSettings(api_key=Secret("sk-test")),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",  # absent ⇒ the tool returns _NO_INVENTORY
    )


def _LogsMarker(tmp_path: Path):  # noqa: N802 - reads as a constructor at the call site
    from obi.core.config import LogsSettings

    return LogsSettings(db_path=tmp_path / "logs.db", log_tz="America/Los_Angeles")


def _serve(tmp_path: Path, config: Config) -> Iterator[str]:
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1",
        port=0,
        export_path=export_path,
        state_dir=tmp_path / "state",
        configs_app_config=config,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def base(tmp_path: Path) -> Iterator[str]:
    """A server whose config HAS [routeros] but an empty inventory (no device is reachable)."""

    yield from _serve(tmp_path, _config(tmp_path, routeros=True))


def _post(base: str, body: object) -> httpx.Response:
    return httpx.post(
        f"{base}/api/tool",
        content=body if isinstance(body, (bytes, str)) else json.dumps(body),
        headers={"content-type": "application/json"},
        verify=False,
        timeout=10.0,
    )


# --- the allowlist runs the real tool (no device contact) -------------------------------------


def _ping(device: str = "gw", destination: str = "8.8.8.8") -> dict[str, object]:
    return {"tool": "routeros_ping", "args": {"device": device, "destination": destination}}


def test_allowlisted_ping_dispatches_to_the_real_tool(base: str) -> None:
    resp = _post(base, _ping())
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["tool"] == "routeros_ping"
    # Empty inventory ⇒ the tool's own _NO_INVENTORY text — it ran, and touched no device.
    assert "inventory" in data["text"].lower()


def test_allowlisted_traceroute_dispatches(base: str) -> None:
    resp = _post(
        base,
        {"tool": "routeros_traceroute", "args": {"device": "gw", "destination": "1.1.1.1"}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_tool_level_error_is_a_200_not_a_crash(base: str) -> None:
    # Missing required arg ⇒ the tool's own "error …" string, still HTTP 200 (FR-010).
    resp = _post(base, {"tool": "routeros_ping", "args": {"device": "gw"}})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["text"].startswith("error")


# --- the allowlist is the security boundary ----------------------------------------------------


def test_off_allowlist_tool_is_refused(base: str) -> None:
    # config_query is a real obi tool for the LLM — but not exposed to the web surface.
    resp = _post(base, {"tool": "config_query", "args": {}})
    assert resp.status_code == 403
    body = resp.json()
    assert body["ok"] is False
    assert "not available" in body["error"]


def test_unknown_tool_is_refused(base: str) -> None:
    assert _post(base, {"tool": "rm_rf", "args": {}}).status_code == 403


# --- malformed requests are 4xx ----------------------------------------------------------------


def test_non_json_body_is_400(base: str) -> None:
    resp = _post(base, b"not json at all")
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_non_object_json_is_400(base: str) -> None:
    assert _post(base, [1, 2, 3]).status_code == 400


def test_missing_tool_is_400(base: str) -> None:
    resp = _post(base, {"args": {"destination": "8.8.8.8"}})
    assert resp.status_code == 400
    assert "tool" in resp.json()["error"]


def test_args_not_object_is_400(base: str) -> None:
    resp = _post(base, {"tool": "routeros_ping", "args": "8.8.8.8"})
    assert resp.status_code == 400
    assert "args" in resp.json()["error"]


def test_get_is_405(base: str) -> None:
    assert httpx.get(f"{base}/api/tool", verify=False, timeout=5.0).status_code == 405


# --- routeros-absent degrades (does not crash) -------------------------------------------------


def test_routeros_absent_degrades_to_the_tool_message(tmp_path: Path) -> None:
    gen = _serve(tmp_path, _config(tmp_path, routeros=False))
    base = next(gen)
    try:
        resp = _post(base, _ping())
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "[routeros]" in data["text"]  # _NO_MIKROTIK — a clear message, not a 500
    finally:
        gen.close()


# --- the allowlist invariants (contract §Invariants) -------------------------------------------


def test_allowlist_invariants_hold() -> None:
    validate_web_tools()  # every name is a real, read-only tool (raises on violation)


def test_allowlist_is_exactly_the_two_net_tools() -> None:
    assert WEB_TOOL_NAMES == ("routeros_ping", "routeros_traceroute")
