"""Unit tests: capacity/usage capture + projection (feature 044 US2, data-model §ArLink).

Mapper rules (drivers/uisp.map_geo_device): the overview sextet maps through verbatim,
absent/None stays None — the mapper never invents a number. Projection rules (core/geo):
per direction, end A's capacity wins, else end B's; usage derives from the SAME end that
supplied the capacity, only when both factors exist; junk utilization (outside [0, 1.5])
is discarded, never exported.
"""

from __future__ import annotations

from typing import Any

from obi.core.drivers.uisp import map_geo_device
from obi.core.geo import build_geometry, link_capacity
from obi.core.models import (
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
)
from tests.fakes import flat_ground


def _raw_device(overview: dict[str, Any]) -> dict[str, Any]:
    return {
        "identification": {"name": "a-ptp-b-60", "site": {"name": "Site A"}},
        "location": {"latitude": 40.78, "longitude": -114.82, "altitude": 1200.0},
        "overview": {"status": "active", **overview},
    }


# --- mapper capture (T009) ----------------------------------------------------------------------


def test_mapper_captures_the_capacity_sextet() -> None:
    device = map_geo_device(
        _raw_device(
            {
                "downlinkCapacity": 2156000000,
                "uplinkCapacity": 1751750000,
                "theoreticalDownlinkCapacity": 2700000000,
                "theoreticalUplinkCapacity": 2700000000,
                "downlinkUtilization": 0.0009650018,
                "uplinkUtilization": 0.022415888,
            }
        )
    )
    assert device is not None
    assert device.capacity_down_bps == 2156000000
    assert device.capacity_up_bps == 1751750000
    assert device.capacity_down_theoretical_bps == 2700000000
    assert device.capacity_up_theoretical_bps == 2700000000
    assert device.utilization_down == 0.0009650018
    assert device.utilization_up == 0.022415888


def test_mapper_maps_explicit_nulls_and_absent_keys_to_none() -> None:
    for overview in (
        {  # disconnected radio: the controller reports explicit nulls (fixture-verified)
            "downlinkCapacity": None,
            "uplinkCapacity": None,
            "theoreticalDownlinkCapacity": None,
            "theoreticalUplinkCapacity": None,
            "downlinkUtilization": None,
            "uplinkUtilization": None,
        },
        {},  # pre-044 capture: the keys are entirely absent
    ):
        device = map_geo_device(_raw_device(overview))
        assert device is not None
        assert device.capacity_down_bps is None
        assert device.capacity_up_bps is None
        assert device.capacity_down_theoretical_bps is None
        assert device.capacity_up_theoretical_bps is None
        assert device.utilization_down is None
        assert device.utilization_up is None


# --- projection (T010) ----------------------------------------------------------------------------


def _end(
    name: str = "a-ptp-b-60",
    *,
    cap_down: int | None = None,
    cap_up: int | None = None,
    util_down: float | None = None,
    util_up: float | None = None,
) -> GeoDeviceRecord:
    return GeoDeviceRecord(
        name=name,
        site="Site A",
        latitude=40.78,
        longitude=-114.82,
        altitude=1200.0,
        status="active",
        link_score=0.9,
        capacity_down_bps=cap_down,
        capacity_up_bps=cap_up,
        utilization_down=util_down,
        utilization_up=util_up,
    )


def test_end_a_wins_per_direction_and_usage_follows_the_same_end() -> None:
    a = _end(cap_down=1_000_000_000, util_down=0.25)  # no up-direction data at all
    b = _end("b-ptp-a-60", cap_down=999, cap_up=500_000_000, util_down=0.9, util_up=0.5)
    cap_down, usage_down, cap_up, usage_up = link_capacity(a, b)
    assert (cap_down, usage_down) == (1_000_000_000, 250_000_000)  # A's, never B's 999
    assert (cap_up, usage_up) == (500_000_000, 250_000_000)  # up falls back to B


def test_usage_requires_both_factors_from_the_same_end() -> None:
    # A supplies down capacity but no utilization; B has utilization — NOT mixed in.
    a = _end(cap_down=1_000_000_000)
    b = _end("b-ptp-a-60", util_down=0.5)
    cap_down, usage_down, cap_up, usage_up = link_capacity(a, b)
    assert cap_down == 1_000_000_000
    assert usage_down is None
    assert cap_up is None and usage_up is None


def test_junk_utilization_is_discarded() -> None:
    a = _end(cap_down=100, util_down=7.3)  # >1.5 = junk → no usage, capacity kept
    cap_down, usage_down, _, _ = link_capacity(a, _end("b-ptp-a-60"))
    assert cap_down == 100
    assert usage_down is None


def test_zero_or_negative_capacity_is_treated_as_absent() -> None:
    a = _end(cap_down=0, util_down=0.5)
    b = _end("b-ptp-a-60", cap_down=200_000_000, util_down=0.1)
    cap_down, usage_down, _, _ = link_capacity(a, b)
    assert (cap_down, usage_down) == (200_000_000, 20_000_000)


def test_projection_reaches_the_exported_link() -> None:
    a = _end("a-ptp-b-60", cap_down=2_000_000_000, cap_up=1_500_000_000,
             util_down=0.1, util_up=0.02)
    b = GeoDeviceRecord(
        name="b-ptp-a-60", site="Site B", latitude=40.80, longitude=-114.80,
        altitude=1210.0, status="active", link_score=0.9,
    )
    link = GeoLinkRecord(id="L1", from_name=a.name, to_name=b.name,
                         link_type="wireless", state="active")
    geometry = build_geometry(
        GeometrySnapshot(devices=[a, b], links=[link], sites=[
            GeoSiteRecord(name="Site A", latitude=40.78, longitude=-114.82),
            GeoSiteRecord(name="Site B", latitude=40.80, longitude=-114.80),
        ]),
        controller="https://uisp.example.com",
        ground=flat_ground(1190.0),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
    )
    [exported] = geometry.links
    assert exported.capacity_down_bps == 2_000_000_000
    assert exported.capacity_up_bps == 1_500_000_000
    assert exported.usage_down_bps == 200_000_000
    assert exported.usage_up_bps == 30_000_000
