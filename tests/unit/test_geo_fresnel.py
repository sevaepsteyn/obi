"""Unit tests: frequency resolution + first-Fresnel-zone radius (025, T004).

Radius values are pinned to the hand-computed table in specs/025-ar-fresnel/research.md §2
(cross-checkable against any published Fresnel calculator — SC-001); the resolution matrix
mirrors contracts/ar-fresnel-core.md §Core, including the live-observed disagreeing device
pair (68040 vs 64800 MHz) that motivates the link-channel-wins rule.
"""

from __future__ import annotations

import math

import pytest

from obi.core.geo import build_geometry, fresnel_radius_m, resolve_frequency
from obi.core.models import (
    FrequencyProvenance,
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    LinkHealth,
)
from tests.fakes import flat_ground

_C = 299_792_458.0


def _device(name: str, *, site: str = "Site A", status: str | None = "active",
            lat: float = 40.78, lon: float = -119.21,
            frequency: float | None = None) -> GeoDeviceRecord:
    return GeoDeviceRecord(
        name=name, site=site, latitude=lat, longitude=lon, altitude=1200.0,
        status=status, link_score=0.9, frequency_mhz=frequency,
    )


def _link(a: str, b: str, *, state: str | None = "active",
          frequency: float | None = None) -> GeoLinkRecord:
    return GeoLinkRecord(
        id="L1", from_name=a, to_name=b, link_type="wireless", state=state,
        frequency_mhz=frequency,
    )


def _build(devices: list[GeoDeviceRecord], links: list[GeoLinkRecord]):
    sites = [GeoSiteRecord("Site A", 40.78, -119.21)]
    return build_geometry(
        GeometrySnapshot(devices=devices, links=links, sites=sites),
        controller="https://uisp.example.com",
        ground=flat_ground(1190.0),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
    )


# --- radius physics (research §2 hand-check table) ----------------------------------------------


@pytest.mark.parametrize(
    ("frequency_mhz", "length_m", "expected_m"),
    [
        (5500.0, 10_000.0, 11.67),
        (5590.0, 3_000.0, 6.34),
        (11245.0, 10_000.0, 8.16),
        (65880.0, 5_800.0, 2.57),
    ],
)
def test_fresnel_radius_matches_the_hand_computed_table(
    frequency_mhz: float, length_m: float, expected_m: float
) -> None:
    radius = fresnel_radius_m(length_m, frequency_mhz)
    assert radius == pytest.approx(expected_m, abs=0.01)  # well inside SC-001's ±2%
    # and against the formula symbolically, not just the table
    assert radius == pytest.approx(0.5 * math.sqrt((_C / (frequency_mhz * 1e6)) * length_m))


def test_fresnel_radius_scales_with_the_inverse_square_root_of_frequency() -> None:
    # 4× the frequency → half the radius (same span)
    assert fresnel_radius_m(8_000.0, 22_000.0) == pytest.approx(
        fresnel_radius_m(8_000.0, 5_500.0) / 2
    )


# --- frequency resolution matrix (contracts/ar-fresnel-core.md §Core) ---------------------------


def test_link_channel_wins_over_disagreeing_device_channels() -> None:
    # the live-observed pair: link says 68040 while the ends' cached values disagree
    link = _link("manzanita-ptp-noc-60", "noc-ptp-manzanita-60", frequency=68040.0)
    a = _device("manzanita-ptp-noc-60", frequency=68040.0)
    b = _device("noc-ptp-manzanita-60", frequency=64800.0)
    assert resolve_frequency(link, a, b) == (68040.0, FrequencyProvenance.LINK_CHANNEL)


def test_device_mean_when_the_link_reports_no_channel() -> None:
    link = _link("a-ptp-b-05", "b-ptp-a-05")
    a = _device("a-ptp-b-05", frequency=5500.0)
    b = _device("b-ptp-a-05", frequency=5510.0)
    assert resolve_frequency(link, a, b) == (5505.0, FrequencyProvenance.DEVICE_CHANNEL)


def test_single_device_channel_is_used_verbatim() -> None:
    link = _link("a-ptp-b-05", "b-ptp-a-05")
    a = _device("a-ptp-b-05", frequency=5590.0)
    b = _device("b-ptp-a-05")
    assert resolve_frequency(link, a, b) == (5590.0, FrequencyProvenance.DEVICE_CHANNEL)


@pytest.mark.parametrize(
    ("suffix", "nominal_mhz"),
    [("05", 5500.0), ("11", 11000.0), ("24", 24000.0), ("60", 60000.0), ("70", 73000.0)],
)
def test_band_nominal_when_no_channel_is_reported_anywhere(
    suffix: str, nominal_mhz: float
) -> None:
    link = _link(f"a-ptp-b-{suffix}", f"b-ptp-a-{suffix}")
    a = _device(f"a-ptp-b-{suffix}")
    b = _device(f"b-ptp-a-{suffix}")
    assert resolve_frequency(link, a, b) == (nominal_mhz, FrequencyProvenance.BAND_NOMINAL)


def test_unresolvable_when_no_channel_and_the_band_is_unknown() -> None:
    # mismatched band tokens → derive_band yields "unknown" → no nominal → None
    link = _link("a-ptp-b-05", "b-ptp-a-60")
    assert resolve_frequency(link, _device("a-ptp-b-05"), _device("b-ptp-a-60")) is None


def test_zero_and_negative_channels_are_treated_as_absent() -> None:
    link = _link("a-ptp-b-05", "b-ptp-a-05", frequency=0.0)
    a = _device("a-ptp-b-05", frequency=-5.0)
    b = _device("b-ptp-a-05", frequency=5490.0)
    # link's 0 is junk → tier 2; a's -5 is junk → b's value verbatim
    assert resolve_frequency(link, a, b) == (5490.0, FrequencyProvenance.DEVICE_CHANNEL)


# --- build_geometry wiring: the all-or-none ArLink trio (data-model invariants) ------------------


def test_exported_link_carries_the_consistent_zone_trio() -> None:
    devices = [
        _device("a-ptp-b-05", lat=40.78, lon=-114.82),
        _device("b-ptp-a-05", lat=40.80, lon=-114.79, site="Site B"),
    ]
    geometry = _build(devices, [_link("a-ptp-b-05", "b-ptp-a-05", frequency=5590.0)])
    (link,) = geometry.links
    assert link.frequency_ghz == pytest.approx(5.59)
    assert link.frequency_provenance is FrequencyProvenance.LINK_CHANNEL
    assert link.fresnel_radius_m is not None and link.fresnel_radius_m > 0
    # radius consistent with the exported length and frequency, to export rounding
    assert link.fresnel_radius_m == pytest.approx(
        fresnel_radius_m(link.length_m, 5590.0), abs=0.01
    )
    # rounding contract: GHz to 3 dp, radius to 2 dp
    assert link.frequency_ghz == round(link.frequency_ghz, 3)
    assert link.fresnel_radius_m == round(link.fresnel_radius_m, 2)


def test_zoneless_link_exports_the_all_none_trio() -> None:
    devices = [
        _device("a-ptp-b-xx", lat=40.78, lon=-114.82),
        _device("b-ptp-a-yy", lat=40.80, lon=-114.79, site="Site B"),
    ]
    geometry = _build(devices, [_link("a-ptp-b-xx", "b-ptp-a-yy")])
    (link,) = geometry.links
    assert link.frequency_ghz is None
    assert link.frequency_provenance is None
    assert link.fresnel_radius_m is None


def test_a_down_link_still_carries_its_zone() -> None:
    devices = [
        _device("a-ptp-b-60", lat=40.78, lon=-114.82, status="disconnected"),
        _device("b-ptp-a-60", lat=40.80, lon=-114.79, site="Site B", status="disconnected"),
    ]
    geometry = _build(
        devices, [_link("a-ptp-b-60", "b-ptp-a-60", state="disconnected", frequency=58320.0)]
    )
    (link,) = geometry.links
    assert link.health is LinkHealth.DOWN
    assert link.frequency_ghz == pytest.approx(58.32)
    assert link.fresnel_radius_m is not None and link.fresnel_radius_m > 0
