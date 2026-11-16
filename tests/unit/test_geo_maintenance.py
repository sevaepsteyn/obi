"""Unit tests: maintenance-mode capture + projection (feature 044 US3, research R1).

Mapper rule: ``meta.maintenance`` maps through as a strict boolean — absent or malformed
meta is ``False`` (a guessed maintenance flag must never mask a real outage). Projection:
a link is in maintenance when EITHER endpoint device is flagged; every exported endpoint
carries its own flag (dot color).
"""

from __future__ import annotations

from typing import Any

from obi.core.drivers.uisp import map_geo_device
from obi.core.geo import build_geometry
from obi.core.models import (
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
)
from tests.fakes import flat_ground


def _raw_device(meta: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "identification": {"name": "a-ptp-b-60", "site": {"name": "Site A"}},
        "location": {"latitude": 40.78, "longitude": -114.82, "altitude": 1200.0},
        "overview": {"status": "active"},
    }
    if meta is not ...:
        raw["meta"] = meta
    return raw


def test_mapper_captures_the_maintenance_flag() -> None:
    flagged = map_geo_device(
        _raw_device({"maintenance": True, "maintenanceEnabledAt": "2025-06-06T08:18:23Z"})
    )
    clear = map_geo_device(_raw_device({"maintenance": False}))
    assert flagged is not None and flagged.maintenance is True
    assert clear is not None and clear.maintenance is False


def test_mapper_defaults_missing_or_malformed_meta_to_false() -> None:
    # strict boolean-True only: absent meta, junk meta, and truthy non-booleans all read False
    for meta in (..., None, {}, {"maintenance": None}, "junk", {"maintenance": "yes"}):
        device = map_geo_device(_raw_device(meta))
        assert device is not None
        assert device.maintenance is False, meta


def _device(name: str, site: str, *, maintenance: bool = False) -> GeoDeviceRecord:
    lat, lon = (40.78, -119.21) if site == "Site A" else (40.80, -119.19)
    return GeoDeviceRecord(
        name=name, site=site, latitude=lat, longitude=lon, altitude=1200.0,
        status="active", link_score=0.9, maintenance=maintenance,
    )


def _geometry(a_flagged: bool, b_flagged: bool):
    a = _device("a-ptp-b-60", "Site A", maintenance=a_flagged)
    b = _device("b-ptp-a-60", "Site B", maintenance=b_flagged)
    link = GeoLinkRecord(id="L1", from_name=a.name, to_name=b.name,
                         link_type="wireless", state="active")
    return build_geometry(
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


def test_either_flagged_end_puts_the_link_in_maintenance() -> None:
    for a_flagged, b_flagged in ((True, False), (False, True), (True, True)):
        [link] = _geometry(a_flagged, b_flagged).links
        assert link.maintenance is True, (a_flagged, b_flagged)
    [link] = _geometry(False, False).links
    assert link.maintenance is False


def test_every_exported_endpoint_carries_its_own_flag() -> None:
    geometry = _geometry(True, False)
    flags = {endpoint.name: endpoint.maintenance for endpoint in geometry.endpoints}
    assert flags == {"a-ptp-b-60": True, "b-ptp-a-60": False}
