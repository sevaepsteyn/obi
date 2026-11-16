"""Contract tests: the 094 HUD rework — links button + links panel, tap-cycled views, top
icon row (specs/094-xr-hud-links/contracts/hud-links.md).

Same tripwire discipline as the other immersive-viewer suites (test_web_viewer_link_views,
_bands, _nav, _terrain): asset-source assertions on the served ``viewer.js``, because there
is no headless WebXR runner — rendering and gestures are field-verified per the 094
quickstart (the accepted 022 trade-off). Each assertion pins a sentence of the contract.
"""

from __future__ import annotations

import re

from obi.web.server import STATIC_DIR

VIEWER = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
BANDS = (STATIC_DIR / "bands.js").read_text(encoding="utf-8")
XR_WIDGET = (STATIC_DIR / "widgets" / "xr.js").read_text(encoding="utf-8")


# --- US1: the links button + links panel ---------------------------------------------------------


def test_a_links_button_is_constructed_with_a_drawn_glyph() -> None:
    """FR-002: a persistent help-sized links button joins the icon row; its glyph is canvas-drawn
    (the 033 no-emoji-font lesson), not text."""

    assert "function makeLinksButton" in VIEWER
    assert "makeLinksButton()" in VIEWER  # instantiated, not just defined


def test_the_two_panels_come_from_the_one_factory_with_disjoint_states() -> None:
    """R1: the links panel is a second makeSettingsPanel instance. The settings state carries
    `modeRows` (and no link list); the links state carries the list (and no mode selector) —
    the factory renders sections purely from the supplied state."""

    assert VIEWER.count("= makeSettingsPanel(") == 2
    # round 3: the links instance is wider so fleet names fit — same px-per-meter density,
    # so wider means more room, never smaller text
    assert "const LINKS_PANEL_W = 768" in VIEWER
    assert "makeSettingsPanel(LINKS_PANEL_W)" in VIEWER
    assert "PANEL_WIDTH_M * (widthPx / PANEL_W)" in VIEWER
    assert "if (state.modeRows)" in VIEWER
    assert "modeRows: true" in VIEWER
    # the settings panelState no longer supplies the list; the links state owns it
    panel_state = re.search(r"function panelState\(\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert panel_state and "links:" not in panel_state.group(0)
    links_state = re.search(r"function linksPanelState\(\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert links_state and "sortedLinks()" in links_state.group(0)
    assert "links, listPage" in links_state.group(0)
    assert "modeRows" not in links_state.group(0)


def test_one_centered_surface_at_a_time() -> None:
    """FR-005 as widened by field round 2: ALL five head-locked surfaces (settings, links, help,
    inspect card, voice card) share the center dock through one closeSurfaces path — every
    open/show first closes the rest, and a link tap swaps the links panel for its card."""

    assert "function closeSurfaces" in VIEWER
    toggle_settings = re.search(r"function togglePanel\(\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert toggle_settings and "closeSurfaces('settings')" in toggle_settings.group(0)
    toggle_links = re.search(r"function toggleLinksPanel\(\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert toggle_links and "closeSurfaces('links')" in toggle_links.group(0)
    open_card = re.search(r"function openInspectCard\([^)]*\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert open_card and "closeSurfaces('inspect')" in open_card.group(0)
    assert "closeSurfaces('help')" in VIEWER
    assert "closeSurfaces('voice')" in VIEWER


def test_every_surface_docks_centered_with_a_close_affordance() -> None:
    """Field round 2: the panels, help card, inspect card, and voice card all sit at the SAME
    centered dock (0, 0, −PANEL_DISTANCE); the panels carry a title bar with a ✕ that dispatches
    the shared 'close' action (cards keep their tap-anywhere idiom)."""

    # the shared panel factory + help card + inspect card + voice card = 4 centered docks
    assert VIEWER.count("position.set(0, 0, -PANEL_DISTANCE)") == 4
    assert "function titleRow" in VIEWER
    assert "kind: 'close'" in VIEWER and "case 'close'" in VIEWER
    assert "title: 'settings'" in VIEWER
    assert "title: `links (" in VIEWER


def test_the_links_button_and_panel_route_ahead_of_the_grab_engine() -> None:
    """FR-008: a select on the links button or the open links panel performs its action and never
    starts a grab — the exact gear/panel routing discipline."""

    links_button_route = VIEWER.find("linksButtonHit(event)")
    links_panel_route = VIEWER.find("panelHit(event, linksPanel)")
    grab_route = VIEWER.find("grabs.set(event.inputSource")
    assert links_button_route != -1 and links_panel_route != -1 and grab_route != -1
    assert links_button_route < grab_route and links_panel_route < grab_route


def test_the_list_sorts_by_the_pair_canonical_name() -> None:
    """Field round 4: UISP names a link by ITS OWN endpoint order, which scattered one site
    pair's hops across the alphabet ("Tamarack <-> BRC - NOC" under S, its sibling under B) and
    read in-headset as an unsorted list. The panel maps every name through
    bands.canonicalLinkName (harness-proven) before sorting, so the SHOWN list is alphabetical
    in the SHOWN names and one pair's hops sit together; the inspect card still shows UISP's
    raw label (it looks the link up by key)."""

    assert "export function canonicalLinkName" in BANDS
    sorted_fn = re.search(r"function sortedLinks\(\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert sorted_fn and "canonicalLinkName(link.name)" in sorted_fn.group(0)


def test_scene_link_labels_grow_to_fit_their_text() -> None:
    """Field round 5: the in-scene link/peak label card was a fixed 512 px canvas — long UISP
    pair names clipped at both edges. The card now measures its lines and widens canvas AND
    sprite together at the same px-per-meter (glyph size unchanged, whole name visible),
    capped at 4× with an explicit ellipsis beyond."""

    label = re.search(r"function makeUpdatableLabel\([^)]*\) \{.*?\n\}", VIEWER, flags=re.S)
    assert label, "makeUpdatableLabel must exist"
    assert "measureText" in label.group(0)
    assert "width * (canvas.width / 512)" in label.group(0)  # sprite widens with the canvas
    assert "fitText(ctx, line" in label.group(0)  # the 4x cap degrades to ellipsis, not clip
    assert "function resizeSpriteTexture(sprite, canvas, height, width = canvas.width)" in VIEWER


def test_link_and_pager_actions_still_dispatch_through_the_one_dispatcher() -> None:
    """R6: the links panel's rows reuse dispatchPanelAction — 'link' opens the inspect card,
    'pager' pages via the shared clamped helper; no second dispatch path appears."""

    assert VIEWER.count("function dispatchPanelAction") == 1
    assert "case 'link'" in VIEWER and "case 'pager'" in VIEWER
    assert "linkListPage(" in VIEWER


def test_a_live_refresh_repaints_whichever_panel_is_open() -> None:
    """FR-006: the one refreshPanel path repaints the settings panel AND the links panel from
    their current states, so the 5-min refresh keeps an open links panel fresh (list re-render,
    page clamp, card re-resolve) exactly as it kept the old in-settings list."""

    refresh = re.search(r"function refreshPanel\(\) \{.*?\n  \}", VIEWER, flags=re.S)
    assert refresh
    assert "settingsPanel.render(panelState())" in refresh.group(0)
    assert "linksPanel.render(linksPanelState())" in refresh.group(0)


# --- US3: the icon row at the top of the view ----------------------------------------------------


def test_the_icon_row_hangs_from_one_positive_row_constant() -> None:
    """FR-009 + field round 2: gear, links, mic, and help all place from HUD_ROW_Y (positive =
    top of the view), re-projected by HUD_AT so the 0.75 m-tuned layout keeps its angular size
    and feel at the pushed-back depth. Two constants tune the whole row (height, depth)."""

    assert "const HUD_ROW_Y = 0.34" in VIEWER
    assert "const VOICE_UI_DISTANCE = 1.2" in VIEWER  # round 2: pushed back from 0.75
    assert "const HUD_AT = VOICE_UI_DISTANCE / 0.75" in VIEWER
    assert VIEWER.count("HUD_ROW_Y * HUD_AT, -VOICE_UI_DISTANCE") == 4  # gear, links, mic, help
    assert "(HUD_ROW_Y + 0.055) * HUD_AT" in VIEWER  # the mode chip docks outboard of the gear
    # the hit/hover slacks re-project with the layout, so the buttons stay as easy to aim at
    assert "0.1 * HUD_AT" in VIEWER and "0.08 * HUD_AT" in VIEWER and "0.06 * HUD_AT" in VIEWER


def test_the_mic_button_is_persistent_whenever_voice_is_enabled() -> None:
    """Field round 1 (2026-08-24): the talk button is constructed unconditionally inside the
    voice-enabled block — captureReady no longer gates construction. An unstaged capture (fresh
    origin, skipped setup-page enable) explains on hold via talk.holdStart's unavailable path
    instead of leaving a hole at the center of the icon row."""

    assert "captureReady" not in VIEWER  # the construction gate (and its import) are gone
    assert "talkButton = makeVoiceButton()" in VIEWER
    enabled_block = re.search(
        r"if \(voice && voice\.enabled\) \{.*?talkButton = makeVoiceButton\(\)", VIEWER, flags=re.S
    )
    assert enabled_block, "the mic button must be built in the voice-enabled block, ungated"


def test_the_voice_card_is_a_centered_surface_and_no_bottom_remnant_survives() -> None:
    """FR-009 + field round 2: the voice/ask-obi card left the row cluster for the shared
    centered dock (it is one of the five exclusive surfaces), and no sprite still carries the
    retired bottom-row placement."""

    voice_card = re.search(r"function makeVoiceCard\(\) \{.*?\n\}", VIEWER, flags=re.S)
    assert voice_card and "position.set(0, 0, -PANEL_DISTANCE)" in voice_card.group(0)
    assert "-0.34, -VOICE_UI_DISTANCE" not in VIEWER
    assert "set(0, -0.13, -VOICE_UI_DISTANCE)" not in VIEWER
    assert "set(0, 0.13, -VOICE_UI_DISTANCE)" not in VIEWER  # the round-1 below-row dock


def test_the_links_button_has_hover_feedback_and_a_panel_plane_snap() -> None:
    """FR-002/R6: the links button hover-highlights like gear/help, and the aim dot snaps to the
    open links panel's plane so taps land where the dot shows (2026-07-17 parallax fix)."""

    assert "linksHovering" in VIEWER and "linksButton.paint(linksHover)" in VIEWER
    assert re.search(r"panelOpen \? settingsPanel : \(linksOpen \? linksPanel : null\)", VIEWER)
