"""Contract tests: viewer terrain rendering (feature 031, contracts/ar-viewer-terrain.md).

Same tripwire discipline as 022/025/029 (no browser JS runner): the served text must show
the terrain machinery present, wired to the right inputs, and the thin-adapter negatives
(no geodesy, no external origins, poll untouched) surviving the new code.
"""

from __future__ import annotations

import re

from obi.web.server import STATIC_DIR

VIEWER = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
XR_WIDGET = (STATIC_DIR / "widgets" / "xr.js").read_text(encoding="utf-8")


# --- US2: ground profiles + verdict labels --------------------------------------------------


def test_terrain_is_fetched_exactly_once_and_never_polled() -> None:
    assert VIEWER.count("fetch('terrain.json'") == 1
    poll_body = VIEWER.split("async function pollOnce", 1)[1].split("const pollId", 1)[0]
    assert "terrain.json" not in poll_body  # the 5-min poll stays links.json-only (FR-010)
    assert "links.json" in poll_body


def test_profiles_join_links_by_id() -> None:
    assert "userData.profiles" in VIEWER
    assert "[link.id]" in VIEWER
    assert "samples_amsl" in VIEWER
    assert "ground_elevation_m" in VIEWER  # the blessed affine map's only vertical input


def test_verdict_suffix_marks_only_non_clear_links() -> None:
    assert "verdictSuffix" in VIEWER
    assert "clearance_verdict" in VIEWER
    assert "'marginal'" in VIEWER
    assert "'obstructed'" in VIEWER
    assert "clearance_margin_m" in VIEWER
    assert "clearance_at_fraction" in VIEWER


# --- US3: wireframe + toggle ----------------------------------------------------------------


def test_terrain_group_is_a_world_child_with_wireframe_material() -> None:
    assert "userData.terrain" in VIEWER
    assert "wireframe: true" in VIEWER
    assert "rel_heights_dm" in VIEWER
    assert "half_extent_m" in VIEWER
    # 025 passthrough discipline extends to terrain: transparent, never depth-writing.
    assert VIEWER.count("depthWrite: false") >= 2  # zones + terrain


def test_terrain_toggle_is_the_secondary_face_button_edge_triggered() -> None:
    assert "TERRAIN_BUTTON = 5" in VIEWER
    assert "toggleTerrain" in VIEWER
    assert "terrainButtonWasPressed" in VIEWER
    assert "ZONE_BUTTON = 4" in VIEWER  # the 025 toggle is untouched


def test_terrain_group_survives_live_refreshes() -> None:
    apply_body = VIEWER.split("function applyGeometry", 1)[1].split("function buildWorld", 1)[0]
    assert "terrain" in apply_body and "continue" in apply_body


# --- US4: peaks -------------------------------------------------------------------------------


def test_peaks_render_as_labels_inside_the_terrain_group() -> None:
    assert "peaks" in VIEWER
    assert "peak.name" in VIEWER
    assert "enuToScene(peak.enu)" in VIEWER  # positions consumed verbatim


# --- thin-adapter negatives (022/029) re-asserted on the 031 text ----------------------------


def test_viewer_stays_a_thin_adapter_with_no_external_origins() -> None:
    for marker in ("6371", "haversine", "toRadians", "latitude", "Mercator math"):
        assert marker not in VIEWER, marker
    assert not re.search(r"https?://", VIEWER)  # tiles/gazetteer never reach the headset
    assert "s3.amazonaws.com" not in VIEWER
    assert not re.search(r"https?://(?!localhost)", XR_WIDGET)


def test_setup_still_fetches_once_and_mentions_the_terrain_toggle() -> None:
    # 037: the setup surface is the XR widget (same one-shot fetch + controls crib).
    assert XR_WIDGET.count("fetch(") == 1  # terrain intake lives in viewer.js, post-session
    assert "B/Y" in XR_WIDGET
    assert "terrain" in XR_WIDGET.lower()


def test_missing_terrain_artifact_degrades_silently() -> None:
    intake = VIEWER.split("terrain intake", 1)[1].split("grab-gesture engine", 1)[0]
    assert "response.ok" in intake  # 404 ⇒ return, no error path
    assert "catch" in intake
