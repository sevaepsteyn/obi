"""Contract tests: per-band visibility toggles (feature 044, US1 — contracts/viewer-bands.md).

Asset-source assertions on the served modules — the repo's established viewer pattern (no
DOM emulation in CI; rendering and gestures are field-verified per quickstart). Each test
pins a sentence of contracts/viewer-bands.md.
"""

from __future__ import annotations

import re

from obi.web.server import STATIC_DIR

BANDS = (STATIC_DIR / "bands.js").read_text(encoding="utf-8")
VIEWER2D = (STATIC_DIR / "viewer2d.js").read_text(encoding="utf-8")
VIEWER3D = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")


# --- one band vocabulary, one source (contracts/viewer-bands.md §Band toggles) -----------------


def test_the_fixed_band_order_is_defined_once_in_the_shared_module() -> None:
    assert (
        "export const BAND_ORDER = ['5 GHz', '11 GHz', '24 GHz', '60 GHz', '70 GHz', 'other']"
        in BANDS
    )


def test_both_viewers_import_the_shared_band_module() -> None:
    for source, name in ((VIEWER2D, "viewer2d.js"), (VIEWER3D, "viewer.js")):
        assert re.search(r"from ['\"]\./bands\.js['\"]", source), name
        # neither surface redefines the vocabulary locally
        assert "BAND_ORDER =" not in source, name


def test_unknown_bands_fall_into_the_catch_all_group() -> None:
    """A raw token or absent band maps to 'other' — no link is ever unreachable."""

    assert "return BAND_ORDER.includes(band) ? band : 'other'" in BANDS


def test_band_read_is_nullish_safe_for_pre_044_exports() -> None:
    assert "typeof link.band === 'string'" in BANDS


# --- persistence (FR-005) -----------------------------------------------------------------------


def test_each_surface_persists_under_its_own_key() -> None:
    assert "'obi.viewer2d.bands'" in VIEWER2D
    assert "'obi.viewer.bands'" in VIEWER3D
    # and the keys never cross surfaces
    assert "'obi.viewer.bands'" not in VIEWER2D
    assert "'obi.viewer2d.bands'" not in VIEWER3D


def test_persistence_stores_hidden_bands_only_and_drops_stale_labels() -> None:
    # hidden-only shape {label: false} …
    assert "[band, false]" in BANDS
    # … and load filters through the current vocabulary, ignoring stale entries
    assert "BAND_ORDER.filter((band) => raw?.[band] === false)" in BANDS


def test_storage_failure_degrades_to_session_only_toggles() -> None:
    """Private-mode browsers throw on localStorage access; toggles must survive that."""

    assert BANDS.count("try {") >= 2 and BANDS.count("catch") >= 2


# --- hiding is total per band (FR-003) ------------------------------------------------------------


def test_2d_draw_and_hit_test_both_skip_hidden_bands() -> None:
    """Hidden links are neither drawn nor selectable."""

    assert VIEWER2D.count("hiddenBands.has(bandOf(link))") >= 2


def test_3d_band_groups_carry_links_zones_and_labels_together() -> None:
    """The immersive scene parents each link's objects to its band group, so one visibility
    flip hides line, markers, label, zones, and profile as a unit."""

    assert "bandGroups" in VIEWER3D
    assert "zoneBandGroups" in VIEWER3D


def test_3d_gaze_anchor_ignores_hidden_bands() -> None:
    """Invisible content must not magnetize the 029 grab gestures."""

    assert "hiddenBands.has(segment.band)" in VIEWER3D or (
        "visibleSegments" in VIEWER3D and "segment.band" in VIEWER3D
    )


# --- XR palette routing (FR-004, the 032 discipline) ----------------------------------------------


def test_xr_band_toggle_is_a_panel_row_not_a_floating_chip() -> None:
    """078 US4/R7: the 044 standalone chip column is subsumed by the settings panel. The band
    toggle is now a panel row whose select is consumed by the panel hit-test — which sits ahead of
    the grab engine — so a band tap still never reaches the grab engine, and the chip constructor,
    palette refresh, and chip hit-test are gone."""

    for removed in ("makeBandChip", "bandChipHit", "refreshBandPalette"):
        assert removed not in VIEWER3D, removed
    # the band toggle still persists + applies, now dispatched from a panel 'band' action ahead of
    # the grab engine (panelHit precedes grabs.set — pinned in test_web_viewer_link_views)
    assert "toggleBand(" in VIEWER3D
    panel_route = VIEWER3D.find("panelHit")
    grab_route = VIEWER3D.find("grabs.set(event.inputSource")
    assert panel_route != -1 and grab_route != -1 and panel_route < grab_route


def test_xr_toggle_state_survives_live_refresh() -> None:
    """applyGeometry rebuilds the world's children; band visibility must be re-applied from
    the persisted set, not reset."""

    assert re.search(r"applyGeometry\([^)]*hiddenBands", VIEWER3D)


# --- capacity encoding (044 US2, contracts/viewer-bands.md §Thickness/§Labels) -------------------


def test_the_thickness_scale_is_defined_once_with_the_contract_constants() -> None:
    """One shared scale, exact contract numbers: sqrt-normalized against a 3 Gbps ceiling,
    2D width 1.5 + n·4.5 px (default 2), 3D radius 0.4 + n·2.1 m (default 0.7)."""

    assert "export const CAP_MAX_BPS = 3e9" in BANDS
    assert "1.5 + norm * 4.5" in BANDS
    assert "0.4 + norm * 2.1" in BANDS
    assert "norm === null ? 2 " in BANDS
    assert "norm === null ? 0.7 " in BANDS
    assert "Math.sqrt(Math.min(capacity, CAP_MAX_BPS) / CAP_MAX_BPS)" in BANDS
    # neither surface grows a private scale
    for source, name in ((VIEWER2D, "viewer2d.js"), (VIEWER3D, "viewer.js")):
        assert "CAP_MAX" not in source, name


def test_2d_width_and_3d_radius_come_from_the_shared_scale() -> None:
    assert "lineWidth2d(link)" in VIEWER2D
    assert "linkRadius3d(link)" in VIEWER3D
    # 044: the 3D link body is real geometry — WebGL ignores THREE.Line width
    assert "CylinderGeometry" in VIEWER3D


def test_capacity_reads_are_nullish_safe_and_never_fabricated() -> None:
    """A link without capacity gets the neutral default and NO label line — the guard
    lives in the shared module, so both surfaces inherit it."""

    assert "capacity <= 0) return null" in BANDS
    assert "if (!down && !up) return null" in BANDS


def test_both_surfaces_render_the_capacity_label_from_the_one_formatter() -> None:
    assert "capacityLine(link)" in VIEWER2D
    assert "capacityLine(link)" in VIEWER3D


def test_2d_panel_states_unavailable_capacity_explicitly() -> None:
    """FR-009: absence must never read as zero."""

    assert "row('capacity', 'unavailable')" in VIEWER2D


# --- maintenance rendering (044 US3, contracts/viewer-bands.md §Colors) ---------------------------


def test_maintenance_wins_the_line_color_in_both_surfaces() -> None:
    """Orange overrides any health color for the line/cylinder — strict-boolean read, so a
    pre-044 payload (no key) can never render orange."""

    assert "link.maintenance === true" in VIEWER2D
    assert "link.maintenance === true" in VIEWER3D


def test_maintenance_never_hides_the_underlying_health() -> None:
    """2D panel and the XR label both state the flag AND the health."""

    assert "`maintenance · ${link.health}`" in VIEWER2D
    assert "maintenance (health: ${link.health})" in VIEWER3D


def test_maintenance_endpoint_dots_go_orange_in_both_surfaces() -> None:
    assert VIEWER2D.count("endpointMaintenance") >= 2
    assert VIEWER3D.count("endpointMaintenance") >= 2


# --- utilization fraction (055 US2, contracts/bands-usage.md) -----------------------------------


def test_usage_fraction_is_exported_and_worst_direction() -> None:
    """The utilization-fill scale lives in the shared module, is worst-direction (MAX over the
    computable directions), and is UNCLAMPED — the renderer clamps at draw time."""

    assert "export function usageFraction(link)" in BANDS
    # iterates both directions and keeps the max
    assert "for (const dir of ['down', 'up'])" in BANDS
    assert re.search(r"if \(best === null \|\| fraction > best\) best = fraction", BANDS)
    # no clamping in the helper (min() lives in the renderer)
    fn = re.search(r"export function usageFraction\(link\) \{.*?\n\}", BANDS, flags=re.S)
    assert fn and "Math.min" not in fn.group(0)


def test_usage_fraction_is_nullish_safe_and_capacity_guarded() -> None:
    """Both factors must be finite, capacity > 0, usage ≥ 0 — else that direction is skipped;
    no direction computable ⇒ null (absence never becomes zero). Pre-044 exports ⇒ null."""

    fn = re.search(r"export function usageFraction\(link\) \{.*?\n\}", BANDS, flags=re.S)
    assert fn
    body = fn.group(0)
    assert "link?.[`usage_${dir}_bps`]" in body
    assert "link?.[`capacity_${dir}_bps`]" in body
    assert "capacity > 0" in body
    assert "usage >= 0" in body
    assert "let best = null" in body and "return best" in body


def test_usage_fraction_does_not_disturb_the_existing_down_only_text() -> None:
    """usagePercent (the down-only label text, 044) stays; usageFraction is the new visual
    scale — both exist, each for its consumer."""

    assert "export function usagePercent(link)" in BANDS
    assert "const usage = link?.usage_down_bps" in BANDS  # usagePercent unchanged, down-only


# --- 078 XR link-view vocabulary (contracts/view-modes.md, panel-card.md §Tripwires) -------------


def test_the_four_view_modes_are_defined_in_the_shared_module() -> None:
    """Exactly health/signal/utilization/fresnel, health first (default), as one ordered set both
    the panel selector and the persistence validation read from. Fresnel became the fourth VIEW
    (was a scene toggle) in the 2026-07-17 field amendment."""

    assert "export const MODE_HEALTH = 'health'" in BANDS
    assert "export const MODE_SIGNAL = 'signal'" in BANDS
    assert "export const MODE_UTILIZATION = 'utilization'" in BANDS
    assert "export const MODE_FRESNEL = 'fresnel'" in BANDS
    assert (
        "export const VIEW_MODES = "
        "[MODE_HEALTH, MODE_SIGNAL, MODE_UTILIZATION, MODE_FRESNEL]" in BANDS
    )
    # the fresnel view's categorical link colors (clearance verdicts) live here too
    assert "export const CLEARANCE_COLORS" in BANDS


def test_the_ramp_stops_match_the_contract_hexes() -> None:
    """SIGNAL_RAMP green→yellow→red and USAGE_RAMP blue→yellow→red, the exact
    contracts/view-modes.md stops — one warm/cool vocabulary, idle-blue distinct from health."""

    signal = re.search(r"export const SIGNAL_RAMP = \[.*?\];", BANDS, flags=re.S)
    usage = re.search(r"export const USAGE_RAMP = \[.*?\];", BANDS, flags=re.S)
    assert signal and usage
    for hex_stop in ("#22c55e", "#eab308", "#ef4444"):
        assert hex_stop in signal.group(0), hex_stop
    for hex_stop in ("#3b82f6", "#eab308", "#ef4444"):
        assert hex_stop in usage.group(0), hex_stop
    # the signal anchors are the documented −45/−70 dBm ends (R1)
    assert "export const SIGNAL_STRONG_DBM = -45" in BANDS
    assert "export const SIGNAL_WEAK_DBM = -70" in BANDS


def test_the_new_mode_helpers_are_exported() -> None:
    """The panel-card §Tripwires export list — the whole 078 vocabulary lives in the one shared
    module, so viewer.js can never grow a private ramp or a second precedence rule."""

    for name in (
        "export function rampColor(",
        "export function signalT(",
        "export function modeColor3d(",
        "export function coreScale(",
        "export function linkListPage(",
        "export function loadViewerPref(",
        "export function saveViewerPref(",
    ):
        assert name in BANDS, name


def test_no_data_treatment_is_the_unknown_gray_at_reduced_opacity() -> None:
    """FR-004: the no-data color is a point on NEITHER ramp (the existing unknown gray), and the
    opacity drop is what makes 'no data' legible — one treatment for every metric-less link."""

    assert "export const NO_DATA_COLOR_3D = HEALTH_COLORS_3D.unknown" in BANDS
    assert "export const NO_DATA_OPACITY = 0.45" in BANDS


def test_modecolor3d_precedence_puts_maintenance_and_down_before_the_metric() -> None:
    """The maintenance>down>metric>no-data order is structural (contracts §Precedence): the two
    overrides return before either metric branch, so a dead/maintenance link never ramp-colors."""

    fn = re.search(r"export function modeColor3d\(link, mode\) \{.*?\n\}", BANDS, flags=re.S)
    assert fn
    body = fn.group(0)
    maint = body.find("link?.maintenance === true")
    down = body.find("link?.health === 'down'")
    signal_branch = body.find("mode === MODE_SIGNAL")
    util_branch = body.find("mode === MODE_UTILIZATION")
    assert -1 < maint < down < signal_branch < util_branch


def test_corescale_uses_the_contract_sizing() -> None:
    """R10: radius 0.15 + 0.75·f of the outer radius, length = f of the chord; null ⇒ no core."""

    assert "0.15 + 0.75 * f" in BANDS
    assert "lengthFactor: f" in BANDS
