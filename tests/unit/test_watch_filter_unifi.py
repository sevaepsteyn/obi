"""T017: the watch curated-source filter admits UniFi rows under routeros_only (feature 016)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.models import LogEvent
from obi.core.watch import _filter_window
from tests.fakes import watch_config, watch_inventory

NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _ev(host: str, collectors: list[str], *, topics: str = "x,info", msg: str = "m") -> LogEvent:
    return LogEvent(ts=NOW, host=host, topics=topics, message=msg, collectors=list(collectors))


def test_admits_unifi_drops_other_under_routeros_only(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, routeros_only=True)
    watch_inventory(cfg.inventory_path, ["core-1"])  # RouterOS identity core-1
    events = [
        (1, _ev("core-1", ["core"])),  # RouterOS host in inventory -> kept
        (2, _ev("Alder", ["unifi"])),  # UniFi-sourced -> kept (curated source)
        (3, _ev("rando-switch", ["core"])),  # non-RouterOS, non-UniFi -> dropped
    ]
    kept = {event.host for _id, event in _filter_window(events, cfg)}
    assert kept == {"core-1", "Alder"}


def test_ignore_topics_still_applies_to_unifi_rows(tmp_path: Path) -> None:
    cfg = watch_config(
        tmp_path, routeros_only=True, ignore_topics=["unifi_devices,system_wifi"]
    )
    watch_inventory(cfg.inventory_path, ["core-1"])
    events = [
        (1, _ev("Alder", ["unifi"], topics="unifi_devices,system_wifi,device_stop_mesh")),
        (2, _ev("Alder", ["unifi"], topics="power,outage")),
    ]
    kept = [event.topics for _id, event in _filter_window(events, cfg)]
    assert kept == ["power,outage"]  # the mesh-flap topic was ignored, the power event kept
