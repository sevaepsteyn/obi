"""Integration: `obi web export` end-to-end on the recorded fixtures (feature 022, T019)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers import uisp as uisp_mod
from obi.core.drivers.uisp import UispError
from obi.core.models import SourceOutcome
from tests.fakes import load_uisp_geometry
from tests.integration._uisp import write_uisp_config

runner = CliRunner()

_FIXTURES = {
    "/nms/api/v2.1/devices": load_uisp_geometry("devices"),
    "/nms/api/v2.1/data-links": load_uisp_geometry("data-links"),
    "/nms/api/v2.1/sites": load_uisp_geometry("sites"),
}


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fixture-served controller, offline EPQS, and state isolated under tmp."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def geo_transport(self: object, path: str, timeout: float) -> list[dict[str, Any]]:
        return _FIXTURES[path]

    monkeypatch.setattr(uisp_mod.UispController, "_default_geo_transport", geo_transport)

    from obi.core import elevation as elevation_mod
    from obi.core import gnis as gnis_mod
    from obi.core import terrain as terrain_mod

    def offline_fetcher(timeout: float):
        def fetch(lat: float, lon: float) -> float | None:
            return 1190.66  # deterministic "EPQS": the playa everywhere

        return fetch

    monkeypatch.setattr(elevation_mod, "_default_fetcher", offline_fetcher)

    # 031: the CLI wires real terrain/GNIS sources — keep tests off the network. The
    # default here is "no terrain data" (fast: every build degrades to a named omission);
    # terrain-specific tests below install their own tile fetcher.
    def no_tiles(timeout: float):
        def fetch(z: int, x: int, y: int) -> bytes | None:
            return None

        return fetch

    def no_gnis(timeout: float):
        def fetch(state: str) -> bytes | None:
            return None

        return fetch

    monkeypatch.setattr(terrain_mod, "_default_tile_fetcher", no_tiles)
    monkeypatch.setattr(gnis_mod, "_default_fetcher", no_gnis)
    return tmp_path


def test_export_writes_summary_and_file(offline: Path, tmp_path: Path) -> None:
    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    result = runner.invoke(app, ["web", "export", "--scope", str(config), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "exported" in result.output and "links" in result.output
    payload = json.loads(out.read_text())
    assert payload["links"] and payload["sites"] and payload["endpoints"]
    # the offseason fixture has down links → the summary must mention them
    assert "down" in result.output


def test_export_json_summary(offline: Path, tmp_path: Path) -> None:
    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    result = runner.invoke(
        app, ["web", "export", "--scope", str(config), "--out", str(out), "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["export_path"] == str(out)
    assert summary["links"]["total"] == len(json.loads(out.read_text())["links"])
    assert isinstance(summary["excluded"], list)
    assert isinstance(summary["degraded"], list)


def test_export_bakes_the_elevation_cache_durably(offline: Path, tmp_path: Path) -> None:
    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    assert runner.invoke(
        app, ["web", "export", "--scope", str(config), "--out", str(out)]
    ).exit_code == 0
    cache = offline / "state" / "obi" / "ar" / "elevations.json"
    assert cache.exists()
    # One entry per exported site (plus endpoint lookups). 057 ships only sites referenced
    # by an exported link endpoint, so the controller's endpoint-less entries (the SF
    # offices live) no longer bake ground lookups.
    exported_sites = json.loads(out.read_text())["sites"]
    assert len(exported_sites) > 0
    assert len(json.loads(cache.read_text())) >= len(exported_sites)


def test_controller_failure_exits_1_and_preserves_prior_export(
    offline: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    assert runner.invoke(
        app, ["web", "export", "--scope", str(config), "--out", str(out)]
    ).exit_code == 0
    before = out.read_bytes()

    def failing(self: object, path: str, timeout: float) -> list[dict[str, Any]]:
        raise UispError(SourceOutcome.UNREACHABLE, "controller went away")

    monkeypatch.setattr(uisp_mod.UispController, "_default_geo_transport", failing)
    result = runner.invoke(app, ["web", "export", "--scope", str(config), "--out", str(out)])
    assert result.exit_code == 1
    assert "previous export untouched" in result.output
    assert out.read_bytes() == before


def test_export_without_uisp_section_exits_2(offline: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[logs]\n")
    config.chmod(0o600)
    result = runner.invoke(app, ["web", "export", "--scope", str(config)])
    assert result.exit_code == 2
    assert "uisp" in result.output.lower()


# --- 025: Fresnel zone geometry in the export & summary ------------------------------------------


def test_every_backhaul_gets_a_zone_from_its_link_channel(offline: Path, tmp_path: Path) -> None:
    """Full fixture: 18/18 links zoned, link_channel provenance, no zoneless warnings."""

    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    result = runner.invoke(
        app, ["web", "export", "--scope", str(config), "--out", str(out), "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["no_zone"] == []
    links = json.loads(out.read_text())["links"]
    assert len(links) == 18
    for link in links:
        assert link["frequency_provenance"] == "link_channel"
        assert link["frequency_ghz"] > 0
        assert link["fresnel_radius_m"] > 0

    plain = runner.invoke(app, ["web", "export", "--scope", str(config), "--out", str(out)])
    assert plain.exit_code == 0
    assert "no Fresnel zone" not in plain.output


def _zoneless_fixtures() -> dict[str, list[dict[str, Any]]]:
    """Doctor one backhaul so no frequency tier can resolve: strip its channels, break its band."""

    doctored = {path: json.loads(json.dumps(records)) for path, records in _FIXTURES.items()}
    renames = {"noc-ptp-birch-60": "noc-ptp-birch-xx", "birch-ptp-noc-60": "birch-ptp-noc-xx"}
    for device in doctored["/nms/api/v2.1/devices"]:
        name = (device.get("identification") or {}).get("name")
        if name in renames:
            device["identification"]["name"] = renames[name]
            device.get("overview", {}).pop("frequency", None)
    for link in doctored["/nms/api/v2.1/data-links"]:
        ends = [((link.get(k) or {}).get("device") or {}).get("identification") or {}
                for k in ("from", "to")]
        names = {end.get("name") for end in ends}
        if names & set(renames):
            link.pop("frequency", None)
            for end in ends:
                if end.get("name") in renames:
                    end["name"] = renames[end["name"]]
    return doctored


def test_zoneless_link_is_named_in_summary_and_json(
    offline: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doctored = _zoneless_fixtures()

    def geo_transport(self: object, path: str, timeout: float) -> list[dict[str, Any]]:
        return doctored[path]

    monkeypatch.setattr(uisp_mod.UispController, "_default_geo_transport", geo_transport)

    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    result = runner.invoke(
        app, ["web", "export", "--scope", str(config), "--out", str(out), "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert len(summary["no_zone"]) == 1
    assert "(xx)" in summary["no_zone"][0]  # the doctored link, by its display name

    plain = runner.invoke(app, ["web", "export", "--scope", str(config), "--out", str(out)])
    assert plain.exit_code == 0
    assert "no Fresnel zone" in plain.output


# --- 031: terrain profiles, clearance verdicts & omissions in the summary ------------------------


def test_terrain_unavailable_is_named_not_fatal(offline: Path, tmp_path: Path) -> None:
    """The offline default (no tiles): export succeeds, omissions are named (FR-002)."""

    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    result = runner.invoke(app, ["web", "export", "--scope", str(config), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "terrain omitted" in result.output
    payload = json.loads(out.read_text())
    assert payload["terrain_omitted"]
    assert all(link["clearance_verdict"] is None for link in payload["links"])


def _install_ridged_tiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DEM of repeating ~80 m ridges: mid-path terrain that endpoint anchoring keeps."""

    import math as math_mod

    from obi.core import terrain as terrain_mod
    from tests.fixtures.ar.terrarium_helpers import encode_terrarium

    def ridged(timeout: float):
        def fetch(z: int, x: int, y: int) -> bytes:
            grid = [
                [
                    1190.0 + 80.0 * abs(math_mod.sin((x * 256 + col) / 6.0))
                    for col in range(256)
                ]
                for row in range(256)
            ]
            return encode_terrarium(grid)

        return fetch

    monkeypatch.setattr(terrain_mod, "_default_tile_fetcher", ridged)
    monkeypatch.setattr(terrain_mod, "GRID_POSTS", 9)  # keep 34 site grids cheap in tests
    monkeypatch.setattr(terrain_mod, "SHADE_STEP_M", 4000.0)  # 055: coarse shade raster in tests


def test_flagged_verdicts_render_in_summary_and_json(
    offline: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ridged_tiles(monkeypatch)
    config = write_uisp_config(tmp_path)
    out = tmp_path / "links.json"
    result = runner.invoke(
        app, ["web", "export", "--scope", str(config), "--out", str(out), "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    flagged = summary["clearance_flagged"]
    assert flagged, "the ridged DEM must flag at least one link"
    for entry in flagged:
        assert entry["verdict"] in ("marginal", "obstructed")
        assert isinstance(entry["margin_m"], (int, float))
        assert 0.0 <= entry["at_fraction"] <= 1.0

    plain = runner.invoke(app, ["web", "export", "--scope", str(config), "--out", str(out)])
    assert plain.exit_code == 0
    assert "Terrain clearance" in plain.output

    # terrain.json landed beside the export with profiles for the verdicts.
    terrain = json.loads((tmp_path / "terrain.json").read_text())
    payload = json.loads(out.read_text())
    verdicted = [link for link in payload["links"] if link["clearance_verdict"] is not None]
    assert verdicted
    for link in verdicted:
        assert link["id"] in terrain["profiles"]
