"""Unit (feature 028): parsing [routeros.live] bounds and [watch].investigation_live_call_budget.

Zero-config defaults (the tools work with no extra config), overrides, clamping of nonsense values,
and the watcher budget default.
"""

from __future__ import annotations

from pathlib import Path

from obi.core.config import load_config

_BASE = """
[discovery]
seeds = ["172.25.110.1"]

[routeros]
username = "u"
password = "p"
use_tls  = false
"""


def _load(tmp_path: Path, extra: str = ""):
    path = tmp_path / "config.toml"
    path.write_text(_BASE + extra)
    path.chmod(0o600)
    return load_config(path)


def test_defaults_when_live_absent(tmp_path: Path) -> None:
    live = _load(tmp_path).routeros_live
    assert live.max_records == 100
    assert live.max_output_chars == 8000
    assert live.ping_count == 4
    assert live.ping_count_max == 20
    # Sized for a full internet traceroute (was 48 / 20.0 — too short for a public destination).
    assert live.traceroute_max_probes == 64
    assert live.traceroute_deadline_s == 45.0
    assert live.timeout_s is None


def test_overrides_are_parsed(tmp_path: Path) -> None:
    live = _load(
        tmp_path,
        """
[routeros.live]
max_records = 250
ping_count = 6
ping_count_max = 50
traceroute_max_probes = 30
traceroute_deadline_s = 12.5
timeout_s = 8
""",
    ).routeros_live
    assert live.max_records == 250
    assert live.ping_count == 6
    assert live.ping_count_max == 50
    assert live.traceroute_max_probes == 30
    assert live.traceroute_deadline_s == 12.5
    assert live.timeout_s == 8.0


def test_nonsense_values_are_clamped(tmp_path: Path) -> None:
    live = _load(
        tmp_path,
        """
[routeros.live]
max_records = 0
ping_count_max = -3
timeout_s = 0
""",
    ).routeros_live
    assert live.max_records == 1  # never 0 (would disable truncation)
    assert live.ping_count_max == 1
    assert live.timeout_s is None  # 0 ⇒ inherit the run-level timeout


def test_watch_budget_default_and_override(tmp_path: Path) -> None:
    assert _load(tmp_path).watch.investigation_live_call_budget == 8
    cfg = _load(tmp_path, "\n[watch]\ninvestigation_live_call_budget = 3\n")
    assert cfg.watch.investigation_live_call_budget == 3
