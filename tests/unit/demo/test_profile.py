"""Unit tests for the demo profile (feature 134, T004)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from obi.core.demo import ProfileError
from obi.core.demo.profile import load_profile


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    deny = cfg_dir / "demo-denylist.txt"
    deny.write_text("fictional term\n")
    deny.chmod(0o600)
    path = cfg_dir / "config.toml"
    path.write_text("")
    return path


def load(config_path: Path, demo: dict[str, Any], tmp_path: Path) -> Any:
    demo = {"output_dir": str(tmp_path / "out"), **demo}
    return load_profile({"demo": demo}, config_path=config_path)


def test_defaults(config_path: Path, tmp_path: Path) -> None:
    p = load(config_path, {}, tmp_path)
    assert p.window_hours == 48 and p.metrics_decimate == 6 and p.max_dataset_mb == 200
    assert p.audit_command_sample == 10
    assert (p.destination_lat, p.destination_lon) == (37.16, -118.27)
    assert p.epoch_start.isoformat() == "2025-06-02"
    assert p.mapping_path.name == "mapping.json"
    assert p.denylist_path == config_path.parent / "demo-denylist.txt"


def test_non_monday_epoch_rejected(config_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load(config_path, {"epoch_start": "2025-06-03"}, tmp_path)


def test_output_inside_state_or_config_rejected(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(ProfileError):
        load(config_path, {"output_dir": str(tmp_path / "state" / "obi" / "x")}, tmp_path)
    with pytest.raises(ProfileError):
        load(config_path, {"output_dir": str(config_path.parent / "x")}, tmp_path)


def test_private_files_inside_output_rejected(config_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load(config_path, {"mapping_path": str(tmp_path / "out" / "m.json")}, tmp_path)


def test_denylist_missing_or_group_readable_rejected(config_path: Path, tmp_path: Path) -> None:
    deny = config_path.parent / "demo-denylist.txt"
    deny.chmod(0o644)
    with pytest.raises(ProfileError):
        load(config_path, {}, tmp_path)
    deny.unlink()
    with pytest.raises(ProfileError):
        load(config_path, {}, tmp_path)


def test_window_validation(config_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load(config_path, {"since": "2026-08-01T00:00", "until": "2026-08-20T00:00"}, tmp_path)
    with pytest.raises(ProfileError):
        load(config_path, {"since": "2026-08-01T00:00"}, tmp_path)
    p = load(config_path, {"since": "2026-08-01T00:00", "until": "2026-08-03T00:00"}, tmp_path)
    assert p.since is not None and p.since.tzinfo is not None


def test_destination_outside_conus_rejected(config_path: Path, tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load(config_path, {"destination_lat": 51.5, "destination_lon": -0.1}, tmp_path)
