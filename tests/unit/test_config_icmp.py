"""Unit (feature 072): parsing the ``[icmp]`` collector section and ``[watch.icmp]`` pass section.

Asserts both are inactive/None when absent, the defaults, custom values, and that out-of-range
bounds fail fast at load time rather than mid-cron.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import (
    ConfigError,
    IcmpSettings,
    WatchIcmpSettings,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


# --- [icmp] collector section -----------------------------------------------------------------


def test_absent_icmp_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.icmp is None  # obi icmp run still works with an all-default IcmpSettings


def test_icmp_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[icmp]\n"))
    icmp = cfg.icmp
    assert icmp is not None
    assert icmp.pings_per_cycle == 3
    assert icmp.ping_timeout_ms == 1000
    assert icmp.deadline_s == 5.0
    assert icmp.concurrency == 16
    assert icmp.down_after_cycles == 1
    assert icmp.include_siklu is True
    assert icmp.ignore_devices == []
    assert icmp.db_path.name == "icmp.db"


def test_icmp_custom_values(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[icmp]\npings_per_cycle = 5\nping_timeout_ms = 500\ndeadline_s = 3.0\n"
            'concurrency = 8\ndown_after_cycles = 2\ninclude_siklu = false\n'
            'ignore_devices = ["brc-", "ponderosa-sw"]\n',
        )
    )
    icmp = cfg.icmp
    assert icmp is not None
    assert icmp.pings_per_cycle == 5
    assert icmp.ping_timeout_ms == 500
    assert icmp.deadline_s == 3.0
    assert icmp.concurrency == 8
    assert icmp.down_after_cycles == 2
    assert icmp.include_siklu is False
    assert icmp.ignore_devices == ["brc-", "ponderosa-sw"]


def test_icmp_extra_hosts(tmp_path: Path) -> None:
    hosts = '["core-vms-1.net.example.net", "chestnut-vms-1.net.example.net"]'
    cfg = load_config(_write(tmp_path, f"[icmp]\nextra_hosts = {hosts}\n"))
    assert cfg.icmp is not None
    assert cfg.icmp.extra_hosts == [
        "core-vms-1.net.example.net",
        "chestnut-vms-1.net.example.net",
    ]


def test_icmp_extra_hosts_default_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[icmp]\n"))
    assert cfg.icmp is not None
    assert cfg.icmp.extra_hosts == []


def test_icmp_down_after_cycles_below_one_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[icmp]\ndown_after_cycles = 0\n"))


def test_icmp_pings_per_cycle_below_one_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[icmp]\npings_per_cycle = 0\n"))


def test_icmp_deadline_not_positive_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[icmp]\ndeadline_s = 0\n"))


# --- [watch.icmp] pass section ----------------------------------------------------------------


def test_absent_watch_icmp_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[watch]\nenabled = true\n"))
    assert cfg.watch.icmp is None  # other passes unchanged


def test_watch_icmp_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[watch]\nenabled = true\n[watch.icmp]\n"))
    w = cfg.watch.icmp
    assert w is not None
    assert w.enabled is False  # presence alone does not activate
    assert w.mass_outage_ratio == 0.80
    assert w.severity == "warning"
    assert w.db_path.name == "icmp.db"


def test_watch_icmp_enabled_and_custom(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[watch]\nenabled = true\n[watch.icmp]\nenabled = true\n"
            "mass_outage_ratio = 0.5\nseverity = \"critical\"\n",
        )
    )
    w = cfg.watch.icmp
    assert w is not None
    assert w.enabled is True
    assert w.mass_outage_ratio == 0.5
    assert w.severity == "critical"


def test_watch_icmp_bad_ratio_raises(tmp_path: Path) -> None:
    block = "[watch]\nenabled = true\n[watch.icmp]\nmass_outage_ratio = 0\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, block))


def test_watch_icmp_bad_severity_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, "[watch]\nenabled = true\n[watch.icmp]\nseverity = \"loud\"\n")
        )


def test_defaults_constructible_without_toml() -> None:
    # The dataclasses stand alone (used by `obi icmp run` when [icmp] is absent).
    assert IcmpSettings().pings_per_cycle == 3
    assert WatchIcmpSettings().enabled is False
