"""Unit (feature 135): strict parsing of the ``[maintenance]`` section.

A typo'd key or signal must fail at load (exit 2), never silently leave something alarming
or silently put nothing in maintenance (research D11).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, MaintenanceSettings, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_absent_section_is_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.maintenance == MaintenanceSettings()
    assert cfg.maintenance.is_empty


def test_live_declaration_round_trips_casefolded(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[maintenance]\n"
            'reason = " post-burn teardown 2026-10 "\n'
            'sites = ["Wingnut", "manzanita", " noc ", "magnolia", "manzanita"]\n'
            'uisp_sites = ["BRC - Business Park", "BRC  -  NOC"]\n'
            'signals = ["radio", "mesh", "lighting"]\n'
            'devices = ["Rowan-GW"]\n',
        )
    )
    m = cfg.maintenance
    assert m.sites == ("wingnut", "manzanita", "noc", "magnolia")  # deduped, trimmed, casefolded
    assert m.uisp_sites == ("brc - business park", "brc - noc")
    assert m.signals == ("radio", "mesh", "lighting")
    assert m.devices == ("rowan-gw",)
    assert m.reason == "post-burn teardown 2026-10"
    assert not m.is_empty


def test_reason_is_optional(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[maintenance]\nsites = ["magnolia"]\n'))
    assert cfg.maintenance.reason == ""


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('signals = ["radios"]', "unknown signal(s) radios (valid: radio, mesh, lighting)"),
        ('signals = ["fleet"]', "unknown signal(s) fleet"),
        ('sites = "magnolia"', "sites must be a list of strings"),
        ("sites = [1, 2]", "sites must be a list of strings"),
        ('devices = [""]', "devices must not contain empty names"),
        ('site = ["magnolia"]', "unknown key(s) site"),
        ("reason = 3", "reason must be a string"),
    ],
)
def test_invalid_declarations_fail_at_load(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(ConfigError, match=None) as err:
        load_config(_write(tmp_path, f"[maintenance]\n{body}\n"))
    assert fragment in str(err.value)


def test_non_table_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        path = _write(tmp_path, "")
        path.write_text('maintenance = "magnolia"\n' + path.read_text())  # top-level, not in [logs]
        load_config(path)
