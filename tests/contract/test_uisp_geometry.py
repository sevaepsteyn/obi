"""Contract tests: the UISP geometry ingest (feature 022, T013 — contracts/uisp-geometry.md).

The driver must issue ONLY the three allowlisted read paths, map the recorded fixtures
faithfully, and stay total-in-the-UispError-sense (a controller failure raises the classified
error; nothing is partially returned).
"""

from __future__ import annotations

from typing import Any

import pytest

from obi.core.drivers.uisp import (
    UispController,
    UispError,
    map_geo_device,
    map_geo_link,
    map_geo_site,
)
from obi.core.models import SourceOutcome
from tests.fakes import load_uisp_geometry, uisp_settings

_FIXTURES = {
    "/nms/api/v2.1/devices": load_uisp_geometry("devices"),
    "/nms/api/v2.1/data-links": load_uisp_geometry("data-links"),
    "/nms/api/v2.1/sites": load_uisp_geometry("sites"),
}


class RecordingGeoTransport:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.paths: list[str] = []
        self.fail_on = fail_on

    def __call__(self, path: str, timeout: float) -> list[dict[str, Any]]:
        self.paths.append(path)
        if self.fail_on and path == self.fail_on:
            raise UispError(SourceOutcome.UNREACHABLE, "controller went away")
        assert path in _FIXTURES, f"driver requested a non-allowlisted path: {path}"
        return _FIXTURES[path]


def test_geometry_issues_exactly_the_three_allowlisted_reads() -> None:
    transport = RecordingGeoTransport()
    controller = UispController(uisp_settings(), geo_transport=transport)
    controller.geometry(timeout=5.0)
    assert transport.paths == [
        "/nms/api/v2.1/devices",
        "/nms/api/v2.1/data-links",
        "/nms/api/v2.1/sites",
    ]


def test_geometry_maps_the_recorded_fleet() -> None:
    controller = UispController(uisp_settings(), geo_transport=RecordingGeoTransport())
    snapshot = controller.geometry(timeout=5.0)
    assert len(snapshot.devices) == 77
    assert len(snapshot.links) == 42
    assert len(snapshot.sites) == 38
    # fixture-verified field shapes (see geometry-README.md)
    noc = next(d for d in snapshot.devices if d.name == "noc-ptp-basswood-60")
    assert noc.site == "BRC - NOC"
    assert noc.latitude == pytest.approx(40.782724)
    assert noc.altitude == pytest.approx(1217.7)
    assert noc.status == "disconnected"
    # 025: the cached per-radio channel maps through — even on a disconnected radio
    assert noc.frequency_mhz == pytest.approx(65880.0)
    link_types = {link.link_type for link in snapshot.links}
    assert link_types <= {"wireless", "ethernet"}  # wiredness is `type`, not a `wired` key
    # 025: the link record's own channel maps through (tier-1 frequency source) and is
    # populated on every wireless link in the capture, disconnected ones included
    noc_mountain = next(
        link
        for link in snapshot.links
        if {link.from_name, link.to_name} == {"noc-ptp-basswood-60", "basswood-ptp-noc-60"}
    )
    assert noc_mountain.frequency_mhz == pytest.approx(65880.0)
    wireless = [link for link in snapshot.links if link.link_type == "wireless"]
    assert wireless and all(link.frequency_mhz is not None for link in wireless)


def test_geometry_controller_failure_raises_the_classified_error() -> None:
    transport = RecordingGeoTransport(fail_on="/nms/api/v2.1/data-links")
    controller = UispController(uisp_settings(), geo_transport=transport)
    with pytest.raises(UispError) as excinfo:
        controller.geometry(timeout=5.0)
    assert excinfo.value.outcome is SourceOutcome.UNREACHABLE


def test_mappers_tolerate_junk_records() -> None:
    assert map_geo_device({}) is None
    assert map_geo_device({"identification": {"name": ""}}) is None
    assert map_geo_link({}) is None
    assert map_geo_site({}) is None
    # a named device with everything else missing maps with None fields (core decides)
    device = map_geo_device({"identification": {"name": "x-ptp-y-60"}})
    assert device is not None
    assert device.latitude is None and device.site is None and device.status is None
    assert device.frequency_mhz is None
    link = map_geo_link({"id": "l1"})
    assert link is not None and link.frequency_mhz is None
