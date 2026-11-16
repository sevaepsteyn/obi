"""Unit (feature 087): the config capture-freshness pass inside ``obi watch run``.

Covers the fresh→stale finding, silent first-run baseline, silent recovery, stale-at-baseline
silence, mass coalescing, never-captured and non-roster skips, ignore_devices, dry-run, and the
opt-in / missing-sidecar resilience. Mirrors the 072 baseline-transition pass tests; the sidecar
itself is feature 085's ``config-captures.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from obi.core import configstore
from obi.core.config import (
    ConfigError,
    DiscoveryScope,
    WatchConfigCapturesSettings,
    _parse_watch_config_captures,
)
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.inventory import save as save_inventory
from obi.core.models import DiscoveryState
from obi.core.watch import _CONFIG_CAPTURES_BASELINE, _evaluate_config_captures
from tests.fakes import watch_config

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
FRESH_AT = (NOW - timedelta(hours=2)).isoformat()
STALE_AT = (NOW - timedelta(days=3)).isoformat()


def _cfg(tmp_path: Path, *, enabled: bool = True, mass_outage_ratio: float = 0.8, **kw):
    cfg = watch_config(tmp_path)
    cfg.watch.config_captures = WatchConfigCapturesSettings(
        enabled=enabled,
        mass_outage_ratio=mass_outage_ratio,
        repo_path=tmp_path / "config-repo",
        **kw,
    )
    return cfg


def _seed_sidecar(tmp_path: Path, entries: dict[str, dict]) -> None:
    configstore.record_capture_state(tmp_path / "config-repo", entries)


def _success(at: str, outcome: str = "captured_unchanged") -> dict:
    return {"last_success_at": at, "last_attempt_at": at, "last_outcome": outcome}


def _baseline(cfg, mapping: dict[str, bool]) -> None:
    path = cfg.watch.watch_dir / _CONFIG_CAPTURES_BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping))


def _read_baseline(cfg) -> dict:
    return json.loads((cfg.watch.watch_dir / _CONFIG_CAPTURES_BASELINE).read_text())


def test_fresh_to_stale_transition_alerts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_sidecar(
        tmp_path,
        {
            "chestnut-gw": _success(FRESH_AT),
            "walnut-gw": {**_success(STALE_AT), "last_outcome": "unreachable"},
        },
    )
    _baseline(cfg, {"chestnut-gw": True, "walnut-gw": True})
    findings = _evaluate_config_captures(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.device == "walnut-gw"
    assert f.source == "configs"
    assert "capture stale" in f.summary
    assert "unreachable" in f.description
    assert _read_baseline(cfg) == {"chestnut-gw": True, "walnut-gw": False}


def test_first_run_seeds_silently(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_sidecar(tmp_path, {"walnut-gw": _success(STALE_AT)})
    findings = _evaluate_config_captures(cfg, now=NOW)
    assert findings == []
    assert _read_baseline(cfg) == {"walnut-gw": False}


def test_recovery_is_silent_and_rearms(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_sidecar(tmp_path, {"walnut-gw": _success(FRESH_AT)})
    _baseline(cfg, {"walnut-gw": False})
    assert _evaluate_config_captures(cfg, now=NOW) == []
    assert _read_baseline(cfg) == {"walnut-gw": True}


def test_stale_at_baseline_stays_silent(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_sidecar(tmp_path, {"walnut-gw": _success(STALE_AT)})
    _baseline(cfg, {"walnut-gw": False})
    assert _evaluate_config_captures(cfg, now=NOW) == []


def test_mass_staleness_coalesces(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mass_outage_ratio=0.8)
    _seed_sidecar(
        tmp_path, {name: _success(STALE_AT) for name in ("a-gw", "b-gw", "c-gw")}
    )
    _baseline(cfg, {"a-gw": True, "b-gw": True, "c-gw": True})
    findings = _evaluate_config_captures(cfg, now=NOW)
    assert len(findings) == 1
    assert findings[0].device == "configs"
    assert "mass-stale: 3/3" in findings[0].summary


def test_never_captured_is_skipped(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_sidecar(
        tmp_path,
        {"ghost-gw": {"last_attempt_at": STALE_AT, "last_outcome": "auth_failed"}},
    )
    _baseline(cfg, {"other": True})  # non-empty: not a first run
    assert _evaluate_config_captures(cfg, now=NOW) == []
    assert "ghost-gw" not in _read_baseline(cfg)


def test_non_roster_identity_never_alerts(tmp_path: Path) -> None:
    # A renamed device's sidecar entry lingers; with a real inventory present only roster
    # identities are tracked.
    cfg = _cfg(tmp_path)
    save_inventory(
        Inventory(
            scope=DiscoveryScope(seeds=["172.25.110.1"]),
            entries=[
                InventoryEntry(
                    address="172.25.110.1",
                    identity="chestnut-gw",
                    discovery_state=DiscoveryState.IDENTIFIED,
                    present_in_latest=True,
                )
            ]
        ),
        cfg.inventory_path,
    )
    _seed_sidecar(
        tmp_path,
        {"chestnut-gw": _success(FRESH_AT), "sequoia-name-gw": _success(STALE_AT)},
    )
    _baseline(cfg, {"chestnut-gw": True, "sequoia-name-gw": True})
    assert _evaluate_config_captures(cfg, now=NOW) == []
    assert _read_baseline(cfg) == {"chestnut-gw": True}


def test_ignore_devices_substring(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ignore_devices=["walnut"])
    _seed_sidecar(tmp_path, {"walnut-gw": _success(STALE_AT)})
    _baseline(cfg, {"walnut-gw": True})
    assert _evaluate_config_captures(cfg, now=NOW) == []


def test_disabled_or_missing_sidecar_is_noop(tmp_path: Path) -> None:
    assert _evaluate_config_captures(_cfg(tmp_path, enabled=False), now=NOW) == []
    cfg = _cfg(tmp_path)  # enabled, but no sidecar file exists
    assert _evaluate_config_captures(cfg, now=NOW) == []
    assert not (cfg.watch.watch_dir / _CONFIG_CAPTURES_BASELINE).exists()


def test_dry_run_writes_no_baseline(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_sidecar(tmp_path, {"walnut-gw": _success(STALE_AT)})
    _baseline(cfg, {"walnut-gw": True})
    findings = _evaluate_config_captures(cfg, now=NOW, dry_run=True)
    assert len(findings) == 1
    assert _read_baseline(cfg) == {"walnut-gw": True}  # untouched


# --- [watch.config_captures] parsing -----------------------------------------------------------


def test_parse_defaults_and_absent() -> None:
    assert _parse_watch_config_captures(None) is None
    s = _parse_watch_config_captures({"enabled": True})
    assert s is not None and s.enabled
    assert s.stale_after == "1d" and s.mass_outage_ratio == 0.8 and s.severity == "warning"


@pytest.mark.parametrize(
    "data",
    [
        {"stale_after": "soon"},
        {"mass_outage_ratio": 0.0},
        {"severity": "loud"},
        {"ignore_devices": "walnut"},
    ],
)
def test_parse_rejects_bad_values(data: dict) -> None:
    with pytest.raises(ConfigError):
        _parse_watch_config_captures(data)


def test_135_maintenance_device_skipped(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = _cfg(tmp_path)
    cfg.maintenance = MaintenanceSettings(devices=("walnut-gw",))
    _seed_sidecar(tmp_path, {"walnut-gw": _success(STALE_AT)})
    _baseline(cfg, {"walnut-gw": True})
    assert _evaluate_config_captures(cfg, now=NOW) == []
