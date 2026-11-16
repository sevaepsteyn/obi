"""Contract tests: the 078 XR link views — color view modes, the settings panel, the inspect
card, and the utilization inner core (contracts/view-modes.md, panel-card.md).

Same tripwire discipline as the other immersive-viewer suites (test_web_viewer_bands, _nav,
_terrain): asset-source assertions on the served `viewer.js`/`bands.js`, because there is no
headless WebXR runner — the rendering, gestures, and legibility are field-verified per quickstart
(the accepted 022 trade-off). Each assertion pins a sentence of the two contracts. The pure math
these strings route to (ramps, precedence, paging) is separately executed by the
bands_modes_headless.mjs harness (test_web_bands_modes).
"""

from __future__ import annotations

import re

from obi.web.server import STATIC_DIR

VIEWER = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
BANDS = (STATIC_DIR / "bands.js").read_text(encoding="utf-8")
LINK_WIDGET = (STATIC_DIR / "widgets" / "link.js").read_text(encoding="utf-8")


# --- US1: color view modes (contracts/view-modes.md) --------------------------------------------


def test_viewer_persists_the_color_view_mode_under_its_own_key() -> None:
    """FR-017: the active view survives sessions under obi.viewer.mode, via the shared
    load/save helpers (bands.js is the sanctioned storage owner)."""

    assert "'obi.viewer.mode'" in VIEWER
    assert "loadViewerPref" in VIEWER and "saveViewerPref" in VIEWER


def test_the_four_modes_come_from_the_shared_vocabulary_not_fabricated() -> None:
    """The mode set is the bands.js constants (incl. the fresnel view, 2026-07-17 amendment), and
    the ordered VIEW_MODES is what a stored pref is validated against — viewer.js never invents a
    fifth mode or a private mode string."""

    for name in ("MODE_HEALTH", "MODE_SIGNAL", "MODE_UTILIZATION", "MODE_FRESNEL", "VIEW_MODES"):
        assert name in VIEWER, name
    assert re.search(r"from ['\"]\./bands\.js['\"]", VIEWER)


def test_the_fresnel_view_owns_the_zone_ellipsoids() -> None:
    """2026-07-17 field amendment (rounds 3+4), re-amended by 094: Fresnel zones are the fourth
    VIEW, not a scene toggle — the ellipsoids show exactly while the fresnel view is active. The
    quick tap AND A/X both cycle through ALL views (094 FR-007: the fresnel peek and its
    lastSolidMode return-view are retired), and the retired obi.viewer.zones key is never read
    again."""

    assert "function cycleMode" in VIEWER
    assert "zones.visible = viewMode === MODE_FRESNEL" in VIEWER
    assert "toggleFresnelView" not in VIEWER
    assert "lastSolidMode" not in VIEWER
    # the tap branch of selectend reaches the SAME cycle the A/X button calls
    assert re.search(r"TAP_MOVE_M\) cycleMode\(\)", VIEWER)
    assert "'obi.viewer.zones'" not in VIEWER
    assert "case 'zones'" not in VIEWER


def test_the_fresnel_view_owns_the_ground_profiles_too() -> None:
    """2026-08-29 field ask: the DEM ground line under each link is clearance context, not
    link state — it shows exactly while the fresnel view is active. applyMode flips it in
    place with the mode change (the profile rides the colorable), so the color modes stay
    free of terrain noise under the links."""

    assert "profile.visible = viewMode === MODE_FRESNEL" in VIEWER
    assert "markers, core, profile" in VIEWER  # the profile rides the colorable into applyMode


def test_link_coloring_routes_through_the_one_authority() -> None:
    """Every link cylinder + endpoint marker colors via bands.modeColor3d — viewer.js grows NO
    private ramp or precedence rule (FR-001..FR-005 live in one place)."""

    assert "modeColor3d(" in VIEWER
    # no fabricated metric values: the ramps + no-data gray live only in bands.js
    assert "SIGNAL_RAMP =" not in VIEWER
    assert "USAGE_RAMP =" not in VIEWER


def test_mode_switch_recolors_in_place_without_a_world_rebuild() -> None:
    """SC-005: switching views recolors materials in place (a setMode path), not by tearing down
    and rebuilding the world (which would drop the navigation transform)."""

    assert "function setMode" in VIEWER
    # setMode saves the pref so the choice persists (FR-017)
    fn = re.search(r"function setMode\([^)]*\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert fn and "saveViewerPref" in fn.group(0)


# --- US1: settings panel + gear button (contracts/panel-card.md) --------------------------------


def test_a_gear_settings_button_and_a_settings_panel_are_constructed() -> None:
    """FR-008/FR-009: a persistent head-locked button opens a settings-panel sprite."""

    assert "function makeGearButton" in VIEWER
    assert "function makeSettingsPanel" in VIEWER


def test_the_gear_stays_a_gear_and_the_mode_chip_reads_out_the_view() -> None:
    """FR-007 (2026-07-17 field amendment): the gear is ALWAYS the settings glyph — the active
    view is shown by a separate persistent 'mode: …' chip, so the mode is readable with the panel
    closed without overloading the settings affordance."""

    assert "function makeModeChip" in VIEWER
    assert "'SIG'" not in VIEWER and "'UTL'" not in VIEWER
    assert "modeChip.paint(viewMode)" in VIEWER


def test_a_help_button_opens_the_controls_cheat_sheet() -> None:
    """2026-07-17 field request: a persistent ? button (right of the mic on the icon row) opens a
    head-locked cheat sheet of buttons and gestures; a tap on the sheet (or the ? again) closes
    it, and its hit-tests precede the grab engine like every other head-locked control. 094: the
    sheet describes the new bindings — the quick tap cycles the view, the links button is the
    fleet list."""

    assert "function makeHelpButton" in VIEWER
    assert "function makeHelpCard" in VIEWER
    help_route = VIEWER.find("helpHit")
    grab_route = VIEWER.find("grabs.set(event.inputSource")
    assert help_route != -1 and help_route < grab_route
    # the sheet documents at least the core gesture + button vocabulary
    for phrase in (
        "push-to-talk",
        "pan the world",
        "cycle view mode",
        "fleet list",
        "terrain: off / wireframe / shaded",
    ):
        assert phrase in VIEWER, phrase
    assert "fresnel view on/off" not in VIEWER  # the retired 078 peek row (094 FR-010)


def test_terrain_has_wire_and_shaded_styles_with_per_session_defaults() -> None:
    """2026-07-17 field request: terrain renders as the 031 wireframe OR a lit shaded relief, one
    style-tagged mesh pair over the same displaced geometry; passthrough defaults to wire,
    opaque to shaded, each persisting its own choice under a per-kind key.

    119: the per-kind defaults moved out of viewer.js into the pure `terrainStyleDefaultFor`
    helper in bands.js, because the backdrop now changes mid-session and the key must be
    derived per use rather than captured at entry. The literal ternary this used to pin lives
    in bands.js now and is EXECUTED by bands_modes_headless.mjs — a stronger check than the
    grep it replaces.
    """

    assert "function applyTerrainStyle" in VIEWER
    assert "obi.viewer.terrainStyle." in BANDS
    assert "terrainStyleDefaultFor" in VIEWER  # viewer defers the defaults to the helper
    assert "terrainStyleKeyFor" in VIEWER  # ...and the key, derived at each use
    assert "sessionKind" not in VIEWER  # the captured session-time constant is gone
    assert "MeshLambertMaterial" in VIEWER
    assert "wireframe: true" in VIEWER  # the 031 silhouette style survives
    # rounds 4-5: ONE tri-state control — the panel row and B/Y both cycle off/wire/shaded — and
    # the wireframe overlays the shaded relief as DARKER contour lines (polygon-offset solid)
    assert "case 'terrainCycle'" in VIEWER
    assert "function cycleTerrain" in VIEWER
    assert "polygonOffset: true" in VIEWER
    assert "TERRAIN_CONTOUR_COLOR" in VIEWER


def test_the_panel_hit_test_precedes_the_grab_engine() -> None:
    """FR-020: a select that hits the gear button or the panel toggles/routes and never reaches
    the grab engine — the exact talk-button routing discipline."""

    gear_route = VIEWER.find("gearHit")
    panel_route = VIEWER.find("panelHit")
    grab_route = VIEWER.find("grabs.set(event.inputSource")
    assert gear_route != -1 and panel_route != -1 and grab_route != -1
    assert gear_route < grab_route and panel_route < grab_route


# --- US2: paged link list + inspect card (contracts/panel-card.md) -------------------------------

# The card renders the SAME facts the 2D LINK window (widgets/link.js) shows for the same link
# (SC-007). A grep-level parity tripwire on the field tokens catches a drift in either direction.
CARD_FACT_FIELDS = [
    "signal_dbm",
    "capacity_down_bps",
    "capacity_up_bps",
    "usage_down_bps",
    "usage_up_bps",
    "frequency_ghz",
    "frequency_provenance",
    "length_m",
    "clearance_verdict",
    "clearance_margin_m",
    "clearance_at_fraction",
]


def test_the_panel_link_list_is_paged_via_the_shared_helper() -> None:
    """FR-011: the panel's link list is paged (size 8, ▲/▼) through bands.linkListPage — the same
    clamp/reset math the harness proves, not a bespoke sprite drag-scroll (which would fight the
    grab-drag detection)."""

    assert "linkListPage(" in VIEWER
    # a link-list row tap and a pager tap are distinct panel actions
    assert "'link'" in VIEWER and "'pager'" in VIEWER


def test_an_inspect_card_sprite_is_constructed() -> None:
    """FR-011/FR-015: one head-locked inspect card, opened from a link-list row."""

    assert "function makeInspectCard" in VIEWER
    # re-resolved by the canonical link identity on every refresh (US2 #5)
    assert "linkKey(" in VIEWER


def test_the_card_facts_match_the_2d_link_window_field_for_field() -> None:
    """SC-007: every fact the card shows is a field the widgets/link.js window shows for the same
    link — the parity bar. Both files must reference each fact field."""

    for field in CARD_FACT_FIELDS:
        assert field in VIEWER, f"card missing fact field {field}"
        assert field in LINK_WIDGET, f"2D link window missing fact field {field}"


def test_the_card_uses_the_shared_formatters_not_bespoke_math() -> None:
    """The card's rates + usage percent come from the same bands.js formatters the 2D uses, so the
    numbers can never drift (SC-007)."""

    for fn in ("formatRate(", "usagePercent("):
        assert fn in VIEWER and fn in LINK_WIDGET


def test_missing_card_values_render_as_an_explicit_dash_never_zero() -> None:
    """FR-014: an absent value (Siklu usage, a missing signal) shows as — on the card, never 0."""

    assert "'—'" in VIEWER


def test_the_card_states_when_its_link_left_the_snapshot() -> None:
    """US2 #5: a refresh that drops the inspected link keeps the card with an explicit banner
    rather than showing stale values as current."""

    assert "no longer in the current snapshot" in VIEWER


# --- US3: utilization inner core (contracts/view-modes.md §inner core) ---------------------------


def test_the_inner_core_sizing_comes_from_the_shared_helper() -> None:
    """FR-016/R10: the mid-span core's radius + length come from bands.coreScale — viewer.js does
    not invent its own fill math."""

    assert "coreScale(" in VIEWER
    assert "usageFraction(" in VIEWER  # the fraction feeding coreScale


def test_the_core_is_absent_for_null_usage_and_for_down_links() -> None:
    """FR-016: absence, never an empty pipe — no mesh when usageFraction is null; and R4 — no core
    on a down link (the down treatment must stay unambiguous)."""

    core_block = re.search(r"coreScale\(usageFraction\(link\)\)", VIEWER)
    assert core_block, "core sizing must come from coreScale(usageFraction(link))"
    # a down link is excluded before the fraction is even consulted
    assert re.search(r"link\.health === 'down' \? null : coreScale", VIEWER)


def test_the_core_is_white_hot_and_tracked_for_mode_tinting() -> None:
    """R10: the core is white-hot (#f8fafc), tinted toward the active mode color at high load — so
    it is stored on the colorable and retinted by applyMode when the mode changes."""

    assert "0xf8fafc" in VIEWER or "#f8fafc" in VIEWER
    assert "tintCore(" in VIEWER


# --- US4: consolidated toggles + chip removal (contracts/panel-card.md) --------------------------


def test_terrain_and_its_style_persist_under_their_own_keys() -> None:
    """FR-017 (2026-07-17 amendment): terrain visibility persists per XR surface; the terrain
    STYLE persists per backdrop (AR/VR keys); zones persist as part of the mode itself
    (MODE_KEY carries 'fresnel'). All writes go through the ONE shared path per control (R11).

    119: the style key is now DERIVED at each use via `terrainStyleKeyFor(opaque)` rather than
    captured once as TERRAIN_STYLE_KEY — the backdrop can flip mid-session, and a captured key
    would persist against the side the operator is no longer looking at.
    """

    assert "saveViewerPref(TERRAIN_KEY" in VIEWER
    assert "loadViewerPref(TERRAIN_KEY" in VIEWER
    assert "saveViewerPref(terrainStyleKeyFor(" in VIEWER
    assert "loadViewerPref(terrainStyleKeyFor(" in VIEWER
    assert "TERRAIN_STYLE_KEY" not in VIEWER  # the captured constant must not come back


def test_the_floating_band_chip_column_is_removed() -> None:
    """R7: the 044 standalone chip column is subsumed by the panel — the chip constructor, its
    palette refresh, and its hit-test are gone; bands are panel rows now."""

    assert "makeBandChip" not in VIEWER
    assert "refreshBandPalette" not in VIEWER
    assert "bandChipHit" not in VIEWER


def test_mode_terrain_style_and_bands_dispatch_from_panel_actions() -> None:
    """FR-009/FR-010: the panel carries mode/terrain/terrain-style/band rows, each dispatching to
    the same path the legacy shortcuts call, so the panel and the shortcuts stay consistent."""

    for case in ("case 'mode'", "case 'terrainCycle'", "case 'band'"):
        assert case in VIEWER, case
    assert "toggleBand(" in VIEWER
