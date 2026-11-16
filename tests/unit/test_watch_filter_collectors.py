"""feature 039: ``[watch] ignore_collectors`` drops whole log collectors from the judge window.

Collector-level exclusion is what ``ignore_topics`` structurally cannot do: a single collector
(e.g. UniFi) emits rows under many varied topic categories, so no one topic pattern removes them
all. These tests pin that a named collector is dropped regardless of topic, that matching is
case-insensitive, that it fires even when ``routeros_only`` would otherwise admit the collector,
and that the empty default changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.models import LogEvent
from obi.core.watch import _filter_window
from tests.fakes import watch_config, watch_inventory

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def _ev(host: str, collectors: list[str], *, topics: str = "x,info", msg: str = "m") -> LogEvent:
    return LogEvent(ts=NOW, host=host, topics=topics, message=msg, collectors=list(collectors))


def test_ignore_collectors_drops_all_rows_of_that_collector(tmp_path: Path) -> None:
    """Every UniFi row is dropped no matter its topic; RouterOS and UISP rows survive."""
    cfg = watch_config(tmp_path, routeros_only=True, ignore_collectors=["unifi"])
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("core-1", ["core"], topics="system,error,critical")),  # RouterOS -> kept
        # the three distinct UniFi topic categories — one ignore entry covers all of them:
        (2, _ev("Alder", ["unifi"], topics="unifi_devices,system_wifi,device_stop_mesh")),
        (3, _ev("Alder", ["unifi"], topics="audit,system_admin,admin_access")),
        (
            4,
            _ev(
                "Chestnut", ["unifi"], topics="software_updates,system_devices,firmware_updated_v2"
            ),
        ),
        (5, _ev("SF - Site", ["uisp"], topics="uisp,outage,outage")),  # UISP -> kept
    ]
    kept = [(e.host, tuple(e.collectors)) for _id, e in _filter_window(events, cfg)]
    assert kept == [("core-1", ("core",)), ("SF - Site", ("uisp",))]


def test_ignore_collectors_is_case_insensitive(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True, ignore_collectors=["UniFi"])
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("Alder", ["UNIFI"])),  # event collector cased differently -> still dropped
        (2, _ev("core-1", ["core"])),
    ]
    kept = {e.host for _id, e in _filter_window(events, cfg)}
    assert kept == {"core-1"}


def test_ignore_collectors_applies_even_without_routeros_only(tmp_path: Path) -> None:
    """With routeros_only off the curated-source gate is inactive, but the collector still drops."""
    cfg = watch_config(tmp_path, routeros_only=False, ignore_collectors=["unifi"])
    events = [
        (1, _ev("Alder", ["unifi"])),
        (2, _ev("anything", ["core"])),
    ]
    kept = {e.host for _id, e in _filter_window(events, cfg)}
    assert kept == {"anything"}


def test_ignore_collectors_beats_the_curated_source_admit(tmp_path: Path) -> None:
    """routeros_only *admits* unifi/uisp (features 016/019); ignore_collectors overrides that."""
    cfg = watch_config(tmp_path, routeros_only=True, ignore_collectors=["unifi", "uisp"])
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("core-1", ["core"])),  # RouterOS -> kept
        (2, _ev("Alder", ["unifi"])),  # admitted by the gate, then dropped by ignore_collectors
        (3, _ev("SF", ["uisp"])),  # admitted by the gate, then dropped by ignore_collectors
    ]
    kept = {e.host for _id, e in _filter_window(events, cfg)}
    assert kept == {"core-1"}


def test_empty_ignore_collectors_keeps_curated_sources(tmp_path: Path) -> None:
    """The default (empty list) is inert — UniFi/UISP admitted exactly as before this feature."""
    cfg = watch_config(tmp_path, routeros_only=True)  # ignore_collectors defaults to []
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("core-1", ["core"])),
        (2, _ev("Alder", ["unifi"])),
    ]
    kept = {e.host for _id, e in _filter_window(events, cfg)}
    assert kept == {"core-1", "Alder"}


def test_ignore_collectors_does_not_touch_ingest_window_input(tmp_path: Path) -> None:
    """The knob filters the OUTPUT window only; the caller's event list is left intact."""
    cfg = watch_config(tmp_path, routeros_only=True, ignore_collectors=["unifi"])
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("Alder", ["unifi"])),
        (2, _ev("core-1", ["core"])),
    ]
    _filter_window(events, cfg)
    assert [e.host for _id, e in events] == ["Alder", "core-1"]  # unchanged
