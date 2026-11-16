"""Unit (feature 070): the config-change pass inside `obi watch run`.

Covers the helpers (baseline round-trip, bounded diff), the detector mapping (a new committed
version → a `source="config"` finding carrying the redacted diff), the baseline lifecycle
(first-run silent seed, alert-once via baseline advance, re-alert on a later change, new/removed
device), the ignore list, the large-diff bound, the run_cycle merge (dedup, Slack reuse, failure
isolation), offline/opt-in/resilience, and dry-run inertness.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obi.core import configstore, logstore
from obi.core.audit import AuditTrail
from obi.core.config import Config, WatchConfigChangesSettings
from obi.core.watch import (
    _CONFIG_BASELINE,
    _bounded_diff,
    _evaluate_config_changes,
    _load_config_baseline,
    _store_config_baseline,
    run_cycle,
)
from obi.core.watchstore import WatchStore
from tests.fakes import FakeNotifier, watch_config

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)

# Two configs differing only in one /ip address line; the /snmp community `name=` is a secret the
# feature-026 store redacts before commit (so the diff must show `<redacted-by-obi>`, never the
# cleartext). The redacted line sits within git's default 3-line context of the changed line.
_SNMP = "/snmp community\nset [ find default=yes ] name=topsecret\n"
V1 = _SNMP + "/ip address\nadd address=172.25.110.1/24 interface=ether1\n"
V2 = _SNMP + "/ip address\nadd address=172.25.110.2/24 interface=ether1\n"


def _repo(tmp_path: Path) -> Path:
    return tmp_path / "config-repo"


def _commit(tmp_path: Path, identity: str, body: str, *, address: str = "172.25.110.1") -> str:
    sha = configstore.record_version(
        _repo(tmp_path), identity, body, address=address, router_version="7.15", when=NOW
    )
    assert sha is not None
    return sha


def _cfg(tmp_path: Path, *, enabled: bool = True, slack: bool = False, **kw: object) -> Config:
    cfg = watch_config(tmp_path, slack=slack)
    cfg.watch.config_changes = WatchConfigChangesSettings(
        enabled=enabled, repo_path=_repo(tmp_path), **kw  # type: ignore[arg-type]
    )
    return cfg


def _baseline_path(cfg: Config) -> Path:
    return cfg.watch.watch_dir / _CONFIG_BASELINE


def _seed_baseline(cfg: Config, mapping: dict[str, str]) -> None:
    _store_config_baseline(_baseline_path(cfg), mapping)


def _baseline(cfg: Config) -> dict[str, str]:
    return _load_config_baseline(_baseline_path(cfg))


def _eval(cfg: Config, *, now: datetime = NOW, dry_run: bool = False):
    return _evaluate_config_changes(cfg, AuditTrail(cfg.audit_path), now=now, dry_run=dry_run)


def _audit_actions(cfg: Config, action: str) -> list[dict]:
    if not cfg.audit_path.exists():
        return []
    records = [json.loads(line) for line in cfg.audit_path.read_text().splitlines() if line]
    return [r for r in records if r["action"] == action]


def _empty_logs(cfg: Config) -> None:
    logstore.connect(cfg.logs.db_path).close()  # empty logs store → the log pass is not degraded


def _run(cfg: Config, *, notifier: FakeNotifier | None = None, now: datetime = NOW):
    return run_cycle(
        cfg,
        WatchStore(cfg.watch.watch_dir),
        audit=AuditTrail(cfg.audit_path),
        now=now,
        notifier=notifier,
    )


# --- T004: helpers ----------------------------------------------------------------------------


def test_baseline_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "watch" / _CONFIG_BASELINE
    _store_config_baseline(path, {"core-gw-1": "abc123", "walnut-gw": "def456"})
    assert _load_config_baseline(path) == {"core-gw-1": "abc123", "walnut-gw": "def456"}


def test_baseline_missing_or_corrupt_loads_empty(tmp_path: Path) -> None:
    assert _load_config_baseline(tmp_path / "nope.json") == {}  # missing → {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _load_config_baseline(bad) == {}  # corrupt → {}
    non_dict = tmp_path / "list.json"
    non_dict.write_text("[1, 2, 3]")
    assert _load_config_baseline(non_dict) == {}  # non-dict → {}
    mixed = tmp_path / "mixed.json"
    mixed.write_text('{"a": "sha", "b": 5}')
    assert _load_config_baseline(mixed) == {"a": "sha"}  # non-str values dropped


def test_bounded_diff_counts_strips_and_truncates() -> None:
    diff = (
        "diff --git a/x.rsc b/x.rsc\nindex 111..222 100644\n--- a/x.rsc\n+++ b/x.rsc\n"
        "@@ -1,2 +1,2 @@\n /ip address\n"
        "-add address=172.25.110.1/24\n+add address=172.25.110.2/24\n"
    )
    body, ins, dele = _bounded_diff(diff, "core-gw-1", 120)
    assert ins == 1 and dele == 1
    assert "diff --git" not in body and "index " not in body  # preamble stripped
    assert "--- a/x.rsc" not in body and "+++ b/x.rsc" not in body
    assert "@@ -1,2 +1,2 @@" in body and "+add address=172.25.110.2/24" in body


def test_bounded_diff_truncation_marker() -> None:
    diff = "@@ -1,10 +1,10 @@\n" + "".join(f"+line {i}\n" for i in range(10))
    body, ins, _ = _bounded_diff(diff, "core-gw-1", 5)
    assert ins == 10
    assert "truncated (5 of 11 lines)" in body
    assert "obi configs diff core-gw-1" in body


def test_bounded_diff_empty_is_unavailable_note() -> None:
    body, ins, dele = _bounded_diff("", "core-gw-1", 120)
    assert ins == 0 and dele == 0
    assert "diff unavailable" in body and "obi configs diff core-gw-1" in body


# --- T005: detector mapping -------------------------------------------------------------------


def test_change_maps_to_finding_with_redacted_diff(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha1 = _commit(tmp_path, "core-gw-1", V1, address="172.18.84.1")
    _seed_baseline(cfg, {"core-gw-1": sha1})
    sha2 = _commit(tmp_path, "core-gw-1", V2, address="172.18.84.1")

    findings = _eval(cfg, now=NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.source == "config"
    assert f.severity == "warning"
    assert f.confidence == "high"
    assert f.device == "core-gw-1"
    assert "core-gw-1: config changed" in f.summary and "+1" in f.summary
    assert f.dedup_extra == sha2[:7]
    assert f.evidence and f.evidence[0].kind == "config"
    assert f"{sha1[:7]}" in f.evidence[0].text and f"{sha2[:7]}" in f.evidence[0].text
    # the diff is the REDACTED stored content — the cleartext community never appears
    assert "topsecret" not in f.description
    assert "<redacted-by-obi>" in f.description
    assert "add address=172.25.110.2/24" in f.description


def test_unchanged_device_no_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha1 = _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": sha1})
    assert _eval(cfg, now=NOW) == []  # HEAD == baseline


def test_ignored_device_excluded(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ignore_devices=["lab-"])
    sha1 = _commit(tmp_path, "lab-gw", V1)
    _seed_baseline(cfg, {"lab-gw": sha1})
    _commit(tmp_path, "lab-gw", V2)  # changed, but ignored
    assert _eval(cfg, now=NOW) == []


# --- feature 099: reorder-only advances are suppressed ----------------------------------------

# The noc-ap-birch shape: identical lines, one entry emitted at a different position.
_LINES = "/ip pool add name=guest\n/ip dhcp-server add pool=guest\n/ip dhcp-client add i=v\n"
_MOVED = "/ip pool add name=guest\n/ip dhcp-client add i=v\n/ip dhcp-server add pool=guest\n"


def test_permutation_advance_is_suppressed_but_audited(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha1 = _commit(tmp_path, "noc-ap-birch", _LINES)
    _seed_baseline(cfg, {"noc-ap-birch": sha1})
    sha2 = _commit(tmp_path, "noc-ap-birch", _MOVED)

    assert _eval(cfg, now=NOW) == []  # no finding → no Slack alert
    assert _baseline(cfg) == {"noc-ap-birch": sha2}  # baseline advanced anyway
    events = _audit_actions(cfg, "watch_config_reorder_suppressed")
    assert len(events) == 1
    assert events[0]["target"] == "noc-ap-birch"
    assert events[0]["params"]["prev"] == sha1 and events[0]["params"]["current"] == sha2
    # advanced baseline means the suppressed flip never re-alerts on a later cycle
    assert _eval(cfg, now=NOW) == []
    assert len(_audit_actions(cfg, "watch_config_reorder_suppressed")) == 1


def test_permutation_plus_real_change_alerts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha1 = _commit(tmp_path, "noc-ap-birch", _LINES)
    _seed_baseline(cfg, {"noc-ap-birch": sha1})
    _commit(tmp_path, "noc-ap-birch", _MOVED.replace("name=guest", "name=lan"))

    findings = _eval(cfg, now=NOW)
    assert len(findings) == 1 and findings[0].device == "noc-ap-birch"
    assert _audit_actions(cfg, "watch_config_reorder_suppressed") == []


def test_permutation_window_spans_multiple_commits(tmp_path: Path) -> None:
    # several capture commits between watch cycles whose NET effect is a permutation → silent
    cfg = _cfg(tmp_path)
    sha1 = _commit(tmp_path, "noc-ap-birch", _LINES)
    _seed_baseline(cfg, {"noc-ap-birch": sha1})
    _commit(tmp_path, "noc-ap-birch", _MOVED.replace("name=guest", "name=lan"))
    _commit(tmp_path, "noc-ap-birch", _MOVED)  # edit reverted; only the reorder remains

    assert _eval(cfg, now=NOW) == []
    assert len(_audit_actions(cfg, "watch_config_reorder_suppressed")) == 1


def test_unreadable_baseline_ref_falls_back_to_alerting(tmp_path: Path) -> None:
    # baseline sha predates the device's file (rename case) → content_at(prev) is None →
    # suppression needs positive proof, so the pass alerts as today
    cfg = _cfg(tmp_path)
    sha_other = _commit(tmp_path, "other-gw", V1)
    _commit(tmp_path, "noc-ap-birch", _LINES)
    _seed_baseline(cfg, {"noc-ap-birch": sha_other})

    findings = _eval(cfg, now=NOW)
    assert len(findings) == 1 and findings[0].device == "noc-ap-birch"
    assert _audit_actions(cfg, "watch_config_reorder_suppressed") == []


def test_warning_transition_commit_is_suppressed(tmp_path: Path) -> None:
    # research R5: a pre-099 stored version still carrying warning-form content advances to the
    # new warning-free normalized form — the watch comparison normalizes BOTH sides, so this
    # one-time transition is silent too. Simulated by committing the raw forms verbatim via git
    # (record_version would now normalize them away).
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    warn = "/tool netwatch\n# Warning: probe waiting startup-delay=5m\nadd host=8.8.8.8\n"
    terse = "/tool netwatch add host=8.8.8.8\n"
    configstore.ensure_repo(repo)
    shas = []
    for body in (warn, terse):
        (repo / "basswood-gw.rsc").write_text(body)
        configstore._git(repo, "add", "-A")
        configstore._git(repo, *configstore._GIT_COMMIT_CONFIG, "commit", "-qm", "v")
        shas.append(configstore._git(repo, "rev-parse", "HEAD").strip())
    _seed_baseline(cfg, {"basswood-gw": shas[0]})

    assert _eval(cfg, now=NOW) == []
    assert len(_audit_actions(cfg, "watch_config_reorder_suppressed")) == 1


# --- T006: run_cycle merge + delivery ---------------------------------------------------------


def test_run_cycle_delivers_config_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    sha1 = _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": sha1})
    _commit(tmp_path, "core-gw-1", V2)
    notifier = FakeNotifier()
    run = _run(cfg, notifier=notifier)
    assert len(run.finding_ids) == 1
    assert notifier.batches and notifier.batches[0][0].source == "config"
    assert list((cfg.watch.watch_dir / "reports").glob("*.md"))  # report written


def test_run_cycle_multiple_devices_each_produce_a_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    sha_a = _commit(tmp_path, "core-gw-1", V1)
    sha_b = _commit(tmp_path, "walnut-gw", V1, address="172.18.81.1")
    _seed_baseline(cfg, {"core-gw-1": sha_a, "walnut-gw": sha_b})
    _commit(tmp_path, "core-gw-1", V2)
    _commit(tmp_path, "walnut-gw", V2, address="172.18.81.1")
    notifier = FakeNotifier()
    run = _run(cfg, notifier=notifier)
    assert len(run.finding_ids) == 2
    delivered = {f.device for f in notifier.batches[0]}
    assert delivered == {"core-gw-1", "walnut-gw"}


# --- T011 (US2): first-run baseline + alert-once + re-alert ------------------------------------


def test_first_run_seeds_silently(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _commit(tmp_path, "core-gw-1", V1)
    sha2 = _commit(tmp_path, "core-gw-1", V2)  # history exists before first enable
    assert _eval(cfg, now=NOW) == []  # silent baseline, no backlog flood
    assert _baseline(cfg) == {"core-gw-1": sha2}  # seeded at current HEAD


def test_alert_once_then_realert_on_next_change(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, slack=True)
    _empty_logs(cfg)
    store = WatchStore(cfg.watch.watch_dir)
    _commit(tmp_path, "core-gw-1", V1)
    # first cycle seeds silently
    run0 = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, notifier=FakeNotifier())
    assert run0.finding_ids == []
    # a change → one alert
    _commit(tmp_path, "core-gw-1", V2)
    n1 = FakeNotifier()
    run1 = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, notifier=n1)
    assert len(run1.finding_ids) == 1 and len(n1.batches[0]) == 1
    # same version, next cycle → no repeat (baseline advanced)
    n2 = FakeNotifier()
    run2 = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, notifier=n2)
    assert run2.finding_ids == [] and n2.batches == []
    # a further change → re-alert
    _commit(tmp_path, "core-gw-1", V1)  # change again (back to V1 content = a new commit)
    n3 = FakeNotifier()
    run3 = run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, notifier=n3)
    assert len(run3.finding_ids) == 1 and len(n3.batches[0]) == 1


# --- T012 (US3): offline + independence -------------------------------------------------------


def test_detects_offline_no_llm(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _empty_logs(cfg)
    sha1 = _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": sha1})
    _commit(tmp_path, "core-gw-1", V2)
    # run_cycle with llm_client=None (default) — detection needs no cloud
    run = _run(cfg)
    assert len(run.finding_ids) == 1


def test_pass_failure_isolated(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    _empty_logs(cfg)
    _commit(tmp_path, "core-gw-1", V1)  # repo exists, so the pass reaches coverage()

    def boom(*_a: object, **_k: object) -> list[object]:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(configstore, "coverage", boom)
    run = _run(cfg)  # the cycle must survive a config-pass failure
    assert "config_error" in run.conditions
    assert run.outcome == "degraded"


# --- T013 (US3): opt-in + resilience ----------------------------------------------------------


def test_disabled_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enabled=False)
    _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": "stale"})
    assert _eval(cfg, now=NOW) == []


def test_absent_subsection_is_noop(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path)
    cfg.watch.config_changes = None
    assert _eval(cfg, now=NOW) == []


def test_missing_or_empty_repo_no_crash(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # repo never created
    assert _eval(cfg, now=NOW) == []
    _repo(tmp_path).mkdir(parents=True)  # dir exists but no .git
    assert _eval(cfg, now=NOW) == []


def test_new_device_is_seeded_not_alerted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha_a = _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": sha_a})  # baseline knows only core-gw-1
    sha_b = _commit(tmp_path, "walnut-gw", V1, address="172.18.81.1")  # a newly-covered device
    assert _eval(cfg, now=NOW) == []  # first sighting → no alert
    assert _baseline(cfg)["walnut-gw"] == sha_b  # but baselined


def test_removed_device_drops_without_finding(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha_a = _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": sha_a, "gone-gw": "oldsha"})  # gone-gw not in the repo
    assert _eval(cfg, now=NOW) == []
    assert "gone-gw" not in _baseline(cfg)  # stale key drops out


def test_large_diff_is_truncated(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, max_diff_lines=5)
    # v1 and v2 differ on EVERY line, so the diff is ~80 ± lines — well past the 5-line bound
    # (appending a single line would only yield a small tail hunk).
    v1 = "/ip firewall filter\n" + "".join(f"add comment=old{i}\n" for i in range(40))
    v2 = "/ip firewall filter\n" + "".join(f"add comment=new{i}\n" for i in range(40))
    sha1 = _commit(tmp_path, "core-gw-1", v1)
    _seed_baseline(cfg, {"core-gw-1": sha1})
    _commit(tmp_path, "core-gw-1", v2)
    findings = _eval(cfg, now=NOW)
    assert len(findings) == 1
    assert "truncated" in findings[0].description
    assert "obi configs diff core-gw-1" in findings[0].description


def test_dry_run_writes_no_baseline(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    sha1 = _commit(tmp_path, "core-gw-1", V1)
    _seed_baseline(cfg, {"core-gw-1": sha1})
    _commit(tmp_path, "core-gw-1", V2)
    findings = _eval(cfg, now=NOW, dry_run=True)
    assert len(findings) == 1  # still detects
    assert _baseline(cfg) == {"core-gw-1": sha1}  # but the baseline is NOT advanced
