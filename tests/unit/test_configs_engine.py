"""Unit (feature 026): the engine seam — configs_ingest (targets, changed/unchanged/failed tallies,
one audit record, total on per-device failure) and the read functions (show/history/diff/status)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import configstore, engine
from obi.core.audit import AuditTrail
from obi.core.config import Config, DiscoveryScope
from obi.core.drivers.routeros_config import ExportResult
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.inventory import save as save_inventory
from obi.core.models import CaptureOutcome, DiscoveryState
from tests.fakes import config_backup_settings
from tests.fakes import settings as mikrotik_settings

_W = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _export(addr: str, ver: str = "7.15", extra: str = "") -> str:
    return f"# 2026-07-05 12:00:00 by RouterOS {ver}\n/ip address\nadd address={addr}/24\n{extra}"


class StubDriver:
    """A fake RouterosConfigDriver: maps address → ExportResult (or a failure)."""

    def __init__(self, results: dict[str, ExportResult]) -> None:
        self._results = results

    def fetch_export(self, address: str, *, timeout: float) -> ExportResult:
        return self._results[address]


def _config(tmp_path: Path, identities: list[tuple[str, str]]) -> Config:
    entries = [
        InventoryEntry(
            address=addr,
            identity=ident,
            discovery_state=DiscoveryState.IDENTIFIED,
            present_in_latest=True,
        )
        for ident, addr in identities
    ]
    inv_path = tmp_path / "inventory.json"
    save_inventory(
        Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]), entries=entries), inv_path
    )
    return Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        routeros=mikrotik_settings(),
        configs=config_backup_settings(tmp_path),
        inventory_path=inv_path,
        audit_path=tmp_path / "audit.jsonl",
    )


def _audit(config: Config) -> AuditTrail:
    return AuditTrail(config.audit_path)


def _read_audit(config: Config) -> list[dict]:
    return [json.loads(line) for line in config.audit_path.read_text().splitlines()]


def test_ingest_changed_unchanged_failed_and_one_audit(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1"), ("edge-gw-2", "172.25.110.2")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            ),
            "172.25.110.2": ExportResult(
                "172.25.110.2", CaptureOutcome.UNREACHABLE, note="host down"
            ),
        }
    )
    report = engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    assert report.targets_matched == 2
    assert report.changed == 1
    assert report.unchanged == 0
    assert report.failed == 1
    outcomes = {r.identity: r.outcome for r in report.results}
    assert outcomes["core-gw-1"] is CaptureOutcome.CAPTURED_CHANGED
    assert outcomes["edge-gw-2"] is CaptureOutcome.UNREACHABLE
    # exactly one configs_ingest audit record, counts only, no config text
    records = [r for r in _read_audit(config) if r["action"] == "configs_ingest"]
    assert len(records) == 1
    assert records[0]["params"]["changed"] == 1
    assert records[0]["params"]["failed"] == {"unreachable": 1}


def test_ingest_rerun_is_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    first = engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    assert first.changed == 1
    second = engine.configs_ingest(
        config, audit=_audit(config), driver=driver, now=_W + timedelta(hours=1)
    )
    assert second.changed == 0
    assert second.unchanged == 1


def test_ingest_not_configured_returns_guard(tmp_path: Path) -> None:
    config = Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        routeros=mikrotik_settings(),
        configs=None,
        audit_path=tmp_path / "audit.jsonl",
    )
    report = engine.configs_ingest(config, audit=_audit(config))
    assert report.detail is not None and "not configured" in report.detail
    assert report.targets_matched == 0


def test_ingest_no_targets_when_filter_matches_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    config.configs.targets = "nonexistent"
    report = engine.configs_ingest(config, audit=_audit(config), driver=StubDriver({}), now=_W)
    assert report.targets_matched == 0
    assert report.detail is not None


def _seed_two_versions(tmp_path: Path) -> Config:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    d1 = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=d1, now=_W)
    d2 = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.9")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=d2, now=_W + timedelta(days=2))
    return config


def test_show_history_diff_status(tmp_path: Path) -> None:
    config = _seed_two_versions(tmp_path)

    show = engine.config_show(config, "core-gw-1", audit=_audit(config))
    assert show.found and "172.25.110.9/24" in show.content
    assert show.router_version is None or isinstance(show.router_version, str)

    filtered = engine.config_show(
        config, "core-gw-1", contains="172.25.110.9", audit=_audit(config)
    )
    assert "172.25.110.9/24" in filtered.content
    assert "/ip address" not in filtered.content  # only matching lines

    hist = engine.config_history(config, "core-gw-1", audit=_audit(config))
    assert hist.found and len(hist.entries) == 2

    diff = engine.config_diff(config, "core-gw-1", audit=_audit(config))
    assert diff.changed and "172.25.110.9/24" in diff.diff

    status = engine.config_status(config, audit=_audit(config))
    assert status.totals["devices"] == 1
    assert status.totals["backed_up"] == 1


def test_show_unknown_device(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    show = engine.config_show(config, "ghost", audit=_audit(config))
    assert not show.found
    assert show.detail is not None


def test_diff_to_older_version_compares_against_its_own_predecessor(tmp_path: Path) -> None:
    # Regression: with only `to` given, the baseline is the version immediately before `to`
    # in the device's history — not second-newest-overall (which made the second-newest
    # version diff against itself → falsely "no changes", and older versions diff backwards
    # against a newer one).
    config = _seed_two_versions(tmp_path)
    d3 = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.42")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=d3, now=_W + timedelta(days=4))

    hist = engine.config_history(config, "core-gw-1", audit=_audit(config))
    assert hist.found and len(hist.entries) == 3  # newest first

    middle = hist.entries[1]
    diff = engine.config_diff(config, "core-gw-1", to_ref=middle.commit, audit=_audit(config))
    assert diff.changed
    assert "-add address=172.25.110.1/24" in diff.diff
    assert "+add address=172.25.110.9/24" in diff.diff
    assert "172.25.110.42" not in diff.diff  # nothing from the newer version leaks in

    # the first capture has no predecessor → the whole file, rendered as all additions
    oldest = hist.entries[2]
    first = engine.config_diff(config, "core-gw-1", to_ref=oldest.commit, audit=_audit(config))
    assert first.found and first.changed
    assert first.from_ref == "(empty)"
    assert "+add address=172.25.110.1/24" in first.diff
    assert not [
        line for line in first.diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]


def test_diff_single_version_shows_entire_file(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    diff = engine.config_diff(config, "core-gw-1", audit=_audit(config))
    assert diff.found and diff.changed
    assert diff.from_ref == "(empty)"
    assert "+add address=172.25.110.1/24" in diff.diff


def test_status_orphan_device_retained(tmp_path: Path) -> None:
    # capture core-gw-1, then a config whose roster no longer lists it → orphaned but retained.
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    # rebuild the inventory WITHOUT core-gw-1
    save_inventory(
        Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]), entries=[]), config.inventory_path
    )
    status = engine.config_status(config, audit=_audit(config))
    orphans = [r for r in status.rows if not r.in_roster]
    assert len(orphans) == 1
    assert orphans[0].identity == "core-gw-1"
    assert orphans[0].has_backup is True


# --- capture-freshness staleness (feature 085) ---------------------------------------------------
#
# `stale` is CAPTURE health: no successful capture within --stale-after. A config that merely
# has not CHANGED is healthy (the operator's 2026-08-19 complaint: 16 long-settled Alder
# devices flagged "stale" read as "something is wrong").


def test_status_unchanged_config_with_fresh_capture_is_not_stale(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    # 90 days later the config has never changed, but every capture succeeded — re-ingest
    # (unchanged) and ask for status shortly after: NOT stale, change age irrelevant.
    late = _W + timedelta(days=90)
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=late)
    status = engine.config_status(config, audit=_audit(config), now=late + timedelta(hours=1))
    (row,) = [r for r in status.rows if r.in_roster]
    assert row.last_changed_at is not None and row.last_changed_at < late - timedelta(days=80)
    assert row.last_captured_at == late
    assert row.stale is False
    assert status.totals["stale"] == 0


def test_status_old_capture_is_stale_regardless_of_changes(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    status = engine.config_status(
        config, audit=_audit(config), now=_W + timedelta(days=3), stale_after="1d"
    )
    (row,) = [r for r in status.rows if r.in_roster]
    assert row.stale is True
    assert status.totals["stale"] == 1


def test_status_failed_attempt_keeps_last_good_and_notes_it(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    ok = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=ok, now=_W)
    dead = StubDriver({"172.25.110.1": ExportResult("172.25.110.1", CaptureOutcome.UNREACHABLE)})
    engine.configs_ingest(config, audit=_audit(config), driver=dead, now=_W + timedelta(hours=2))
    status = engine.config_status(config, audit=_audit(config), now=_W + timedelta(hours=3))
    (row,) = [r for r in status.rows if r.in_roster]
    assert row.last_captured_at == _W  # last known good preserved through the failure
    assert row.stale is False  # still within the 1d window
    assert row.note is not None and "unreachable" in row.note


def test_status_pre_085_store_without_capture_record_is_not_stale(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    # simulate a pre-085 store: the sidecar does not exist
    configstore.capture_state_path(config.configs.repo_path).unlink()
    status = engine.config_status(config, audit=_audit(config), now=_W + timedelta(days=30))
    (row,) = [r for r in status.rows if r.in_roster]
    assert row.stale is False
    assert row.last_captured_at is None
    assert row.note == "no capture record yet"


def test_status_orphan_is_not_stale(tmp_path: Path) -> None:
    config = _config(tmp_path, [("core-gw-1", "172.25.110.1")])
    driver = StubDriver(
        {
            "172.25.110.1": ExportResult(
                "172.25.110.1", CaptureOutcome.CAPTURED_CHANGED, content=_export("172.25.110.1")
            )
        }
    )
    engine.configs_ingest(config, audit=_audit(config), driver=driver, now=_W)
    save_inventory(
        Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]), entries=[]), config.inventory_path
    )
    status = engine.config_status(config, audit=_audit(config), now=_W + timedelta(days=30))
    (orphan,) = [r for r in status.rows if not r.in_roster]
    assert orphan.stale is False
    assert orphan.note == "not in current roster"
    assert status.totals["stale"] == 0
