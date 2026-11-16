"""T030 (analyze F1/C2): both vendors render separately, and UISP is never persisted.

With [routeros]+[uisp] configured, `obi status` shows the unchanged Mikrotik report and a
separate UISP section (FR-007), and UISP devices are NOT written into the persisted roster
(FR-017) — `obi routeros discover` stays Mikrotik-only.
"""

from __future__ import annotations

from pathlib import Path

import librouteros
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.fakes import RecordingConnection, load_fixture
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()


@pytest.fixture
def stub_mikrotik(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = load_fixture("border-gw-01")
    records = {
        ("system", "identity"): [fixture["identity"]],
        ("system", "resource"): [fixture["resource"]],
        ("system", "routerboard"): [fixture["routerboard"]],
        ("ip", "neighbor"): fixture["neighbors"],
    }
    monkeypatch.setattr(librouteros, "connect", lambda **kwargs: RecordingConnection(records))


def test_both_vendors_render_and_uisp_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mikrotik: None
) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path, routeros=True)

    assert runner.invoke(app, ["routeros", "discover", "--scope", str(config)]).exit_code == 0
    roster_after_discover = (tmp_path / "inventory.json").read_text()

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "border-gw-01" in result.output  # Mikrotik report, unchanged (FR-007)
    assert "ptp-ridge-east" in result.output  # separate UISP section

    # FR-017: UISP devices are never written into the persisted Mikrotik roster.
    roster_after_status = (tmp_path / "inventory.json").read_text()
    assert "ptp-ridge-east" not in roster_after_status
    assert "uisp" not in roster_after_status
    assert roster_after_status == roster_after_discover  # status does not mutate the roster
