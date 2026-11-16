"""Unit (feature 034): the UISP wireless-link pass inside `obi watch run`.

Covers the deterministic rules (down = active→non-active; drop = signal below baseline by more
than the threshold), the baseline lifecycle (first-run seed, retention while degraded, recovery
re-arm, disappearance, new links, corruption ⇒ first-run), the ignore list, dry-run, the
opt-in/back-compat property, and the run_cycle merge (dedup, Slack reuse, failure isolation).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from obi.core import logstore
from obi.core.audit import AuditTrail
from obi.core.config import Config, WatchUispLinksSettings
from obi.core.models import (
    SourceOutcome,
    UispLinkEnd,
    UispLinksReport,
    UispWirelessLink,
)
from obi.core.watch import _UISP_LINKS_BASELINE, _evaluate_uisp_links, run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import FakeNotifier, uisp_settings, watch_config

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def _link(
    link_id: str = "L1",
    a: str = "chestnut-gw",
    b: str = "chestnut-ptp-60",
    state: str | None = "active",
    signal: float | None = -62.0,
) -> UispWirelessLink:
    return UispWirelessLink(
        id=link_id,
        end_a=UispLinkEnd(name=a),
        end_b=UispLinkEnd(name=b),
        state=state,
        signal_dbm=signal,
        ssid="BM-Chestnut-airMAX",
        frequency_mhz=5550.0,
        distance_m=2822.0,
    )


def _report(links: list[UispWirelessLink], outcome: SourceOutcome = SourceOutcome.REACHABLE):
    return UispLinksReport(
        controller="https://uisp.example.com",
        outcome=outcome,
        links=links,
        started_at=NOW,
        finished_at=NOW,
        detail="controller went away" if outcome is not SourceOutcome.REACHABLE else None,
    )


def _cfg(tmp_path: Path, *, enabled: bool = True, **kw: Any) -> Config:
    cfg = watch_config(tmp_path)
    cfg.uisp = uisp_settings()
    cfg.watch.uisp_links = WatchUispLinksSettings(enabled=enabled, **kw)
    return cfg


def _serve(monkeypatch: pytest.MonkeyPatch, links: list[UispWirelessLink], **kw: Any) -> None:
    monkeypatch.setattr(
        "obi.core.engine.uisp_links", lambda *a, **k: _report(links, **kw)
    )


def _evaluate(cfg: Config, *, now: datetime = NOW, dry_run: bool = False):
    return _evaluate_uisp_links(cfg, AuditTrail(cfg.audit_path), now=now, dry_run=dry_run)


def _baseline_path(cfg: Config) -> Path:
    return cfg.watch.watch_dir / _UISP_LINKS_BASELINE


def _baseline(cfg: Config) -> dict[str, Any]:
    return json.loads(_baseline_path(cfg).read_text())


# --- rules -------------------------------------------------------------------------------------


def test_first_run_seeds_baseline_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link()])

    assert _evaluate(cfg) == []
    assert _baseline(cfg)["L1"] == {
        "label": "chestnut-gw ↔ chestnut-ptp-60",
        "state": "active",
        "signal_dbm": -62.0,
    }


def test_active_to_disconnected_raises_down_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link()])
    _evaluate(cfg)  # seed
    _serve(monkeypatch, [_link(state="disconnected", signal=None)])

    findings = _evaluate(cfg, now=NOW + timedelta(hours=1))

    assert len(findings) == 1
    f = findings[0]
    assert f.source == "uisp-links"
    assert f.severity == "warning"
    assert f.device == "chestnut-gw ↔ chestnut-ptp-60"
    assert f.dedup_extra == "L1:down"
    assert "link down" in f.summary
    assert "was active" in f.summary and "disconnected" in f.summary
    # a missing signal on the downed link is part of the down state, never a signal-drop finding
    assert all(x.dedup_extra != "L1:signal" for x in findings)


def test_signal_drop_beyond_threshold_raises_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, signal_drop_db=6.0)
    _serve(monkeypatch, [_link(signal=-62.0)])
    _evaluate(cfg)  # seed
    _serve(monkeypatch, [_link(signal=-71.0)])

    findings = _evaluate(cfg, now=NOW + timedelta(hours=1))

    assert len(findings) == 1
    f = findings[0]
    assert f.dedup_extra == "L1:signal"
    assert "fell 9.0 dB" in f.summary
    assert "-62" in f.summary and "-71" in f.summary
    assert f.evidence and "−9.0 dB" in f.evidence[0].text


def test_drop_at_or_below_threshold_is_quiet_and_rebases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, signal_drop_db=6.0)
    _serve(monkeypatch, [_link(signal=-62.0)])
    _evaluate(cfg)  # seed
    _serve(monkeypatch, [_link(signal=-66.0)])  # 4 dB — under the threshold

    assert _evaluate(cfg, now=NOW + timedelta(hours=1)) == []
    assert _baseline(cfg)["L1"]["signal_dbm"] == -66.0  # baseline tracks when not degraded


def test_baseline_retained_while_degraded_and_rearms_on_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, signal_drop_db=6.0)
    _serve(monkeypatch, [_link(signal=-62.0)])
    _evaluate(cfg)  # seed

    _serve(monkeypatch, [_link(signal=-71.0)])
    assert len(_evaluate(cfg, now=NOW + timedelta(hours=1))) == 1
    assert _baseline(cfg)["L1"]["signal_dbm"] == -62.0  # held, not re-based to -71

    # still degraded next cycle: the rule keeps firing (run_cycle's dedup holds it to one alert)
    assert len(_evaluate(cfg, now=NOW + timedelta(hours=2))) == 1
    assert _baseline(cfg)["L1"]["signal_dbm"] == -62.0

    # recovery: quiet, and the baseline resumes tracking
    _serve(monkeypatch, [_link(signal=-63.0)])
    assert _evaluate(cfg, now=NOW + timedelta(hours=3)) == []
    assert _baseline(cfg)["L1"]["signal_dbm"] == -63.0

    # a fresh drop after recovery alerts again
    _serve(monkeypatch, [_link(signal=-72.0)])
    assert len(_evaluate(cfg, now=NOW + timedelta(hours=4))) == 1


def test_missing_current_signal_on_active_link_is_never_a_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link(signal=-62.0)])
    _evaluate(cfg)  # seed
    _serve(monkeypatch, [_link(signal=None)])  # active but signal absent

    assert _evaluate(cfg, now=NOW + timedelta(hours=1)) == []


def test_baseline_without_signal_seeds_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link(signal=None)])
    _evaluate(cfg)  # seed: no signal in the baseline
    _serve(monkeypatch, [_link(signal=-90.0)])  # a signal appears — no baseline to compare

    assert _evaluate(cfg, now=NOW + timedelta(hours=1)) == []
    assert _baseline(cfg)["L1"]["signal_dbm"] == -90.0


def test_disappeared_link_leaves_baseline_without_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link(), _link("L2", "noc-a", "noc-b")])
    _evaluate(cfg)  # seed both
    _serve(monkeypatch, [_link()])  # L2 decommissioned

    assert _evaluate(cfg, now=NOW + timedelta(hours=1)) == []
    assert set(_baseline(cfg)) == {"L1"}


def test_new_link_seeds_silently_midlife(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link()])
    _evaluate(cfg)  # seed
    _serve(monkeypatch, [_link(), _link("L2", "noc-a", "noc-b", state="disconnected")])

    # the brand-new (already down) link has no baseline: no finding, but it joins the baseline
    assert _evaluate(cfg, now=NOW + timedelta(hours=1)) == []
    assert set(_baseline(cfg)) == {"L1", "L2"}


def test_ignore_links_matches_either_endpoint_or_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, ignore_links=["lab-"])
    _serve(monkeypatch, [_link(), _link("L2", "lab-ap", "yard-st")])
    _evaluate(cfg)  # seed
    assert set(_baseline(cfg)) == {"L1"}  # ignored links are not even baselined

    _serve(
        monkeypatch,
        [_link(state="disconnected"), _link("L2", "lab-ap", "yard-st", state="disconnected")],
    )
    findings = _evaluate(cfg, now=NOW + timedelta(hours=1))

    assert [f.dedup_extra for f in findings] == ["L1:down"]  # the lab link stays quiet


def test_uisp_ignore_names_forwarded_to_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """feature 035: the pass passes [uisp] ignore_names to engine.uisp_links (which excludes there),
    in union with the pass-local [watch.uisp_links] ignore_links applied on top."""
    cfg = _cfg(tmp_path, ignore_links=["lab-"])
    cfg.uisp = uisp_settings(ignore_names=["aspen"])

    captured: dict[str, Any] = {}

    def _capture(*_a: Any, **kw: Any) -> UispLinksReport:
        captured.update(kw)
        return _report([_link()])

    monkeypatch.setattr("obi.core.engine.uisp_links", _capture)
    _evaluate(cfg)

    assert list(captured.get("ignore_names", [])) == ["aspen"]  # [uisp] knob reaches the engine
    assert captured.get("name_filter") == cfg.uisp.name_filter  # include still forwarded


def test_corrupt_baseline_file_means_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _baseline_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    _baseline_path(cfg).write_text("{not json")
    _serve(monkeypatch, [_link(state="disconnected")])

    assert _evaluate(cfg) == []  # seed, no crash, no finding
    assert _baseline(cfg)["L1"]["state"] == "disconnected"


def test_unreachable_controller_raises_and_preserves_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link()])
    _evaluate(cfg)  # seed
    before = _baseline(cfg)
    _serve(monkeypatch, [], outcome=SourceOutcome.UNREACHABLE)

    with pytest.raises(RuntimeError, match="unreachable"):
        _evaluate(cfg, now=NOW + timedelta(hours=1))
    assert _baseline(cfg) == before  # untouched — no false "disappeared" pruning


def test_dry_run_evaluates_but_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link(signal=-62.0)])
    _evaluate(cfg)  # seed (a real run)
    before = _baseline(cfg)
    _serve(monkeypatch, [_link(state="disconnected", signal=None)])

    findings = _evaluate(cfg, now=NOW + timedelta(hours=1), dry_run=True)

    assert len(findings) == 1  # detection still works
    assert _baseline(cfg) == before  # but the baseline file is unwritten


def test_dry_run_first_ever_run_creates_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link()])

    assert _evaluate(cfg, dry_run=True) == []
    assert not _baseline_path(cfg).exists()


def test_disabled_or_absent_pass_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, enabled=False)
    _serve(monkeypatch, [_link()])
    assert _evaluate(cfg) == []
    assert not _baseline_path(cfg).exists()

    cfg.watch.uisp_links = None
    assert _evaluate(cfg) == []
    assert not _baseline_path(cfg).exists()


# --- run_cycle merge (US3 acceptance: dedup, Slack reuse, failure isolation) --------------------


def _run(cfg: Config, *, notifier: FakeNotifier | None = None, now: datetime = NOW, **kw: Any):
    logstore.connect(cfg.logs.db_path).close()  # empty logs store → log pass not degraded
    return run_cycle(
        cfg,
        WatchStore(cfg.watch.watch_dir),
        audit=AuditTrail(cfg.audit_path),
        now=now,
        notifier=notifier,
        **kw,
    )


def test_cycle_merges_link_findings_and_alerts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    cfg.watch.slack_notify = True
    notifier = FakeNotifier()
    _serve(monkeypatch, [_link()])
    _run(cfg, notifier=notifier)  # seed cycle: no findings
    assert notifier.batches == []

    _serve(monkeypatch, [_link(state="disconnected", signal=None)])
    run1 = _run(cfg, notifier=notifier, now=NOW + timedelta(hours=1))
    assert len(run1.finding_ids) == 1
    assert len(notifier.batches) == 1  # existing Slack path, no link-specific delivery code
    assert "link down" in notifier.batches[0][0].summary

    # the condition persists: alert-once holds (dedup suppresses the repeat finding)
    run2 = _run(cfg, notifier=notifier, now=NOW + timedelta(hours=2))
    assert run2.finding_ids == []
    assert len(notifier.batches) == 1


def test_cycle_controller_failure_degrades_and_isolates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _serve(monkeypatch, [_link()])
    _run(cfg)  # seed
    _serve(monkeypatch, [], outcome=SourceOutcome.UNREACHABLE)

    run = _run(cfg, now=NOW + timedelta(hours=1))

    assert "uisp_links_error" in run.conditions
    assert run.finding_ids == []
    assert _baseline(cfg)["L1"]["state"] == "active"  # baseline preserved


def test_cycle_without_the_section_creates_no_baseline(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)  # no [watch.uisp_links], no [uisp]
    run = _run(cfg)
    assert "uisp_links_error" not in run.conditions
    assert not (cfg.watch.watch_dir / _UISP_LINKS_BASELINE).exists()


def test_135_maintenance_link_is_not_baselined_or_alerted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = _cfg(tmp_path)
    cfg.maintenance = MaintenanceSettings(sites=("noc",))
    _serve(monkeypatch, [_link(), _link("L2", "noc-a", "tamarack-b")])
    _evaluate(cfg)  # seed
    assert set(_baseline(cfg)) == {"L1"}
    _serve(
        monkeypatch,
        [_link(state="disconnected"), _link("L2", "noc-a", "tamarack-b", state="disconnected")],
    )
    findings = _evaluate(cfg, now=NOW + timedelta(hours=1))
    assert [f.dedup_extra for f in findings] == ["L1:down"]
