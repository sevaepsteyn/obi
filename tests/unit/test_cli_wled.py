"""Unit (feature 103): the `obi wled` CLI — thin over the lighting engine, honest exits."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core import lighting as lighting_mod
from obi.core.drivers.wled import WledDriver

FIXTURES = Path(__file__).parent.parent / "fixtures" / "wled"


def _config(tmp_path: Path, *, wled: bool = True) -> Path:
    path = tmp_path / "config.toml"
    body = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
    if wled:
        body += '\n[wled.devices]\nportal = "https://portal.test"\n'
    path.write_text(body)
    path.chmod(0o600)
    return path


@pytest.fixture
def portal_transport(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    posts: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json=json.loads((FIXTURES / "state.json").read_text()))
        name = {
            "/json/info": "info.json",
            "/json/state": "state.json",
            "/json/eff": "effects.json",
            "/json/pal": "palettes.json",
        }
        return httpx.Response(200, json=json.loads((FIXTURES / name[request.url.path]).read_text()))

    monkeypatch.setattr(
        lighting_mod,
        "WledDriver",
        lambda settings: WledDriver(settings, transport=httpx.MockTransport(handle)),
    )
    return posts


def test_wled_status_renders_device(tmp_path: Path, portal_transport: list) -> None:
    result = CliRunner().invoke(app, ["wled", "status", "--config", str(_config(tmp_path))])
    assert result.exit_code == 0
    assert "Dig-Quad-AR" in result.output and "WLED 0.15.3" in result.output
    assert portal_transport == []  # status never writes


def test_wled_set_dry_run_writes_nothing(tmp_path: Path, portal_transport: list) -> None:
    result = CliRunner().invoke(
        app,
        ["wled", "set", "--color", "purple", "--dry-run", "--config", str(_config(tmp_path))],
    )
    assert result.exit_code == 0
    assert "Dry-run" in result.output
    assert portal_transport == []


def test_wled_set_and_revert_round_trip(tmp_path: Path, portal_transport: list) -> None:
    config = _config(tmp_path)
    result = CliRunner().invoke(app, ["wled", "set", "--color", "purple", "--config", str(config)])
    assert result.exit_code == 0 and "portal is now" in result.output
    assert len(portal_transport) == 1
    result = CliRunner().invoke(app, ["wled", "revert", "--config", str(config)])
    assert result.exit_code == 0 and "restored" in result.output
    # The revert POSTs the captured pre-change state verbatim (plus the echo flag).
    assert portal_transport[-1] == {
        **json.loads((FIXTURES / "state.json").read_text()),
        "v": True,
    }


def test_wled_unconfigured_exits_with_guidance(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["wled", "status", "--config", str(_config(tmp_path, wled=False))]
    )
    assert result.exit_code == 2
    assert "wled not configured" in result.output
