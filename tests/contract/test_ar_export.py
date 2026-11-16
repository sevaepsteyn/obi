"""Contract tests: the geometry export (022 T016, revised by 025, 031, and 044).

The NORMATIVE schema moved forward with feature 097 (specs/097-site-towers/contracts/);
the 022, 025, 031, 044, and 083 copies remain untouched as ship-time historical artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from obi.core.audit import AuditTrail
from obi.core.config import WebSettings
from obi.core.drivers.uisp import UispError
from obi.core.engine import ar_export
from obi.core.models import GroundProvenance, SourceOutcome
from tests.fakes import geometry_snapshot

SCHEMA_PATH = (
    Path(__file__).parents[2]
    / "tests" / "schemas" / "ar-export.schema.json"
)


class FakeGeometrySource:
    def __init__(self, *, error: UispError | None = None) -> None:
        self.error = error

    def geometry(self, *, timeout: float) -> Any:
        if self.error is not None:
            raise self.error
        return geometry_snapshot()


class FakeElevation:
    def ground(self, site_name: str, lat: float, lon: float) -> tuple[float, GroundProvenance]:
        if site_name == "Basswood Relay - East":
            return 1370.0, GroundProvenance.OVERRIDE
        return 1190.66, GroundProvenance.CACHE


def _export(tmp_path: Path, *, source: FakeGeometrySource | None = None):
    out_path = tmp_path / "links.json"
    geometry = ar_export(
        source or FakeGeometrySource(),  # type: ignore[arg-type]
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(),
        elevation=FakeElevation(),  # type: ignore[arg-type]
        out_path=out_path,
        timeout=5.0,
    )
    return geometry, out_path


def test_export_validates_against_the_normative_schema(tmp_path: Path) -> None:
    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)  # raises on any shape drift
    assert payload["controller"] == "https://uisp.example.com"
    assert payload["links"], "the live fixture must export links"
    # 079: every endpoint carries a location provenance (present, enumerated) — never missing.
    assert payload["endpoints"]
    assert all(e["location_provenance"] in ("device", "site") for e in payload["endpoints"])


def test_export_writes_one_ok_audit_record(tmp_path: Path) -> None:
    _geometry, _out = _export(tmp_path)
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["action"] == "ar_export"
    assert records[0]["outcome"] == "ok"
    assert records[0]["params"]["links"]["total"] > 0


def test_controller_failure_preserves_the_prior_export_and_audits(tmp_path: Path) -> None:
    _geometry, out_path = _export(tmp_path)
    before = out_path.read_bytes()

    failing = FakeGeometrySource(
        error=UispError(SourceOutcome.UNREACHABLE, "controller went away")
    )
    with pytest.raises(UispError):
        _export(tmp_path, source=failing)

    assert out_path.read_bytes() == before  # FR-008: prior export untouched
    assert not out_path.with_name(out_path.name + ".tmp").exists()
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in records] == ["ok", "unreachable"]


def test_no_credential_material_in_the_export(tmp_path: Path) -> None:
    """The export pipeline never sees the token; defense-in-depth: assert on the artifact."""

    _geometry, out_path = _export(tmp_path)
    content = out_path.read_text().lower()
    for needle in ("token", "password", "x-auth", "secret"):
        assert needle not in content


def test_exclusions_and_degradations_are_reported_not_silent(tmp_path: Path) -> None:
    geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    # The recorded fleet is fully located, so exclusions may be empty — but the keys MUST
    # exist (the viewer and the CLI summary rely on them), and any entry must carry a reason.
    assert "excluded" in payload and "degraded" in payload
    for item in payload["excluded"]:
        assert item["link"] and item["reason"]


def test_every_fixture_link_carries_the_zone_trio(tmp_path: Path) -> None:
    """025: the live capture resolves every backhaul's channel from the link record."""

    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    assert payload["links"]
    for link in payload["links"]:
        assert link["frequency_ghz"] is not None
        assert link["frequency_provenance"] == "link_channel"
        assert link["fresnel_radius_m"] is not None and link["fresnel_radius_m"] > 0


def test_zoneless_link_serializes_explicit_nulls_and_validates(tmp_path: Path) -> None:
    """025: a link with no resolvable frequency carries explicit nulls — keys never missing."""

    snapshot = geometry_snapshot()
    # doctor one backhaul: strip every frequency source and un-derive its band
    target = next(
        link for link in snapshot.links
        if link.from_name and "-ptp-" in link.from_name and link.link_type == "wireless"
    )
    target.frequency_mhz = None
    by_name = {d.name: d for d in snapshot.devices}
    for name in (target.from_name, target.to_name):
        device = by_name[str(name)]
        device.frequency_mhz = None
        device.name = device.name + "-zz"  # unmapped band token → no nominal
    renamed = {str(target.from_name) + "-zz", str(target.to_name) + "-zz"}
    target.from_name = str(target.from_name) + "-zz"
    target.to_name = str(target.to_name) + "-zz"

    class DoctoredSource:
        def geometry(self, *, timeout: float) -> Any:
            return snapshot

    _geometry, out_path = _export(tmp_path, source=DoctoredSource())  # type: ignore[arg-type]
    payload = json.loads(out_path.read_text())
    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text()))  # nulls are schema-legal
    zoneless = [link for link in payload["links"] if {link["a"], link["b"]} == renamed]
    assert len(zoneless) == 1
    assert zoneless[0]["frequency_ghz"] is None
    assert zoneless[0]["frequency_provenance"] is None
    assert zoneless[0]["fresnel_radius_m"] is None


def test_without_a_terrain_source_the_clearance_quadruple_is_null_and_named(
    tmp_path: Path,
) -> None:
    """031: no terrain source ⇒ explicit nulls (keys never missing) + a named omission."""

    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text()))
    for link in payload["links"]:
        assert link["clearance_verdict"] is None
        assert link["clearance_margin_m"] is None
        assert link["clearance_at_fraction"] is None
        assert link["clearance_basis"] is None
    assert payload["terrain_omitted"], "the missing terrain source must be named"
    assert all(entry["item"] and entry["reason"] for entry in payload["terrain_omitted"])


def test_with_terrain_every_link_carries_a_full_quadruple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obi.core import terrain as terrain_mod
    from obi.core.terrain import TerrainTileSource
    from tests.fixtures.ar.terrarium_helpers import flat_tile_bytes

    # 055: coarsen the shade raster (its shader is pinned at full res in test_terrain_shade.py);
    # this test's subject is the clearance quadruple, not the underlay.
    monkeypatch.setattr(terrain_mod, "SHADE_STEP_M", 4000.0)
    tile = flat_tile_bytes(1200.0)
    out_path = tmp_path / "links.json"
    geometry = ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(),
        elevation=FakeElevation(),  # type: ignore[arg-type]
        out_path=out_path,
        terrain=TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile),
    )
    payload = json.loads(out_path.read_text())
    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text()))
    assert payload["links"]
    for link in payload["links"]:
        assert link["clearance_verdict"] in ("clear", "marginal", "obstructed")
        assert isinstance(link["clearance_margin_m"], (int, float))
        assert 0.0 <= link["clearance_at_fraction"] <= 1.0
        assert link["clearance_basis"] == "fresnel"  # fixture links all resolve a channel
    # Verdicts never touch health (FR-006): identical to the terrain-less export.
    plain = [link.health for link in _export(tmp_path / "plain")[0].links]
    assert [link.health for link in geometry.links] == plain


def test_capacity_usage_and_source_ride_the_links(tmp_path: Path) -> None:
    """044 US2/US4: capacity/usage in int bps with explicit nulls when the controller has
    no numbers (off-season radios), and every UISP link carries the vendor source marker."""

    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    links = payload["links"]
    assert links and all(link["source"] == "uisp" for link in links)
    carrying = [link for link in links if link["capacity_down_bps"] is not None]
    assert carrying, "the augmented fixture must yield capacity on its active links"
    for link in carrying:
        assert link["capacity_down_bps"] > 0
        if link["usage_down_bps"] is not None:
            assert link["usage_down_bps"] >= 0
    down = [link for link in links if link["health"] == "down"]
    assert any(link["capacity_down_bps"] is None for link in down), (
        "a dark radio exports explicit nulls, never fabricated numbers"
    )


def test_maintenance_rides_links_and_endpoints(tmp_path: Path) -> None:
    """044 US3: the fixture fleet is off-season — flagged devices must surface as
    maintenance links/endpoints, and the flag never alters health."""

    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    flagged_links = [link for link in payload["links"] if link["maintenance"]]
    flagged_endpoints = [e for e in payload["endpoints"] if e["maintenance"]]
    assert flagged_links, "the augmented fixture carries maintenance-flagged PtP radios"
    assert flagged_endpoints
    healths = {link["health"] for link in payload["links"]}
    assert "maintenance" not in healths  # a flag, not a health state


# --- 079: site-anchored endpoint locations ------------------------------------------------------

# The recorded capture already contains the real strayed pin: basswood-ptp-yew-05 sits ~169.5 m
# north of its site "Basswood Relay - West" (an old copy-at-placement value). It is the ONLY
# endpoint beyond the default 75 m radius fleet-wide, so the export must name exactly one
# correction and place that endpoint at the site pin.
_MOUNTAIN_WEST_PIN = (40.768823, -114.885009)


def test_site_anchoring_names_the_one_strayed_endpoint(tmp_path: Path) -> None:
    """079 SC-001/SC-003: exactly one correction on the recorded fleet, named with both pins,
    the endpoint relocated to its site pin, and the count recorded in the single audit entry."""

    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())

    assert len(payload["site_anchored"]) == 1
    anchor = payload["site_anchored"][0]
    assert anchor["device"] == "basswood-ptp-yew-05"
    assert anchor["site"] == "Basswood Relay - West"
    assert anchor["distance_m"] == pytest.approx(169.5, abs=1.0)
    assert (anchor["site_latitude"], anchor["site_longitude"]) == _MOUNTAIN_WEST_PIN
    # the device pin it moved FROM is the stale 4-decimal copy, distinct from the site pin
    assert (anchor["device_latitude"], anchor["device_longitude"]) != _MOUNTAIN_WEST_PIN

    # the exported endpoint now sits at the site pin, marked "site"; a normal one stays "device"
    endpoints = {e["name"]: e for e in payload["endpoints"]}
    moved = endpoints["basswood-ptp-yew-05"]
    assert moved["location_provenance"] == "site"
    assert (moved["latitude"], moved["longitude"]) == _MOUNTAIN_WEST_PIN
    assert any(e["location_provenance"] == "device" for e in payload["endpoints"])

    # 079 FR-009: the single ar_export audit record carries the site-anchored count.
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["params"]["site_anchored"] == 1


def test_site_anchoring_is_horizontal_only(tmp_path: Path) -> None:
    """079 FR-011: a correction moves an endpoint horizontally; its height fields are unchanged
    from what the mixed-datum heuristic resolves — the vertical pipeline is untouched."""

    geometry, _out = _export(tmp_path)
    from obi.core.geo import resolve_height

    moved = next(e for e in geometry.endpoints if e.name == "basswood-ptp-yew-05")
    site = next(s for s in geometry.sites if s.name == moved.site)
    height, provenance = resolve_height(
        moved.raw_altitude, site.ground_elevation_m,
        amsl_tolerance=WebSettings().amsl_tolerance,
        default_mast_height=WebSettings().default_mast_height,
    )
    assert moved.height_amsl_m == round(height, 2)
    assert moved.height_provenance is provenance


def test_untowered_export_carries_empty_tower_keys(tmp_path: Path) -> None:
    """097: the keys are always present (025 discipline) — empty, never absent."""

    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    assert payload["tower_placed"] == []
    assert payload["tower_unmatched"] == []


def test_towered_export_collapses_the_site_and_names_every_move(tmp_path: Path) -> None:
    """097 SC-001/SC-004/SC-005: with a tower declared on Basswood Relay - West, every
    endpoint of that site exports at ONE point (the site pin, ground + tower) with ``tower``
    provenance; each move is a named ``tower_placed`` record (superseding the fleet's one 079
    anchor — same site — so ``site_anchored`` empties); a typo'd name lands in
    ``tower_unmatched``; and the payload validates against the normative schema."""

    out_path = tmp_path / "links.json"
    ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(site_towers={"Basswood Relay - West": 2.0, "Sight A": 5.0}),
        elevation=FakeElevation(),  # type: ignore[arg-type]
        out_path=out_path,
        timeout=5.0,
    )
    payload = json.loads(out_path.read_text())
    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text()))

    site = next(s for s in payload["sites"] if s["name"] == "Basswood Relay - West")
    towered = [e for e in payload["endpoints"] if e["site"] == "Basswood Relay - West"]
    assert towered, "the recorded fleet must have Basswood Relay - West endpoints"
    for endpoint in towered:
        assert (endpoint["latitude"], endpoint["longitude"]) == _MOUNTAIN_WEST_PIN
        assert endpoint["height_amsl_m"] == pytest.approx(site["ground_elevation_m"] + 2.0)
        assert endpoint["location_provenance"] == "tower"
        assert endpoint["height_provenance"] == "tower"

    assert sorted(p["device"] for p in payload["tower_placed"]) == sorted(
        e["name"] for e in towered
    )
    # the fleet's single 079 anchor (basswood-ptp-yew-05, same site) is superseded, not doubled
    assert payload["site_anchored"] == []
    assert any(p["device"] == "basswood-ptp-yew-05" for p in payload["tower_placed"])
    assert payload["tower_unmatched"] == ["Sight A"]

    # the single ar_export audit record carries both counts (Principle V)
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["params"]["tower_placed"] == len(towered)
    assert records[0]["params"]["tower_unmatched"] == ["Sight A"]


def test_pre_079_payload_still_validates_against_the_044_schema(tmp_path: Path) -> None:
    """079 FR-010/SC-004: the additions are purely additive. Strip the new keys from a fresh
    export and it validates against the previous (044) normative schema — so a pre-079 export
    (which never had them) is unaffected, and old readers keep working."""

    schema_044 = json.loads(
        (SCHEMA_PATH.parent / "ar-export-044.schema.json")
        .read_text()
    )
    _geometry, out_path = _export(tmp_path)
    payload = json.loads(out_path.read_text())
    payload.pop("site_anchored")
    # 097: the tower keys are additive in exactly the same way.
    payload.pop("tower_placed")
    payload.pop("tower_unmatched")
    for endpoint in payload["endpoints"]:
        endpoint.pop("location_provenance")
        # 083: ground_floored is the only post-044 enum value; a pre-083 export resolved the
        # same endpoint via the symmetric rule-1 band, so map it back for the historical shape.
        if endpoint["height_provenance"] == "ground_floored":
            endpoint["height_provenance"] = "default_mast"
    jsonschema.validate(payload, schema_044)  # additive-only: pre-079 shape stays valid
