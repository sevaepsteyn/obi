"""Integration (feature 026): `obi configs ingest/status/show/history/diff` end-to-end through the
Typer CLI against a tmp git repo + a stubbed SSH driver; covers --json shapes and not-configured."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.config import DiscoveryScope
from obi.core.drivers import routeros_config as rc
from obi.core.drivers.routeros_config import ExportResult
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.inventory import save as save_inventory
from obi.core.models import CaptureOutcome, DiscoveryState

runner = CliRunner()

_EXPORT_V1 = "# 2026-07-05 12:00:00 by RouterOS 7.15\n/ip address\nadd address=172.25.110.1/24\n"
_EXPORT_V2 = "# 2026-07-07 09:00:00 by RouterOS 7.16\n/ip address\nadd address=172.25.110.2/24\n"


def _write_config(tmp_path: Path) -> Path:
    repo = tmp_path / "config-repo"
    inv = tmp_path / "inventory.json"
    audit = tmp_path / "audit.jsonl"
    save_inventory(
        Inventory(
            scope=DiscoveryScope(seeds=["172.25.110.1"]),
            entries=[
                InventoryEntry(
                    address="172.25.110.1",
                    identity="core-gw-1",
                    discovery_state=DiscoveryState.IDENTIFIED,
                    present_in_latest=True,
                )
            ],
        ),
        inv,
    )
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[discovery]
ranges = ["172.25.110.0/24"]
seeds = ["172.25.110.1"]

[routeros]
username = "admin"
password = "super-secret-pw"
use_tls = false

[configs]
repo_path = "{repo}"

[inventory]
path = "{inv}"

[audit]
path = "{audit}"
"""
    )
    cfg.chmod(0o600)
    return cfg


class _StubDriver:
    exports: dict[str, str] = {}

    def __init__(self, settings: object, *, exec_fn: object = None) -> None:
        self._settings = settings

    def fetch_export(self, address: str, *, timeout: float) -> ExportResult:
        content = _StubDriver.exports.get(address)
        if content is None:
            return ExportResult(address, CaptureOutcome.UNREACHABLE, note="host down")
        return ExportResult(address, CaptureOutcome.CAPTURED_CHANGED, content=content)


@pytest.fixture
def _stub_driver(monkeypatch: pytest.MonkeyPatch) -> type[_StubDriver]:
    _StubDriver.exports = {"172.25.110.1": _EXPORT_V1}
    monkeypatch.setattr(rc, "RouterosConfigDriver", _StubDriver)
    return _StubDriver


def test_full_lifecycle(tmp_path: Path, _stub_driver: type[_StubDriver]) -> None:
    cfg = _write_config(tmp_path)

    # ingest → one change
    ing = runner.invoke(app, ["configs", "ingest", "--scope", str(cfg), "--json"])
    assert ing.exit_code == 0, ing.output
    payload = json.loads(ing.stdout)
    assert payload["changed"] == 1 and payload["failed"] == 0

    # re-ingest with the same content → no change
    ing2 = runner.invoke(app, ["configs", "ingest", "--scope", str(cfg), "--json"])
    assert json.loads(ing2.stdout)["changed"] == 0

    # a real change → one new version
    _StubDriver.exports = {"172.25.110.1": _EXPORT_V2}
    runner.invoke(app, ["configs", "ingest", "--scope", str(cfg)])

    # status
    st = runner.invoke(app, ["configs", "status", "--scope", str(cfg), "--json"])
    assert st.exit_code == 0
    status = json.loads(st.stdout)
    assert status["totals"]["backed_up"] == 1

    # show (latest is V2, redacted header stripped)
    show = runner.invoke(app, ["configs", "show", "core-gw-1", "--scope", str(cfg), "--json"])
    assert show.exit_code == 0
    shown = json.loads(show.stdout)
    assert shown["found"] and "172.25.110.2/24" in shown["content"]
    assert "by RouterOS" not in shown["content"]  # volatile header normalized away

    # history has two change points
    hist = runner.invoke(app, ["configs", "history", "core-gw-1", "--scope", str(cfg), "--json"])
    assert len(json.loads(hist.stdout)["entries"]) == 2

    # diff shows the address change
    diff = runner.invoke(app, ["configs", "diff", "core-gw-1", "--scope", str(cfg), "--json"])
    d = json.loads(diff.stdout)
    assert d["changed"] and "172.25.110.2/24" in d["diff"]


def test_show_unknown_device_exits_2(tmp_path: Path, _stub_driver: type[_StubDriver]) -> None:
    cfg = _write_config(tmp_path)
    runner.invoke(app, ["configs", "ingest", "--scope", str(cfg)])
    result = runner.invoke(app, ["configs", "show", "ghost", "--scope", str(cfg)])
    assert result.exit_code == 2


def test_not_configured_exits_2(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[discovery]
ranges = ["172.25.110.0/24"]

[routeros]
username = "u"
password = "p"
"""
    )
    cfg.chmod(0o600)
    for cmd in (["configs", "status"], ["configs", "ingest"], ["configs", "show", "x"]):
        result = runner.invoke(app, [*cmd, "--scope", str(cfg)])
        assert result.exit_code == 2, cmd
