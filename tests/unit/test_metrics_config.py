"""Unit (feature 021): parsing the opt-in ``[snmp.history]`` subsection.

Asserts the collector is inactive without the subsection (back-compat with feature 012), the
defaults, the ``retain`` duration parsing, and that an unknown profile fails fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, SnmpHistorySettings, load_config

_BASE = """
[discovery]
seeds  = ["172.25.110.1"]
ranges = ["172.25.0.0/16"]

[routeros]
username = "u"
password = "p"
use_tls  = false
"""

_SNMP = '[snmp]\ncommunity = "public"\n'


def _write(tmp_path: Path, snmp_block: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_BASE + snmp_block)
    path.chmod(0o600)
    return path


def _hist(tmp_path: Path, history_block: str) -> SnmpHistorySettings:
    cfg = load_config(_write(tmp_path, _SNMP + history_block))
    assert cfg.snmp is not None
    assert cfg.snmp.history is not None
    return cfg.snmp.history


def test_history_absent_leaves_collector_inactive(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _SNMP))
    assert cfg.snmp is not None
    assert cfg.snmp.history is None  # feature 012 config unchanged; collector off


def test_history_defaults(tmp_path: Path) -> None:
    hist = _hist(tmp_path, "[snmp.history]\n")
    assert hist.targets == ""  # 137: no built-in naming convention — every identified device
    assert hist.profiles == ["interfaces", "system"]
    assert hist.retain_days == 90
    assert hist.query_buckets == 60
    assert hist.db_path.name == "metrics.db"


def test_history_retain_duration_and_int(tmp_path: Path) -> None:
    assert _hist(tmp_path, '[snmp.history]\nretain = "12w"\n').retain_days == 84
    assert _hist(tmp_path, "[snmp.history]\nretain = 30\n").retain_days == 30


def test_history_custom_targets_and_profiles(tmp_path: Path) -> None:
    block = '[snmp.history]\ntargets="core"\nprofiles=["system"]\nquery_buckets=120\n'
    hist = _hist(tmp_path, block)
    assert hist.targets == "core"
    assert hist.profiles == ["system"]
    assert hist.query_buckets == 120


def test_history_unknown_profile_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _hist(tmp_path, '[snmp.history]\nprofiles=["bogus"]\n')


def test_history_bad_retain_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _hist(tmp_path, '[snmp.history]\nretain = "soon"\n')


# --- 096: the health/optics profiles join the registry ----------------------------------------


def test_health_and_optics_profiles_are_registered() -> None:
    from obi.core.drivers.snmp_profiles import PROFILES, get_profile

    assert "health" in PROFILES and "optics" in PROFILES
    assert get_profile("health").name == "health"
    assert get_profile("optics").name == "optics"


def test_history_accepts_health_and_optics_profiles(tmp_path: Path) -> None:
    block = '[snmp.history]\nprofiles=["interfaces", "system", "health", "optics"]\n'
    hist = _hist(tmp_path, block)
    assert hist.profiles == ["interfaces", "system", "health", "optics"]


def test_interfaces_profile_and_targets_semantics_unchanged(tmp_path: Path) -> None:
    """FR-005's coverage half: adding health profiles changes neither the interfaces profile
    definition nor the single shared targets filter (default "", every device)."""

    from obi.core.drivers.snmp_profiles import get_profile

    interfaces = get_profile("interfaces")
    assert [c.name for c in interfaces.table] == [
        "ifName",
        "ifOperStatus",
        "ifHCInOctets",
        "ifHCOutOctets",
        "ifInErrors",
        "ifOutErrors",
        "ifInDiscards",
        "ifOutDiscards",
    ]
    hist = _hist(tmp_path, '[snmp.history]\nprofiles=["interfaces", "health", "optics"]\n')
    assert hist.targets == ""  # one shared filter; health rides it, never widens it
