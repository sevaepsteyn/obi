"""Contract tests: the UISP link-health read (feature 034 — contracts/uisp-links-rest.md).

``UispController.links()`` must issue ONLY the two allowlisted read paths (in order), keep only
wireless links, join per-end radio quality by the controller device id (a miss keeps the link with
``radio=None``), map the recorded untrimmed fixtures faithfully, and classify controller failures
exactly like every other UISP read.
"""

from __future__ import annotations

from typing import Any

import pytest

from obi.core.drivers.uisp import (
    UispController,
    UispError,
    map_radio_health,
    map_wireless_link,
)
from obi.core.models import SourceOutcome
from tests.fakes import load_uisp_links_fixture, uisp_settings

_FIXTURES = {
    "/nms/api/v2.1/data-links": load_uisp_links_fixture("data-links"),
    "/nms/api/v2.1/devices": load_uisp_links_fixture("devices"),
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


def _links(transport: RecordingGeoTransport | None = None) -> list[Any]:
    controller = UispController(
        uisp_settings(), geo_transport=transport or RecordingGeoTransport()
    )
    return controller.links(timeout=5.0)


def test_links_issues_exactly_the_two_allowlisted_reads() -> None:
    transport = RecordingGeoTransport()
    _links(transport)
    assert transport.paths == ["/nms/api/v2.1/data-links", "/nms/api/v2.1/devices"]


def test_links_keeps_only_wireless_records() -> None:
    links = _links()
    raw = _FIXTURES["/nms/api/v2.1/data-links"]
    wireless = [r for r in raw if str(r.get("type", "")).lower() == "wireless"]
    assert len(raw) > len(wireless)  # the capture contains ethernet links to exclude
    assert len(links) == len(wireless) == 40


def test_links_join_covers_the_recorded_fleet() -> None:
    # 0 natural join misses in the capture: every wireless end resolves to a device record.
    links = _links()
    assert links and all(
        end.radio is not None for link in links for end in (link.end_a, link.end_b)
    )


def test_healthy_link_maps_full_rf_detail() -> None:
    links = _links()
    active = [link for link in links if link.is_active and link.signal_dbm is not None]
    assert active, "capture contains active wireless links with a link-level signal"
    link = active[0]
    assert link.id
    assert link.end_a.name and link.end_b.name
    assert link.frequency_mhz is not None
    assert link.distance_m is not None  # devicesDistance or sitesDistance fallback
    for end in (link.end_a, link.end_b):
        radio = end.radio
        assert radio is not None
        assert radio.status is not None
    # at least one end of an active link carries the linkScore quality sub-object
    assert any(
        end.radio is not None and end.radio.score is not None
        for link in active
        for end in (link.end_a, link.end_b)
    )


def test_down_link_maps_without_fabricating_values() -> None:
    links = _links()
    down = [link for link in links if not link.is_active]
    assert down, "off-season capture contains disconnected links"
    assert all(link.state == "disconnected" for link in down)
    # disconnected links generally carry no link-level signal — absence maps to None, no invention
    assert any(link.signal_dbm is None for link in down)


def test_join_miss_keeps_the_link_with_radio_none() -> None:
    raw_link = next(
        r
        for r in _FIXTURES["/nms/api/v2.1/data-links"]
        if str(r.get("type", "")).lower() == "wireless"
    )
    link = map_wireless_link(raw_link, {})  # no radios at all → both ends miss
    assert link is not None
    assert link.end_a.radio is None and link.end_b.radio is None
    assert link.end_a.name  # endpoint identity survives the miss


def test_totality_on_malformed_elements() -> None:
    assert map_wireless_link({}, {}) is None  # no id
    assert map_wireless_link({"id": "x", "type": "ethernet"}, {}) is None  # not wireless
    assert map_wireless_link({"id": "x"}, {}) is None  # missing type ≠ wireless
    junk = {
        "id": "x",
        "type": "WIRELESS",  # case-insensitive
        "state": 7,
        "signal": "not-a-number",
        "frequency": None,
        "from": "junk",
        "to": {"device": {"identification": {"name": ""}}},
    }
    link = map_wireless_link(junk, {})
    assert link is not None
    assert link.signal_dbm is None
    assert link.end_a.name is None and link.end_b.name is None
    assert map_radio_health({}) is None  # no identification.id
    keyed = map_radio_health({"identification": {"id": "d1"}, "overview": {"linkScore": "junk"}})
    assert keyed is not None
    device_id, radio = keyed
    assert device_id == "d1"
    assert radio.score is None and radio.signal_dbm is None


@pytest.mark.parametrize("fail_on", ["/nms/api/v2.1/data-links", "/nms/api/v2.1/devices"])
def test_controller_failure_on_either_read_raises_classified(fail_on: str) -> None:
    transport = RecordingGeoTransport(fail_on=fail_on)
    controller = UispController(uisp_settings(), geo_transport=transport)
    with pytest.raises(UispError) as excinfo:
        controller.links(timeout=5.0)
    assert excinfo.value.outcome is SourceOutcome.UNREACHABLE
