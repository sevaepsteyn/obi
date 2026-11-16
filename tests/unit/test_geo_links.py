"""Unit tests: PtP selection, band derivation, health mapping, exclusions (022, T009)."""

from __future__ import annotations

import pytest

from obi.core.geo import build_geometry, derive_band, is_ptp_name, link_health
from obi.core.models import (
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    LinkHealth,
)
from tests.fakes import flat_ground, geometry_snapshot


def _device(name: str, *, site: str = "Site A", status: str | None = "active",
            score: float | None = 0.9, lat: float | None = 40.78,
            lon: float | None = -119.21, alt: float | None = 1200.0) -> GeoDeviceRecord:
    return GeoDeviceRecord(
        name=name, site=site, latitude=lat, longitude=lon, altitude=alt,
        status=status, link_score=score,
    )


def _link(a: str, b: str, *, link_type: str = "wireless", state: str | None = "active",
          link_id: str = "L1") -> GeoLinkRecord:
    return GeoLinkRecord(id=link_id, from_name=a, to_name=b, link_type=link_type, state=state)


def _build(devices: list[GeoDeviceRecord], links: list[GeoLinkRecord],
           sites: list[GeoSiteRecord] | None = None, *, degraded_link_score: float = 0.5):
    return build_geometry(
        GeometrySnapshot(devices=devices, links=links, sites=sites or []),
        controller="https://uisp.example.com",
        ground=flat_ground(1190.0),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=degraded_link_score,
    )


# --- selection ---------------------------------------------------------------------------------


def test_is_ptp_name() -> None:
    assert is_ptp_name("noc-ptp-basswood-60")
    assert is_ptp_name("rowan-PTP-joshua-ap-05")  # case-insensitive
    assert not is_ptp_name("chestnut-ptmp-180-05")
    assert not is_ptp_name("chestnut-st-autoshop-05")
    assert not is_ptp_name(None)
    assert not is_ptp_name("")


def test_selection_requires_wireless_and_both_ptp_names() -> None:
    a = _device("sitea-ptp-siteb-60")
    b = _device("siteb-ptp-sitea-60", site="Site B")
    station = _device("siteb-st-house-05", site="Site B")
    geometry = _build(
        [a, b, station],
        [
            _link(a.name, b.name, link_id="keep"),
            _link(a.name, station.name, link_id="ptmp"),  # one end not ptp → ignored
            _link(a.name, b.name, link_type="ethernet", link_id="wired"),  # not wireless
        ],
    )
    assert [link.id for link in geometry.links] == ["keep"]
    assert geometry.excluded == []  # PtMP / wired links are not "excluded", just not candidates


def test_selection_on_the_real_fixture_matches_the_naming_convention() -> None:
    snapshot = geometry_snapshot()
    geometry = _build(snapshot.devices, snapshot.links, snapshot.sites)
    assert geometry.links, "the live fixture must yield backhaul links"
    for link in geometry.links:
        assert "-ptp-" in link.a and "-ptp-" in link.b
    # PtMP station links (station names carry -st- WITHOUT -ptp-, e.g. chestnut-st-autoshop-05)
    # exist in the fixture and none may leak through. (rowan-ptp-joshua-st-05 is a
    # legitimate backhaul end — its name carries the -ptp- marker.)
    def is_station(name: str | None) -> bool:
        return bool(name) and "-st-" in str(name) and "-ptp-" not in str(name)

    fixture_names = {(li.from_name, li.to_name) for li in snapshot.links}
    assert any(is_station(a) or is_station(b) for a, b in fixture_names)
    assert all(not is_station(link.a) and not is_station(link.b) for link in geometry.links)


# --- site selection (057: only sites that anchor exported geometry) -----------------------------


def test_sites_without_exported_endpoints_are_dropped() -> None:
    """057 revised the 022 "every snapshot site" rule: a controller site hosting no exported
    link endpoint (the SF offices live, 404 km from the playa) draws nothing on any surface
    but cost two full terrain grids and dragged the fleet centroid. Exactly the
    endpoint-referenced sites ship; they reappear the moment they host a real backhaul."""

    a = _device("sitea-ptp-siteb-60")
    b = _device("siteb-ptp-sitea-60", site="Site B")
    geometry = _build(
        [a, b],
        [_link(a.name, b.name)],
        sites=[
            GeoSiteRecord(name="Site A", latitude=40.78, longitude=-114.82),
            GeoSiteRecord(name="Site B", latitude=40.79, longitude=-114.83),
            GeoSiteRecord(name="SF - Office", latitude=37.76, longitude=-122.41),
        ],
    )
    assert [link.id for link in geometry.links] == ["L1"]  # links untouched by the site rule
    assert {site.name for site in geometry.sites} == {"Site A", "Site B"}


# --- exclusions (FR-009: named, with reasons — never silent) ------------------------------------


def test_unknown_endpoint_device_is_excluded_with_reason() -> None:
    a = _device("sitea-ptp-siteb-60")
    geometry = _build([a], [_link(a.name, "ghost-ptp-sitea-60")])
    assert geometry.links == []
    assert len(geometry.excluded) == 1
    assert "ghost-ptp-sitea-60" in geometry.excluded[0].reason
    assert "not in the controller device list" in geometry.excluded[0].reason


def test_endpoint_without_coordinates_is_excluded_with_reason() -> None:
    a = _device("sitea-ptp-siteb-60")
    b = _device("siteb-ptp-sitea-60", site="Site B", lat=None, lon=None)
    geometry = _build([a, b], [_link(a.name, b.name)])
    assert geometry.links == []
    assert "no coordinates" in geometry.excluded[0].reason


def test_endpoint_without_site_is_excluded_with_reason() -> None:
    a = _device("sitea-ptp-siteb-60")
    b = _device("siteb-ptp-sitea-60", site=None)
    b.site = None
    geometry = _build([a, b], [_link(a.name, b.name)])
    assert geometry.links == []
    assert "no site" in geometry.excluded[0].reason


def test_empty_result_is_valid_not_an_error() -> None:
    geometry = _build([], [])
    assert geometry.links == [] and geometry.endpoints == [] and geometry.excluded == []


# --- band --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name_a", "name_b", "expected"),
    [
        ("noc-ptp-basswood-60", "basswood-ptp-noc-60", "60 GHz"),
        ("alder-ptp-manzanita-11", "manzanita-ptp-alder-11", "11 GHz"),
        ("zelkova-ptp-chestnut-05", "chestnut-ptp-zelkova-05", "5 GHz"),
        ("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70", "70 GHz"),  # e-band (044)
        ("a-ptp-b-60", "b-ptp-a-05", "unknown"),  # ends disagree
        ("a-ptp-b-77", "b-ptp-a-77", "77"),  # unmapped token passes through
    ],
)
def test_derive_band(name_a: str, name_b: str, expected: str) -> None:
    assert derive_band(name_a, name_b) == expected


# --- health (research §2; min-of-both-endpoints rule from the analyze remediation) --------------


def _health(*, state: str | None = "active", status_a: str | None = "active",
            status_b: str | None = "active", score_a: float | None = 0.9,
            score_b: float | None = 0.9, threshold: float = 0.5) -> LinkHealth:
    return link_health(
        GeoLinkRecord(id="L", state=state),
        _device("a-ptp-b-60", status=status_a, score=score_a),
        _device("b-ptp-a-60", status=status_b, score=score_b),
        degraded_link_score=threshold,
    )


def test_health_matrix() -> None:
    assert _health() is LinkHealth.HEALTHY
    # down: link state not active, or either device disconnected
    assert _health(state="disconnected") is LinkHealth.DOWN
    assert _health(status_a="disconnected") is LinkHealth.DOWN
    assert _health(status_b="disconnected") is LinkHealth.DOWN
    # degraded: the WORSE endpoint governs — a one-sided low score still degrades
    assert _health(score_a=0.2) is LinkHealth.DEGRADED
    assert _health(score_b=0.2) is LinkHealth.DEGRADED
    assert _health(score_a=0.2, score_b=0.9) is LinkHealth.DEGRADED
    # missing scores never degrade (absent ≠ zero)
    assert _health(score_a=None, score_b=None) is LinkHealth.HEALTHY
    assert _health(score_a=None, score_b=0.9) is LinkHealth.HEALTHY
    # unknown: required status fields absent
    assert _health(state=None) is LinkHealth.UNKNOWN
    assert _health(status_a=None) is LinkHealth.UNKNOWN
    # threshold is exclusive at the boundary
    assert _health(score_a=0.5) is LinkHealth.HEALTHY
    assert _health(score_a=0.499) is LinkHealth.DEGRADED


def test_fixture_contains_a_real_down_link() -> None:
    """The live capture (off-season) includes down backhauls — colors have real data."""

    snapshot = geometry_snapshot()
    geometry = _build(snapshot.devices, snapshot.links, snapshot.sites)
    healths = {link.health for link in geometry.links}
    assert LinkHealth.DOWN in healths
