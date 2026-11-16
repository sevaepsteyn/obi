"""Unit (feature 072): the ICMP reachability pass inside ``obi watch run``.

Covers the up→down finding, the silent first-run baseline, silent recovery, down-at-baseline
silence, mass-outage coalescing, dry-run, and the opt-in / missing-store resilience — plus the
``obi icmp status`` render. Mirrors the 034/030 baseline-transition pass tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from obi.core import icmpstore
from obi.core.config import WatchIcmpSettings
from obi.core.icmpstore import DeviceState
from obi.core.watch import _ICMP_BASELINE, _evaluate_icmp
from tests.fakes import watch_config

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
NOW_US = int(NOW.timestamp() * 1_000_000)


def _cfg(tmp_path: Path, *, enabled: bool = True, mass_outage_ratio: float = 0.8):
    cfg = watch_config(tmp_path)
    cfg.watch.icmp = WatchIcmpSettings(
        enabled=enabled, mass_outage_ratio=mass_outage_ratio, db_path=tmp_path / "icmp.db"
    )
    return cfg


def _seed_store(tmp_path: Path, states: list[DeviceState]) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    icmpstore.upsert_state(conn, states)
    conn.commit()
    conn.close()


def _state(device: str, *, up: bool, since_us: int = NOW_US) -> DeviceState:
    return DeviceState(
        device=device,
        address=f"172.25.110.{len(device)}",
        up=up,
        consecutive_misses=0 if up else 1,
        last_seen_us=since_us if up else since_us - 600_000_000,
        last_change_us=since_us,
        updated_us=NOW_US,
    )


def _baseline(cfg, mapping: dict[str, bool]) -> None:
    path = cfg.watch.watch_dir / _ICMP_BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping))


def _read_baseline(cfg) -> dict:
    return json.loads((cfg.watch.watch_dir / _ICMP_BASELINE).read_text())


# --- T008 (US1): down detection ---------------------------------------------------------------


def test_up_to_down_transition_alerts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_store(tmp_path, [_state("chestnut-sw", up=False), _state("core-gw-1", up=True)])
    _baseline(cfg, {"chestnut-sw": True, "core-gw-1": True})
    findings = _evaluate_icmp(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.source == "icmp"
    assert f.severity == "warning"
    assert f.device == "chestnut-sw"
    assert f.dedup_extra == "chestnut-sw:down"
    assert "unreachable by ICMP" in f.summary
    assert f.evidence and f.evidence[0].kind == "probe"


def test_disabled_returns_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enabled=False)
    _seed_store(tmp_path, [_state("chestnut-sw", up=False)])
    _baseline(cfg, {"chestnut-sw": True})
    assert _evaluate_icmp(cfg, now=NOW) == []


def test_no_watch_icmp_section_inactive(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    assert cfg.watch.icmp is None
    assert _evaluate_icmp(cfg, now=NOW) == []


def test_missing_store_returns_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # store never created
    assert _evaluate_icmp(cfg, now=NOW) == []


def test_severity_is_configurable(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.watch.icmp.severity = "critical"
    _seed_store(tmp_path, [_state("chestnut-sw", up=False)])
    _baseline(cfg, {"chestnut-sw": True})
    assert _evaluate_icmp(cfg, now=NOW)[0].severity == "critical"


# --- T010 (US2): silent baseline, recovery, coalescing ----------------------------------------


def test_first_run_seeds_baseline_silently(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_store(
        tmp_path,
        [_state("up-gw", up=True), _state("down-gw", up=False), _state("brc-gw", up=False)],
    )
    # no baseline file yet → first run
    findings = _evaluate_icmp(cfg, now=NOW)
    assert findings == []  # zero alerts on first sight
    baseline = _read_baseline(cfg)
    assert baseline == {"up-gw": True, "down-gw": False, "brc-gw": False}


def test_down_since_baseline_never_alerts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_store(tmp_path, [_state("brc-gw", up=False)])
    _baseline(cfg, {"brc-gw": False})  # already down at baseline (off-season)
    assert _evaluate_icmp(cfg, now=NOW) == []


def test_recovery_is_silent_clear(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_store(tmp_path, [_state("chestnut-sw", up=True)])
    _baseline(cfg, {"chestnut-sw": False})  # was down, now up
    findings = _evaluate_icmp(cfg, now=NOW)
    assert findings == []  # no "recovered" message
    assert _read_baseline(cfg) == {"chestnut-sw": True}  # baseline re-armed


def test_recovered_device_can_alert_again(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # baseline says up (post-recovery), store now down → a fresh transition alerts
    _seed_store(tmp_path, [_state("chestnut-sw", up=False)])
    _baseline(cfg, {"chestnut-sw": True})
    assert len(_evaluate_icmp(cfg, now=NOW)) == 1


def test_mass_outage_coalesces(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mass_outage_ratio=0.8)
    # 4 of 5 transition down at once → 0.8 ≥ 0.8 → one coalesced finding
    _seed_store(
        tmp_path,
        [
            _state("a", up=False),
            _state("b", up=False),
            _state("c", up=False),
            _state("d", up=False),
            _state("e", up=True),
        ],
    )
    _baseline(cfg, {"a": True, "b": True, "c": True, "d": True, "e": True})
    findings = _evaluate_icmp(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.device == "reachability"
    assert f.dedup_extra == "icmp-mass-down"
    assert "4/5 devices" in f.summary
    assert "a, b, c, d" in f.description


def test_below_ratio_is_per_device(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mass_outage_ratio=0.8)
    # 2 of 5 down → 0.4 < 0.8 → per-device findings
    _seed_store(
        tmp_path,
        [
            _state("a", up=False),
            _state("b", up=False),
            _state("c", up=True),
            _state("d", up=True),
            _state("e", up=True),
        ],
    )
    _baseline(cfg, {"a": True, "b": True, "c": True, "d": True, "e": True})
    findings = _evaluate_icmp(cfg, now=NOW)
    assert {f.device for f in findings} == {"a", "b"}
    assert all(f.dedup_extra and f.dedup_extra.endswith(":down") for f in findings)


def test_dry_run_writes_no_baseline(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _seed_store(tmp_path, [_state("chestnut-sw", up=False)])
    _baseline(cfg, {"chestnut-sw": True})
    findings = _evaluate_icmp(cfg, now=NOW, dry_run=True)
    assert len(findings) == 1  # still evaluates
    assert _read_baseline(cfg) == {"chestnut-sw": True}  # but baseline unchanged


def test_new_device_seeds_without_alert(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # baseline exists (not first run) but 'pine-sw' is unseen; it is down now
    _seed_store(tmp_path, [_state("core-gw-1", up=True), _state("pine-sw", up=False)])
    _baseline(cfg, {"core-gw-1": True})
    findings = _evaluate_icmp(cfg, now=NOW)
    assert findings == []  # a device not in the baseline never alerts on first sight
    assert _read_baseline(cfg)["pine-sw"] is False


# --- T012 (US3): status render ----------------------------------------------------------------


# --- feature 095 (FR-013): the dash pager supersedes this pass, baseline stays fresh ----------


def _run_scoped(cfg):
    from obi.core import logstore
    from obi.core.audit import AuditTrail
    from obi.core.watch import run_cycle
    from obi.core.watchstore import WatchStore
    from tests.fakes import FakeNotifier

    logstore.connect(cfg.logs.db_path).close()  # empty logs store → the cycle is not degraded
    return run_cycle(
        cfg,
        WatchStore(cfg.watch.watch_dir),
        audit=AuditTrail(cfg.audit_path),
        now=NOW,
        llm_client=None,
        notifier=FakeNotifier(),
    )


def _with_dash_pager(cfg, tmp_path: Path, *, enabled: bool):
    from obi.core.config import DashPagerSettings, DashSettings

    cfg.dash = DashSettings(
        state_dir=tmp_path / "dash", pager=DashPagerSettings(enabled=enabled)
    )
    return cfg


def _icmp_findings(cfg) -> list:
    from obi.core.watchstore import WatchStore

    return [f for f in WatchStore(cfg.watch.watch_dir).list_findings() if f.source == "icmp"]


def test_dash_pager_supersedes_the_icmp_pass_but_reseeds_the_baseline(tmp_path: Path) -> None:
    cfg = _with_dash_pager(_cfg(tmp_path), tmp_path, enabled=True)
    _seed_store(tmp_path, [_state("chestnut-sw", up=False), _state("core-gw-1", up=True)])
    _baseline(cfg, {"chestnut-sw": True, "core-gw-1": True})
    run = _run_scoped(cfg)
    assert run.finding_ids == [] and _icmp_findings(cfg) == []  # dash already paged this
    assert "icmp pass superseded by dash pager" in run.conditions
    # The load-bearing half (analysis I1): the baseline advanced anyway, so disabling the dash
    # pager later resumes 072 with no stale-baseline alert burst.
    assert _read_baseline(cfg) == {"chestnut-sw": False, "core-gw-1": True}


def test_dash_pager_disabled_leaves_the_icmp_pass_alone(tmp_path: Path) -> None:
    cfg = _with_dash_pager(_cfg(tmp_path), tmp_path, enabled=False)
    _seed_store(tmp_path, [_state("chestnut-sw", up=False), _state("core-gw-1", up=True)])
    _baseline(cfg, {"chestnut-sw": True, "core-gw-1": True})
    run = _run_scoped(cfg)
    assert [f.device for f in _icmp_findings(cfg)] == ["chestnut-sw"]
    assert "icmp pass superseded by dash pager" not in run.conditions


def test_render_icmp_status_lists_devices() -> None:
    from obi.cli.render import _render_icmp_status

    console = Console(record=True, width=100)
    states = [_state("core-gw-1", up=True), _state("chestnut-sw", up=False)]
    _render_icmp_status(states, NOW_US, console)
    text = console.export_text()
    assert "core-gw-1" in text and "chestnut-sw" in text
    assert "up" in text and "down" in text
    assert "2 device(s): 1 up, 1 down." in text


def test_render_icmp_status_empty() -> None:
    from obi.cli.render import _render_icmp_status

    console = Console(record=True, width=100)
    _render_icmp_status([], NOW_US, console)
    assert "No devices in the ICMP store" in console.export_text()
