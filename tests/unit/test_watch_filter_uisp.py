"""T015: the watch curated-source filter admits UISP rows under routeros_only (feature 019).

Also covers feature 035: the UISP-scoped ``[uisp] ignore_names`` drop inside ``_filter_window``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.models import LogEvent
from obi.core.watch import _filter_window
from tests.fakes import uisp_settings, watch_config, watch_inventory

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _ev(host: str, collectors: list[str], *, topics: str = "x,info", msg: str = "m") -> LogEvent:
    return LogEvent(ts=NOW, host=host, topics=topics, message=msg, collectors=list(collectors))


def test_admits_uisp_drops_other_under_routeros_only(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])  # RouterOS identity core-1
    events = [
        (1, _ev("core-1", ["core"])),  # RouterOS host in inventory -> kept
        (2, _ev("Alder", ["uisp"])),  # UISP-sourced -> kept (curated source)
        (3, _ev("Alder", ["unifi"])),  # UniFi-sourced -> still kept (feature 016)
        (4, _ev("rando-host", ["core"])),  # non-curated -> dropped
    ]
    kept = {event.host for _id, event in _filter_window(events, cfg)}
    assert kept == {"core-1", "Alder"}
    kept_collectors = {tuple(e.collectors) for _i, e in _filter_window(events, cfg)}
    assert ("uisp",) in kept_collectors


def test_ignore_topics_still_applies_to_uisp_rows(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True, ignore_topics=["uisp,log,info"])
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("Alder", ["uisp"], topics="uisp,log,info")),  # ignored topic -> dropped
        (2, _ev("Alder", ["uisp"], topics="uisp,outage,outage")),  # kept
    ]
    kept = [event.topics for _id, event in _filter_window(events, cfg)]
    assert kept == ["uisp,outage,outage"]


def test_uisp_ignore_names_drops_matching_uisp_rows(tmp_path: Path) -> None:
    """feature 035: an out-of-scope UISP site (host) is dropped; other sites still pass."""
    cfg = watch_config(tmp_path, routeros_only=True)
    cfg.uisp = uisp_settings(ignore_names=["aspen"])
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("SF - ASPEN", ["uisp"], topics="uisp,outage,outage")),  # out-of-scope -> dropped
        (2, _ev("Alder", ["uisp"], topics="uisp,outage,outage")),  # in-scope -> kept
    ]
    kept = [event.host for _id, event in _filter_window(events, cfg)]
    assert kept == ["Alder"]
    # Ingest is untouched: the raw window still holds the aspen row; only the output drops it.
    assert any(event.host == "SF - ASPEN" for _id, event in events)


def test_uisp_ignore_names_is_uisp_scoped(tmp_path: Path) -> None:
    """feature 035 (FR-003): a RouterOS host that coincidentally contains the substring is kept."""
    cfg = watch_config(tmp_path, routeros_only=True)
    cfg.uisp = uisp_settings(ignore_names=["aspen"])
    watch_inventory(cfg.inventory_path, ["aspen-core-1"])  # a RouterOS host literally named aspen-*
    events = [
        (1, _ev("aspen-core-1", ["aspen-core-1"], topics="system,error")),  # RouterOS -> kept
        (2, _ev("SF - ASPEN", ["uisp"], topics="uisp,outage,outage")),  # UISP -> dropped
    ]
    kept = [event.host for _id, event in _filter_window(events, cfg)]
    assert kept == ["aspen-core-1"]


def test_empty_uisp_ignore_names_keeps_uisp_rows(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    cfg.uisp = uisp_settings()  # ignore_names defaults to [] ⇒ excludes nothing
    events = [(1, _ev("SF - ASPEN", ["uisp"], topics="uisp,outage,outage"))]
    kept = [event.host for _id, event in _filter_window(events, cfg)]
    assert kept == ["SF - ASPEN"]


def test_135_maintenance_drops_declared_uisp_site_and_site_hosts(tmp_path: Path) -> None:
    from obi.core.config import MaintenanceSettings

    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1", "magnolia-gw"])
    cfg.maintenance = MaintenanceSettings(sites=("magnolia",), uisp_sites=("brc - business park",))
    events = [
        (1, _ev("BRC - Business Park", ["uisp"], topics="uisp,outage,outage")),  # dropped
        (2, _ev("Tamarack", ["uisp"], topics="uisp,outage,outage")),  # kept
        (3, _ev("magnolia-gw", ["core"])),  # site token ⇒ dropped
        (4, _ev("core-1", ["core"])),  # kept
        (5, _ev("BRC - Cottonwood - MRS", ["uisp"])),  # NOT declared as a UISP site ⇒ kept
    ]
    kept = [event.host for _id, event in _filter_window(events, cfg)]
    assert kept == ["Tamarack", "core-1", "BRC - Cottonwood - MRS"]
