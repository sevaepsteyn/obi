"""Contract: the OSPF map widget rendering surface (feature 052, polished by 053).

Asset-source assertions (research R8 — the repo's no-DOM-in-CI convention). Correctness of the
analysis lives in the Python unit tests (the widget only renders precomputed results, R3); these
pin the RENDERING contract clauses: per-side interface+cost labels on curved parallel links,
value-only tier styling, SPF emphasis with a toolbar root selector, ghosts (one-sided drawn to
their resolved peer), SPOF marks, pseudonodes, external badges, the default-route statement
panel, click-to-open drill-downs, and the separate lint window with focus-on-select.
"""

from __future__ import annotations

from obi.web.server import STATIC_DIR

OSPF = (STATIC_DIR / "widgets" / "ospf.js").read_text(encoding="utf-8")
THEME = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")


def test_polls_the_snapshot_and_wears_age_and_absent_states() -> None:
    assert "/snapshots/ospf.json" in OSPF
    assert "formatAge" in OSPF
    assert "no OSPF snapshot yet" in OSPF  # the standard absent state (FR-018)
    assert "setBadge" in OSPF


def test_each_side_is_labeled_with_its_interface_and_outbound_cost() -> None:
    """053: costs can differ per direction, so each end carries its interface name with that
    side's outbound cost — no collapsed center label."""
    assert "function sideLabel(" in OSPF
    assert "function labelFor(" in OSPF
    assert "edge.iface_a, edge.cost_ab" in OSPF
    assert "edge.iface_b, edge.cost_ba" in OSPF
    assert "drawEdgeCost" not in OSPF  # the 052 center-label helper is gone


def test_edge_labels_rotate_along_their_link_and_lod_gate_the_names() -> None:
    """054 declutter: a side label is drawn rotated along its own link's tangent (flipped so it
    is never upside down), and the interface NAME only joins the always-on cost once the
    connection has screen room (zoom) or is selected — twelve routers of always-on 30-char
    horizontal names made the fit view unreadable."""
    assert "c.rotate(angle)" in OSPF
    assert "angle += Math.PI" in OSPF  # the upright flip
    assert "LABEL_FULL_PX" in OSPF
    assert "function connectionLabels(" in OSPF


def test_node_labels_flip_below_on_overlap() -> None:
    """054: co-sited/nearby node labels no longer garble into each other — a greedy pass flips a
    colliding label below its node."""
    assert "placedLabels" in OSPF
    assert "measureText" in OSPF


def test_status_overlay_never_eats_pointer_events() -> None:
    """054 regression guard: .ospf-status spans the whole canvas and its author display:flex
    overrides the UA [hidden] rule — without pointer-events:none it silently swallows every
    canvas click/drag/wheel (the dead-click bug the 053 field pass caught)."""
    block = THEME.split(".ospf-status")[1].split("}")[0]
    assert "pointer-events: none" in block


def test_parallel_links_between_one_pair_fan_out_on_curves() -> None:
    """053: two or three links can join the same router pair (formed + configured backups); each
    connection gets a perpendicular offset level and draws as a quadratic curve, so parallel
    links never overlap. Hit-testing samples the curves."""
    assert "PARALLEL_GAP" in OSPF
    assert "rebuildConnections" in OSPF
    assert "quadraticCurveTo" in OSPF
    assert "item.level = index - (group.length - 1) / 2" in OSPF
    assert "function connectionAt(" in OSPF and "qPoint(" in OSPF


def test_cost_tier_styling_is_named_constants_with_bare_value_labels() -> None:
    assert "const TIER_STYLE" in OSPF
    for tier in ("  1:", "  100:", "  200:", "  300:", "  1000:"):
        assert tier in OSPF, tier
    assert "TIER_NEUTRAL" in OSPF
    assert "function tierStyle(" in OSPF  # unknown tier → neutral
    # 053: labels are the bare cost values — no commentary like "deprioritized".
    for label in ("label: '1'", "label: '100'", "label: '200'", "label: '300'", "label: '1000'"):
        assert label in OSPF, label
    for commentary in ("wired", "60 GHz", "5 GHz", "deprioritized", "last-resort"):
        assert commentary not in OSPF, commentary


def test_legend_is_reachable_in_widget() -> None:
    assert "renderLegend" in OSPF
    assert "ospf-legend" in OSPF and ".ospf-legend" in THEME


def test_spf_emphasis_is_a_lookup_with_a_toolbar_root_selector() -> None:
    """053: re-rooting moved from node click to the toolbar's SPF-root selector (a click now
    opens the router's drill-down window); still a lookup, never a JS SPF."""
    assert "spfTree" in OSPF
    assert "edgeOnTree" in OSPF  # membership predicate spf[root][a]==b || spf[root][b]==a
    assert "ospf-root" in OSPF and ".ospf-root" in THEME
    assert "currentRoot = rootSelect.value" in OSPF  # re-root by lookup, never a JS SPF
    assert "meta.source_router" in OSPF  # default root = the snapshot's primary source


def test_backup_links_keep_full_colour_via_dash_not_opacity() -> None:
    """073 follow-up: SPF emphasis no longer dims off-tree/backup links with globalAlpha — canvas
    opacity blended the stroke with the dark background, so a backup link read as a different colour
    than its full-opacity legend swatch. Backups now keep FULL colour and are signalled by a dash
    (and thinner weight), so the legend and the screen agree; the legend documents the dash cue."""
    assert "const OFFTREE_DASH" in OSPF
    assert "setLineDash(onTree ? [] : OFFTREE_DASH)" in OSPF
    assert "globalAlpha = onTree ? 1 : 0.35" not in OSPF  # the colour-shifting edge dimming is gone
    assert "globalAlpha = active ? 1 : 0.5" not in OSPF   # standby cloud links no longer dimmed
    assert "dashed = backup" in OSPF  # the dash cue is named in the legend


def test_a_node_click_opens_the_router_drilldown() -> None:
    assert "ctx.openOspf?.(drag.node.identity ?? drag.node.router_id)" in OSPF
    assert "openOspfLint" in OSPF  # the toolbar's lint button opens the lint window


def test_ghost_classes_have_distinct_named_styles_and_connect_to_resolved_peers() -> None:
    assert "const GHOST_STYLE" in OSPF
    for cls in ("one-sided", "broken", "unpaired"):
        assert cls in OSPF, cls
    assert "setLineDash" in OSPF  # ghosts are dashed
    # Ghosts are dormant debt, ALL grey — no class may borrow a cost-tier color (a red
    # "broken" read as a cost-1000 link on the canvas). Broken is the loudest grey.
    assert "broken: { color: '#d1d5db'" in OSPF
    assert OSPF.count("#ef4444") == 1  # red belongs to the 1000 cost tier alone
    # 053: a ghost whose intended peer is known draws to BOTH ends; only peerless ghosts stub.
    assert "ghost.peer_router_id && nodeById.has(ghost.peer_router_id)" in OSPF
    assert "stubEnd" in OSPF


def test_spof_ring_bridge_marker_and_pseudonode_squares() -> None:
    assert "node.spof" in OSPF and "arc(x, y, NODE_RADIUS + 4" in OSPF
    assert "drawBridgeMarker" in OSPF and "edge.bridge" in OSPF
    assert "pseudonode" in OSPF and "fillRect" in OSPF  # pseudonodes as squares


def test_external_badges_and_default_route_panel() -> None:
    assert "is_asbr" in OSPF  # external-route badge on the injecting router
    assert "renderDefault" in OSPF
    # 073: the panel is per-root and structured (one line per route) — NOT the run-on narrative.
    assert "default_by_root?.[currentRoot]" in OSPF
    assert "ospf-default-line" in OSPF and ".ospf-default-line" in THEME
    assert "Next candidate" in OSPF and "Then" in OSPF  # active, then each standby on its own line
    assert "body.textContent = dr.narrative" not in OSPF  # the 052/053 paragraph is gone
    assert "conflict" in OSPF  # prominent conflict treatment
    assert ".ospf-default.conflict" in THEME


def test_default_panel_is_toggleable_and_reflects_the_selected_root() -> None:
    """073: the default panel is toggled by a `0.0.0.0/0` button and re-renders on re-root, so it
    always shows the SELECTED router's egress (US1, FR-004/005)."""
    assert "0.0.0.0/0" in OSPF and "kit-btn" in OSPF  # 076: the toggle is now a shared .kit-btn
    assert "defaultVisible" in OSPF
    # re-rooting re-renders the per-root panel (the root selector calls renderDefault()).
    block = OSPF.split("rootSelect.addEventListener('change'")[1].split("});")[0]
    assert "renderDefault()" in block


def test_legend_is_toggleable_left_of_fit_and_summarizes_the_map() -> None:
    """073: a `legend` button (immediately left of `fit`) shows/hides the legend, which now spells
    out every glyph in words (FR-012/013)."""
    # 076: the legend toggle is a shared .kit-btn now
    assert "legendButton.textContent = 'legend'" in OSPF
    assert "applyLegendVisibility" in OSPF and "legendVisible" in OSPF
    # button order: legend immediately before fit in the toolbar append.
    order = OSPF.split("toolbar.append(")[1].split(")")[0]
    assert order.index("legendButton") < order.index("fitButton")
    assert order.index("defaultButton") < order.index("legendButton")
    # the summary names the node/ring/ASBR/cloud meanings, not just cost swatches.
    for phrase in ("SPF root", "ASBR", "SPOF", "internet", "stale"):
        assert phrase in OSPF, phrase
    assert "ospf-swatch-dot" in OSPF and ".ospf-swatch-dot" in THEME


def test_internet_cloud_links_default_originators_with_per_root_emphasis() -> None:
    """073: a cloud glyph anchors one link per 0.0.0.0/0 originator; the SELECTED root's active
    egress link is emphasized and the rest dimmed as standby (US2, FR-008..011)."""
    assert "drawCloudAndLinks" in OSPF and "drawCloudGlyph" in OSPF
    assert "defaultAsbrIds" in OSPF  # only 0.0.0.0/0 originators reach the cloud
    assert "external.prefix === '0.0.0.0/0'" in OSPF
    assert "DEFAULT_LINK_ACTIVE" in OSPF and "DEFAULT_LINK_STANDBY" in OSPF
    assert "cloudPresent" in OSPF  # no default ⇒ no cloud
    assert "cloudAt" in OSPF and "drag = { cloud: true" in OSPF  # draggable like a node
    # fit-view includes the cloud, wherever it sits (default seed or dragged position)
    assert "if (cloudPresent()) { // 073: the internet cloud counts toward the fit extent" in OSPF


def test_export_layout_copies_the_blessed_document_shape() -> None:
    """086: the toolbar's export control copies the EFFECTIVE arrangement in the position-store
    shape the snapshot builder parses verbatim (contract ospf-layout.md §5) — built on the 049
    kit copy primitive, cloud included, __ui__ (browser UI state) never exported."""

    assert "createCopyButton(() => layoutDocument()" in OSPF
    assert "label: 'export layout'" in OSPF
    assert "function layoutDocument()" in OSPF
    assert "doc[node.router_id] = { x: round1(w.x), y: round1(w.y) }" in OSPF  # effective (worldOf)
    assert "doc[CLOUD_KEY] = { x: round1(cw.x), y: round1(cw.y) }" in OSPF  # cloud travels
    assert "JSON.stringify(sorted, null, 2)" in OSPF  # sorted keys, stable diffs
    assert "UI_KEY" not in OSPF[OSPF.index("function layoutDocument()"):
                                OSPF.index("function fitViewport()")]  # __ui__ never exported


def test_cloud_default_prefers_the_blessed_cloud_seed() -> None:
    """086: cloudWorld precedence = local drag → snapshot cloud_seed → computed centroid
    (contract ospf-layout.md §5); a pre-086 snapshot (no cloud_seed) falls through cleanly."""

    body = OSPF[OSPF.index("function cloudWorld()"):]
    drag = body.index("positions[CLOUD_KEY]")
    blessed = body.index("topology.cloud_seed")
    assert drag < blessed  # a local drag still wins over the blessed default


def test_panel_and_cloud_state_persist_in_the_existing_store() -> None:
    """073: cloud position and panel visibility persist under RESERVED keys inside the one
    sanctioned position store — no new localStorage owner (FR-011/018)."""
    assert "POSITIONS_KEY = 'obi-ospf-positions-v1'" in OSPF  # still the only store
    assert "UI_KEY = '__ui__'" in OSPF and "CLOUD_KEY = '__cloud__'" in OSPF
    assert "key.startsWith('__')" in OSPF  # prunePositions preserves reserved keys
    assert "loadUi" in OSPF and "saveUi" in OSPF
    # no second localStorage key is introduced — exactly one get + one set, both on POSITIONS_KEY.
    assert OSPF.count("localStorage.setItem(") == 1
    assert OSPF.count("localStorage.getItem(") == 1
    assert "localStorage.setItem(POSITIONS_KEY" in OSPF
    assert "localStorage.getItem(POSITIONS_KEY" in OSPF


def test_lint_is_a_separate_window_that_focuses_the_map() -> None:
    """053: the lint list mounts as its own window (params.view='lint', same module — no new
    served file); selecting a row fires a same-page event the map hears and centers on."""
    assert "mountLint" in OSPF
    assert "view === 'lint'" in OSPF  # mount dispatches on params.view
    assert "obi:ospf-focus" in OSPF  # the lint→map focus event
    assert "dispatchEvent" in OSPF and "addEventListener(FOCUS_EVENT" in OSPF
    assert "removeEventListener(FOCUS_EVENT" in OSPF  # released on unmount
    assert "focusFinding" in OSPF  # centers + highlights the subject (SC-006)
    assert "edge_id" in OSPF and "ghost_id" in OSPF  # focus by the finding's subject id
    # 076: the lint category is coloured by the canonical kit-c-warn/kit-c-muted tokens
    assert "lintSeverityToken(finding.severity)" in OSPF
    assert ".kit-c-warn" in THEME and ".kit-c-muted" in THEME
    assert ".ospf-lint-window" in THEME


def test_interaction_follows_viewer2d_idioms_plus_pinch() -> None:
    for idiom in ("pointerdown", "pointermove", "pointerup", "wheel", "ResizeObserver"):
        assert idiom in OSPF, idiom
    assert "pointers.size === 2" in OSPF  # 053: two-pointer pinch zoom for touch/headset
    assert "clearInterval" in OSPF  # returns an unmount() that releases timers
