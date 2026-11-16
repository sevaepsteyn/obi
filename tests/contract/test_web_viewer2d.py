"""Contract tests: the 2D viewer module (feature 033, US2 / FR-004/005/006/011).

Asset-source assertions on ``viewer2d.js`` — the repo's established pattern for viewer
code (no DOM emulation in CI; the field pass verifies live). Each test pins a sentence
from contracts/web-server-api.md's 2D section.
"""

from __future__ import annotations

import re

from obi.web.server import STATIC_DIR

VIEWER2D = (STATIC_DIR / "viewer2d.js").read_text(encoding="utf-8")
VIEWER3D = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
BANDS = (STATIC_DIR / "bands.js").read_text(encoding="utf-8")
THEME = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")


# --- one palette meaning across versions (FR-004) ---------------------------------------------


def test_health_palette_is_single_sourced_in_bands() -> None:
    """Feature 063: the four health states + maintenance color live ONCE in bands.js as CSS
    strings; the three.js numeric form is DERIVED from them (hexToInt), so the immersive and
    flat surfaces cannot drift. 044 nudged degraded yellowward (#eab308) so the maintenance
    orange (#f97316) reads as its own state. Both viewers import the palette; neither declares
    its own (the pre-063 drift risk this removes)."""

    for state, color in (
        ("healthy", "22c55e"),
        ("degraded", "eab308"),
        ("down", "ef4444"),
        ("unknown", "9ca3af"),
    ):
        assert re.search(rf"{state}:\s*['\"]#{color}['\"]", BANDS), state
    assert "MAINTENANCE_COLOR = '#f97316'" in BANDS
    # The 3D form is derived, not re-typed — this is what makes drift impossible.
    assert "HEALTH_COLORS_3D" in BANDS and "hexToInt" in BANDS
    # Neither viewer re-declares the palette; both pull it from bands.js.
    assert "HEALTH_COLORS = {" not in VIEWER2D and "HEALTH_COLORS = {" not in VIEWER3D
    assert "from './bands.js'" in VIEWER2D and "from './bands.js'" in VIEWER3D


def test_flagged_clearance_is_visually_distinguished() -> None:
    """Marginal/obstructed links get a distinct treatment (dashed stroke), keyed off the
    export's clearance_verdict — not recomputed."""

    assert "clearance_verdict" in VIEWER2D
    assert "setLineDash" in VIEWER2D


# --- thin adapter: precomputed ENU only, no geodesy (Principle III) ---------------------------


def test_layout_reads_the_export_enu_dictionaries() -> None:
    assert ".enu" in VIEWER2D


def test_viewer2d_contains_no_geodesy() -> None:
    for forbidden in ("6371", "haversine", "latitude", "longitude", "toRadians", "Math.PI / 180"):
        assert forbidden not in VIEWER2D, f"viewer2d.js contains geodesy marker: {forbidden}"


# --- freshness: one cadence, in-place rebuild (FR-006) -----------------------------------------


def test_poll_cadence_matches_the_3d_viewer() -> None:
    assert "const POLL_MS = 5 * 60 * 1000" in VIEWER2D
    assert "const POLL_MS = 5 * 60 * 1000" in VIEWER3D


def test_no_export_message_names_the_producer_command() -> None:
    assert "obi web export" in VIEWER2D


def test_terrain_is_fetched_once_not_polled() -> None:
    """terrain.json is the bulky static artifact (031): fetched once per page load; the
    poll refetches links.json only."""

    assert VIEWER2D.count("terrain.json") == 1
    poll = re.search(r"async function pollOnce\(\).*?\n  \}", VIEWER2D, flags=re.S)
    assert poll, "expected a pollOnce() refresh function"
    assert "links.json" in poll.group(0)
    assert "terrain.json" not in poll.group(0)


# --- link detail: export fields only (FR-005) --------------------------------------------------


def test_detail_panel_fields_come_from_the_export() -> None:
    for field in (
        "band",
        "frequency_ghz",
        "length_m",
        "signal_dbm",
        "clearance_verdict",
        "clearance_margin_m",
        "clearance_at_fraction",
    ):
        assert field in VIEWER2D, field


def test_profile_chart_consumes_only_precomputed_inputs() -> None:
    """Terrain line, line of sight, Fresnel envelope, worst marker — all from export fields.
    The envelope is the 2D cross-section of the ellipsoid the 3D viewer scales to (D/2, r, r):
    half-height r·2√(t(1−t))."""

    for field in ("samples_amsl", "height_amsl_m", "fresnel_radius_m", "clearance_at_fraction"):
        assert field in VIEWER2D, field
    assert re.search(r"2\s*\*\s*Math\.sqrt\(\s*t\s*\*\s*\(1\s*-\s*t\)\s*\)", VIEWER2D)


def test_missing_profile_degrades_chartless() -> None:
    """A link with no terrain profile shows its verdict fields with no chart and no error."""

    assert re.search(r"profiles\??\.\[?", VIEWER2D) or "profiles[" in VIEWER2D


# --- phone usability (FR-011) ------------------------------------------------------------------


def test_touch_and_pointer_interaction_present() -> None:
    for marker in ("pointerdown", "pointermove", "pointerup", "wheel"):
        assert marker in VIEWER2D, marker
    assert "touch-action" in VIEWER2D or "touchAction" in VIEWER2D


def test_hit_test_is_distance_to_segment_in_screen_space() -> None:
    assert "distanceToSegment" in VIEWER2D


def test_selection_survives_rebuild_when_link_still_exists() -> None:
    """In-place refresh keeps viewport + selection (FR-006); selection is re-resolved by
    link id against the fresh export."""

    assert re.search(r"selected(Link)?Id", VIEWER2D)


def test_device_pixel_ratio_aware_rendering() -> None:
    assert "devicePixelRatio" in VIEWER2D


def test_detail_panel_becomes_a_bottom_sheet_at_phone_width() -> None:
    """FR-011: at phone width the detail panel docks to the bottom edge, full width.
    (076: the rule now lives on the shared .kit-panel — the map detail is kit.createPanel.)"""

    sheet = re.search(r"@media \(max-width: \d+px\)\s*\{.*?\.kit-panel\b.*?\}", THEME, re.S)
    assert sheet, "expected a phone-width media rule for .kit-panel"
    assert "bottom: 0" in sheet.group(0)


def test_sizing_follows_the_host_element_not_the_window() -> None:
    """037 research R5: inside a workspace window, the window resizes while the browser
    viewport doesn't — the canvas must observe its HOST. The disposer releases it."""

    assert "ResizeObserver" in VIEWER2D
    assert "observer.observe(host)" in VIEWER2D
    assert "window.addEventListener('resize'" not in VIEWER2D
    assert "dispose()" in VIEWER2D


def test_voice_moved_out_of_the_map() -> None:
    """037 research R5: asking obi is the TALK widget's job — one voice surface, not two
    racing the same in-flight lock."""

    assert "setupVoice" not in VIEWER2D
    assert "voice.js" not in VIEWER2D


# --- 055 US1: parallel separation (contracts/viewer2d-layers.md §Parallel separation) ----------


def test_parallel_links_group_by_site_pair_with_symmetric_levels() -> None:
    """Visible links sharing an unordered site pair fan onto symmetric curve levels
    (i − (n−1)/2), the 052/054 treatment ported. 057 made the spacing width-aware:
    GAP_PX is the EDGE gap, and the group's thickest stroke is added on top — two fat
    capacity-scaled pipes (the 60/70 GHz Tamarack↔Mountain pair) sat 14 px apart
    center-to-center under ~6 px strokes and read as one line."""

    assert "const GAP_PX = 14" in VIEWER2D
    assert "i - (members.length - 1) / 2" in VIEWER2D
    assert "endpointSiteMap" in VIEWER2D  # grouping key is the SITE pair, not radio coords
    assert "GAP_PX + Math.max(...members.map((m) => lineWidth2d(m)))" in VIEWER2D
    # the curve itself is a quadratic through a normal-offset control point; 2× the level
    # offset puts the apex ~offset from the chord. The bow side is canonical to the CHORD,
    # not the link's own a→b order — the live 60/70 GHz pair stores reversed endpoints, which
    # once flipped one normal and overlapped the pair exactly (057 hotfix; the smoke test
    # pins the opposite-sides behavior end-to-end).
    assert "quadraticCurveTo" in VIEWER2D
    assert "const bow = offset * 2 * flip" in VIEWER2D
    assert "bx < ax || (bx === ax && by < ay)" in VIEWER2D  # the canonical-orientation flip


def test_single_member_groups_stay_straight() -> None:
    """A lone link renders exactly as today (straight moveTo/lineTo); only bowed members curve."""

    assert re.search(r"if \(g\.curved\) ctx\.quadraticCurveTo", VIEWER2D)
    assert re.search(r"else ctx\.lineTo\(g\.bx, g\.by\)", VIEWER2D)


def test_hit_test_samples_the_curve() -> None:
    """Hit-testing samples each link's actual curve (~24 segments), so a bowed parallel is
    reachable instead of its straight-chord sibling."""

    select = re.search(r"function selectAt\(x, y\).*?\n  \}", VIEWER2D, flags=re.S)
    assert select, "expected selectAt()"
    assert "qPoint(" in select.group(0)
    assert "s <= 24" in select.group(0)
    assert "curveFor(" in select.group(0) and "levelsByLinkId()" in select.group(0)


# --- 055 US2: utilization fill (contracts/viewer2d-layers.md §Utilization fill) -----------------


def test_utilization_fill_uses_the_shared_fraction_and_the_contract_ink() -> None:
    """The inner fill is driven by the shared bands.js worst-direction fraction and painted in
    the contract color at the contract width; no fill without a computable fraction."""

    assert "usageFraction(link)" in VIEWER2D
    assert "const USAGE_FILL = 'rgba(230, 237, 243, 0.5)'" in VIEWER2D
    assert re.search(r"width \* Math\.min\(fraction, 1\)", VIEWER2D)  # clamped to the stroke
    assert re.search(r"fraction != null && fraction > 0", VIEWER2D)  # absence ≠ zero


# --- 055 US3 / 057: fit control + content-bbox framing -----------------------------------------


def test_fit_control_reruns_the_initial_framing() -> None:
    """A fit chip re-runs fitViewport() + draw() in one tap."""

    assert "fit.textContent = 'fit'" in VIEWER2D
    assert "fitViewport(); draw();" in VIEWER2D  # the fit chip re-runs the initial framing


def test_fit_frames_the_content_bounding_box() -> None:
    """057: fit frames the endpoint BOUNDING BOX — per-axis scale, view centered on the bbox
    center. The original fit framed a square of max |e|,|n| around the anchor ORIGIN, so
    content asymmetric around the anchor left half the window blank (operator screenshot)."""

    fit = re.search(r"function fitViewport\(\).*?\n  \}", VIEWER2D, flags=re.S)
    assert fit, "expected fitViewport()"
    body = fit.group(0)
    assert "view.cx = (eMin + eMax) / 2" in body
    assert "view.cy = (nMin + nMax) / 2" in body
    assert "FIT_PAD_PX" in body  # chrome padding on every side
    assert "Math.min(" in body  # per-axis scale — the tighter axis wins


def test_map_controls_live_top_right_apart_from_band_chips() -> None:
    """057: fit + terrain are MAP controls, not band toggles — they render in their own
    top-right #controls2d container (operator request); band chips keep #bands2d
    bottom-left, and the detail panel opens BELOW the controls so it never hides them."""

    assert "controls.id = 'controls2d'" in VIEWER2D
    assert "function rebuildControls()" in VIEWER2D
    assert "controls.appendChild(fit)" in VIEWER2D
    assert "controls.hidden = controls.children.length === 0" in VIEWER2D
    assert "chips.hidden = chips.children.length === 0" in VIEWER2D
    chips_fn = re.search(r"function rebuildChips\(\).*?\n  \}", VIEWER2D, flags=re.S)
    assert chips_fn and "'fit'" not in chips_fn.group(0)  # the band row hosts bands only
    controls_css = THEME.split("#controls2d {")[1].split("}")[0]
    assert "top: 0.75rem" in controls_css and "right: 0.75rem" in controls_css
    # 076: the detail panel is now the shared kit.createPanel, which floats BOTTOM-right — clear
    # of the top-right controls (an open panel never hides fit/terrain), the same guarantee.
    panel_css = THEME.split(".kit-panel {")[1].split("}")[0]
    assert "bottom: 0.6rem" in panel_css and "right: 0.6rem" in panel_css


# --- 055 US4: terrain underlay (contracts/viewer2d-layers.md §Terrain layer rendering) ---------


def test_terrain_toggle_key_is_this_surfaces_own() -> None:
    assert "const TERRAIN_KEY = 'obi.viewer2d.terrain'" in VIEWER2D
    assert "'obi.viewer.terrain'" not in VIEWER2D  # never crosses to the 3D surface


def test_terrain_underlay_draws_first_and_is_guarded() -> None:
    """The hillshade draws FIRST in draw() (beneath the links) and its failure is caught locally
    so it can never blank the link layer (FR-012)."""

    underlay_call = VIEWER2D.index("if (terrainOn) drawTerrainUnderlay()")
    link_loop = VIEWER2D.index("const levels = levelsByLinkId()")
    assert underlay_call < link_loop  # draw order: underlay before links
    body = re.search(r"function drawTerrainUnderlay\(\).*?\n  \}", VIEWER2D, flags=re.S)
    assert body and "try {" in body.group(0) and "catch" in body.group(0)
    assert "drawImage" in body.group(0)


def test_terrain_chip_requires_a_rect_for_the_current_anchor() -> None:
    """The toggle appears only when the shade meta AND a rect for the current anchor exist; the
    image is fetched lazily, at most once, after the first enable (never in the poll)."""

    assert "function terrainAvailable()" in VIEWER2D
    assert "terrain.shade.sites[site.name]" in VIEWER2D
    assert VIEWER2D.count("terrain-shade.png") == 1  # the one lazy image fetch
    # the shade image intake is an Image, not a fetch inside pollOnce
    poll = re.search(r"async function pollOnce\(\).*?\n  \}", VIEWER2D, flags=re.S)
    assert poll and "terrain-shade" not in poll.group(0)


def test_peaks_render_muted_and_ui_scaled() -> None:
    assert "function drawPeaks()" in VIEWER2D
    assert "const PEAK_COLOR" in VIEWER2D
    assert "peak.enu" in VIEWER2D  # positions consumed verbatim (no geodesy)
