"""Contract tests: viewer BRC city-grid wiring (feature 098, contracts/brc-artifacts.md C5).

The 031-terrain tripwire discipline: the served text must show the layer present, wired to
the right seams (permanent group, one-shot fetch, panel row, pref), and the negatives
surviving — no polling, no interaction registration, no view-mode coupling. The geometry
MATH is exercised for real by tests/web_harness/brc_headless.mjs.
"""

from __future__ import annotations

from obi.web.server import STATIC_DIR

VIEWER = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
BRC_JS = (STATIC_DIR / "brc.js").read_text(encoding="utf-8")


# --- the pure module (C4) ---------------------------------------------------------------------


def test_brc_module_is_pure() -> None:
    """No three.js, no DOM, no fetch — the nav.js discipline that keeps it node-runnable."""

    assert "import" not in BRC_JS.replace("import.meta", "")  # no module imports at all
    assert "fetch(" not in BRC_JS
    assert "document." not in BRC_JS
    for export in ("LIFT_M", "makeHeightSampler", "buildCityGeometry"):
        assert f"export const {export}" in BRC_JS or f"export function {export}" in BRC_JS


# --- one-shot intake, never polled (C2/C5) ----------------------------------------------------


def test_brc_is_fetched_exactly_once_and_never_polled() -> None:
    assert VIEWER.count("fetch('brc.json'") == 1
    poll_body = VIEWER.split("async function pollOnce", 1)[1].split("const pollId", 1)[0]
    assert "brc.json" not in poll_body  # the 5-min poll stays links.json-only


def test_brc_intake_runs_after_terrain_settles_either_way() -> None:
    """G1: the city must never be hostage to terrain availability — the intake launches
    after the terrain try/catch completes, on the success AND failure paths alike."""

    terrain_block = VIEWER.split("terrain intake", 1)[1].split("grab-gesture engine", 1)[0]
    after_catch = terrain_block.split("terrain stays absent", 1)[1]
    assert "intakeBrc(" in after_catch


def test_brc_build_repaints_an_open_panel() -> None:
    intake = VIEWER.split("async function intakeBrc", 1)[1].split("\n  }", 1)[0]
    assert "refreshPanel()" in intake  # G2: an already-open panel gains the row immediately


# --- permanent group + refresh survival (C5, spec FR-011) -------------------------------------


def test_brc_group_is_a_permanent_world_child() -> None:
    build_body = VIEWER.split("function buildWorld", 1)[1].split("\n}", 1)[0]
    assert "userData.brc" in build_body


def test_brc_group_survives_live_refreshes() -> None:
    apply_body = VIEWER.split("function applyGeometry", 1)[1].split("function buildWorld", 1)[0]
    assert "brc" in apply_body and "continue" in apply_body


# --- render discipline (C5, spec FR-008/FR-009/FR-010) ----------------------------------------


def test_brc_lines_follow_the_passthrough_and_depth_rules() -> None:
    intake = VIEWER.split("async function intakeBrc", 1)[1].split("\n  }", 1)[0]
    assert "LineSegments" in intake
    assert "depthWrite: false" in intake
    assert "renderOrder" in intake


def test_brc_geometry_registers_no_interactions_and_no_mode_coupling() -> None:
    """FR-009/FR-010: invisible to applyMode, band toggles, and the gaze-anchor tables."""

    intake = VIEWER.split("async function intakeBrc", 1)[1].split("\n  }", 1)[0]
    for register in ("colorables", "allSegments", "bandGroups", "segments"):
        assert register not in intake
    mode_body = VIEWER.split("function applyMode", 1)[1].split("function tintCore", 1)[0]
    assert "brc" not in mode_body  # view modes never repaint the city


def test_brc_import_and_module_are_wired() -> None:
    assert "from './brc.js'" in VIEWER
    assert "buildCityGeometry" in VIEWER
    assert "makeHeightSampler" in VIEWER


# --- US2: the CITY GRID panel toggle (spec FR-005/FR-006, contract C5) ------------------------


def test_brc_pref_key_defaults_on() -> None:
    assert "BRC_KEY = 'obi.viewer.brc'" in VIEWER
    assert "loadViewerPref(BRC_KEY, true)" in VIEWER  # default ON for a fresh profile


def test_city_grid_row_is_gated_on_an_anchor_for_this_site() -> None:
    rows_body = VIEWER.split("function buildRows", 1)[1].split("function render(", 1)[0]
    assert "brcAvailable" in rows_body
    assert "'city grid'" in rows_body
    state_body = VIEWER.split("function panelState", 1)[1].split("function linksPanelState", 1)[0]
    assert "brcDoc" in state_body
    assert "siteName" in state_body  # availability is per-site, from the fetched doc


def test_city_grid_tap_flips_visibility_and_persists_without_rebuilding() -> None:
    assert "function toggleBrc" in VIEWER
    toggle_body = VIEWER.split("function toggleBrc", 1)[1].split("\n  }", 1)[0]
    assert ".visible" in toggle_body
    assert "saveViewerPref(BRC_KEY" in toggle_body
    assert "refreshPanel()" in toggle_body
    assert "buildCityGeometry" not in toggle_body  # a flip, never a rebuild (SC-003)
    dispatch_body = VIEWER.split("function dispatchPanelAction", 1)[1].split("\n  }", 1)[0]
    assert "'brc'" in dispatch_body
    assert "toggleBrc()" in dispatch_body


def test_no_face_button_was_taken_for_the_city_grid() -> None:
    """094's button layout is untouched: A/X cycles views, B/Y cycles terrain — the city
    grid is panel-only (research D8)."""

    assert "ZONE_BUTTON = 4" in VIEWER
    assert "TERRAIN_BUTTON = 5" in VIEWER
    assert "BRC_BUTTON" not in VIEWER


# --- US3: graceful absence (spec US3, FR-007) -------------------------------------------------


def test_failed_or_empty_intake_leaves_scene_and_panel_untouched() -> None:
    intake = VIEWER.split("async function intakeBrc", 1)[1].split("\n  }", 1)[0]
    assert "if (!built || !Object.keys(built).length) return;" in intake
    assert "the city stays absent" in intake  # the silent catch — FR-007's degraded path
    # brcDoc is set only after a successful build, so a no-anchor session never grows a row.
    assert intake.index("buildCityGeometry") < intake.index("brcDoc = doc")


# --- field round 1 (2026-08-25): the wireframe stops at the trash fence -----------------------


def test_wireframe_stops_at_the_trash_fence_and_follows_the_toggle() -> None:
    """Inside the fence the playa is the city floor — terrain grid lines under the streets
    read as noise. The WIRE mesh gets a fence-holed second index; the shaded ground is
    untouched; the mask follows the CITY GRID toggle so layer-off restores terrain exactly."""

    intake = VIEWER.split("async function intakeBrc", 1)[1].split("\n  }", 1)[0]
    assert "fenceRing" in intake
    assert "maskWireIndex" in intake
    assert "fullGeometry" in intake
    assert "fencedGeometry" in intake
    finder = VIEWER.split("function findTerrainWire", 1)[1].split("\n  }", 1)[0]
    assert "terrainStyle === 'wire'" in finder  # only the wire mesh — never the shaded fill
    toggle_body = VIEWER.split("function toggleBrc", 1)[1].split("\n  }", 1)[0]
    assert "fencedGeometry" in toggle_body
    assert "fullGeometry" in toggle_body
