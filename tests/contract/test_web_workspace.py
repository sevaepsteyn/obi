"""Contract tests: the terminal workspace (feature 037).

Asset-source assertions — the repo's established pattern for the no-build frontend (no DOM
or XR emulation in CI; interaction is verified live per specs/037-web-workspace/quickstart.md).
Each test pins a sentence from contracts/widget-contract.md or contracts/workspace-urls.md;
together they enforce the additive-widget invariant: registry, modules on disk, and the
server allowlist can never drift apart silently.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from obi.web.server import STATIC_DIR, _allowlist

INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SHELL = (STATIC_DIR / "shell.js").read_text(encoding="utf-8")
KIT = (STATIC_DIR / "kit.js").read_text(encoding="utf-8")
# The full-screen (?w=<id>) ctx object literal — from `mode: 'fullscreen'` to its closing `};`.
FULLSCREEN_CTX = SHELL.split("mode: 'fullscreen'")[1].split("};")[0]
WM = (STATIC_DIR / "wm.js").read_text(encoding="utf-8")
REGISTRY = (STATIC_DIR / "registry.js").read_text(encoding="utf-8")
THEME = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")


def registry_entries() -> list[dict[str, str]]:
    """The WIDGETS entries, parsed from source (pure-data literals by contract)."""

    entries = []
    for block in re.findall(r"\{(.*?)\}", REGISTRY.split("export const WIDGETS")[1], re.S):
        fields = dict(re.findall(r"(\w+):\s*'([^']*)'", block))
        if "id" in fields:
            entries.append(fields)
    return entries


# --- the registry is the one catalog (widget-contract.md) -------------------------------------


def test_registry_has_the_expected_widgets() -> None:
    entries = registry_entries()
    # 047: STAT renamed to ROS ("RouterOS fleet"); INV retired. 048: PING/TRACE appended.
    # 052: OSPF map appended. 053: its mnemonic is the protocol's own name (OSPF).
    # 088: RADIO (the walkie-talkie transcript board) appended before AUDIT.
    # 095: DASH (the status board) appended. 096: HEALTH (id hlt, fleet health) after CONF.
    assert [e["id"] for e in entries] == [
        "map", "talk", "logs", "uisp", "links", "ros", "sik", "unifi", "nbx", "watch",
        "met", "cfg", "hlt", "radio", "audit", "xr", "ping", "trace", "ospf", "dash"]
    assert [e["mnemonic"] for e in entries] == [
        "MAP", "CHAT", "LOGS", "UISP", "LINKS", "ROS", "SIKLU", "UNIFI", "NBX", "WATCH",
        "METRICS", "CONF", "HEALTH", "RADIO", "AUDIT", "XR", "PING", "TRACE", "OSPF", "DASH"]
    ids = {e["id"] for e in entries}
    assert "inv" not in ids and "stat" not in ids  # retired / renamed (047)


def test_registry_ids_and_mnemonics_are_unique() -> None:
    entries = registry_entries()
    ids = [e["id"] for e in entries]
    mnemonics = [e["mnemonic"] for e in entries]
    assert len(set(ids)) == len(ids)
    assert len(set(mnemonics)) == len(mnemonics)


def test_registry_render_capability_is_declared_in_domain() -> None:
    """The forward-design flag for the XR window host: every entry declares canvas|dom."""

    entries = registry_entries()
    assert all(e.get("render") in ("canvas", "dom") for e in entries), entries
    assert next(e for e in entries if e["id"] == "map")["render"] == "canvas"


def test_registry_is_pure_data() -> None:
    """Importing the catalog must never load widget code (boot stays cheap)."""

    assert "import " not in REGISTRY.split("//", 1)[0]
    assert not re.search(r"^import\b", REGISTRY, re.M)


def test_every_registry_module_exists_is_allowlisted_and_exports_mount(
    tmp_path: Path,
) -> None:
    """The additive-widget invariant: entry ⇒ module file ⇒ allowlist route, no drift."""

    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    for entry in registry_entries():
        relative = entry["module"].removeprefix("./")
        module_path = STATIC_DIR / relative
        assert module_path.is_file(), entry["module"]
        assert f"/{relative}" in routes, entry["module"]
        source = module_path.read_text(encoding="utf-8")
        assert re.search(r"export async function mount\(", source), entry["module"]


def test_registry_sizes_are_present() -> None:
    # 047: bound the count to the WIDGETS array literal — routerWidgetDef() below it also
    # declares defaultSize/minSize (the dynamic per-router window), which is not a catalog entry.
    body = REGISTRY.split("export const WIDGETS")[1].split("];")[0]
    assert body.count("defaultSize:") == len(registry_entries())
    assert body.count("minSize:") == len(registry_entries())


# --- the landing page is the shell (workspace-urls.md) ------------------------------------------


def test_index_is_the_workspace_shell() -> None:
    assert "<title>obi terminal</title>" in INDEX
    assert re.search(r"""['"]\./theme\.css['"]""", INDEX)
    assert re.search(r"""from\s+['"]\./shell\.js['"]""", INDEX)
    # the importmap survives: viewer.js resolves three.js locally for the XR widget's session
    assert "./vendor/three.module.min.js" in INDEX


def test_index_references_only_allowlisted_assets(tmp_path: Path) -> None:
    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    for target in re.findall(r"""(?:href|src|from ?)=?\s*['"](\./[^'"]+)['"]""", INDEX):
        assert f"/{target.removeprefix('./')}" in routes, target


def test_legacy_version_routes_redirect_to_widget_pages() -> None:
    """?version=2d → ?w=map and ?version=3d → ?w=xr (FR-023); unknown values fall through."""

    assert re.search(r"""get\(\s*['"]version['"]\s*\)""", SHELL)
    assert re.search(r"""===\s*['"]2d['"]""", SHELL)
    assert re.search(r"""===\s*['"]3d['"]""", SHELL)
    assert re.search(r"""location\.replace\(\s*['"]\?w=map['"]\s*\)""", SHELL)
    assert re.search(r"""location\.replace\(\s*['"]\?w=xr['"]\s*\)""", SHELL)


def test_fullscreen_host_reads_the_w_parameter() -> None:
    assert re.search(r"""get\(\s*['"]w['"]\s*\)""", SHELL)
    assert "bootFullscreen" in SHELL
    assert "mode: 'fullscreen'" in SHELL


def test_unknown_widget_page_lists_the_valid_ids() -> None:
    """?w=<bogus> must never be a blank page — it names the valid pages and links home."""

    assert "unknown widget" in SHELL
    assert "?w=${w.id}" in SHELL


def test_webxr_probe_stays_affirmative_only() -> None:
    """The 033 detection discipline survives in the shell: only an affirmative probe counts
    (it decides whether the default layout includes the XR window, FR-021)."""

    probe = re.search(r"async function immersiveSupported\(\).*?\n\}", SHELL, flags=re.S)
    assert probe, "expected the immersiveSupported() probe in shell.js"
    body = probe.group(0)
    assert "if (!navigator.xr) return false" in body
    assert "catch" in body and "return false" in body
    assert re.search(r"isSessionSupported\(\s*['\"]immersive-vr['\"]\s*\)", body)
    assert re.search(r"isSessionSupported\(\s*['\"]immersive-ar['\"]\s*\)", body)
    assert "immersiveSupported()" in SHELL.split("async function immersiveSupported")[1][10:], (
        "the probe result must feed the default layout"
    )


# --- window manager operations (US1) --------------------------------------------------------------


def test_wm_implements_the_window_operations() -> None:
    for operation in (
        "startDrag",
        "startResize",
        "minimize",
        "restoreFromMin",
        "toggleMaximize",
        "close",
        "snapTo",
        "zoneRect",
        "clampAll",
        "serialize",
    ):
        assert operation in WM, operation


def test_snapshot_windows_are_ephemeral_and_never_persisted() -> None:
    """076 US2: a pop-out snapshot mints a snap:<uuid> window flagged ephemeral, which wm.serialize
    excludes — so snapshots never enter obi-web-layout and vanish on reload (belt-and-braces:
    defForLayoutId returns null for unknown snap: ids, dropping even a stray entry on load)."""

    assert "SNAP_ID_PREFIX = 'snap:'" in REGISTRY
    assert "ephemeral: true" in REGISTRY  # the def carries the exclusion flag
    assert "'./widgets/snap.js'" in REGISTRY
    # wm.serialize (the sole persistence producer) filters ephemeral windows out.
    serialize_body = WM.split("function serialize()")[1].split("\n  }")[0]
    assert "def.ephemeral" in serialize_body and ".filter(" in serialize_body
    # the shell's openSnapshot deep-clones + inert-izes before minting (kit.inertClone), and the
    # fullscreen ctx deliberately has NO openSnapshot (the D6 truthy-stub lesson).
    assert "inertClone" in SHELL and "snapshotWidgetDef" in SHELL
    # No openSnapshot KEY/method in the fullscreen ctx (a comment may name it, but there is no
    # `openSnapshot:` property nor `openSnapshot()` stub — its ABSENCE is the contract, D6).
    assert "openSnapshot:" not in FULLSCREEN_CTX and "openSnapshot(" not in FULLSCREEN_CTX
    assert "openSnapshot: (subject, bodyNode)" in SHELL  # present on the workspace ctx


def test_wm_uses_pointer_events_with_capture() -> None:
    """One input path for mouse and touch — the viewer2d idiom, required on the Quest."""

    for marker in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert marker in WM, marker
    assert "setPointerCapture" in WM


def test_wm_snap_zones_cover_halves_and_quarters() -> None:
    for zone in ("'left'", "'right'", "'tl'", "'tr'", "'bl'", "'br'"):
        assert zone in WM, zone
    assert "snap-preview" in WM  # the translucent target overlay while dragging


def test_wm_clamps_windows_reachable() -> None:
    """FR-017: a title bar can never leave the workspace entirely."""

    assert "MIN_VISIBLE" in WM
    assert re.search(r"function clamp\(", WM)


def test_windows_respect_the_registry_minimum_size() -> None:
    assert "minSize" in WM


# --- launcher, both forms (US3) -------------------------------------------------------------------


def test_command_line_hotkey_guards_text_fields() -> None:
    """`/` focuses the command line — except while typing in an input/textarea/
    contentEditable (FR-014: TALK's ask box must win)."""

    assert re.search(r"""key\s*===\s*['"]/['"]""", SHELL)
    assert "isTypingTarget" in SHELL
    # 076/D2: the typing-target predicate (incl. the contentEditable check) is now defined ONCE in
    # kit.js and imported by shell.js — one definition shared with the panel's Esc guard.
    assert "isContentEditable" in KIT
    assert "import" in SHELL and "isTypingTarget" in SHELL


def test_command_line_resolves_mnemonics_and_suggests() -> None:
    assert "widgetByMnemonic" in SHELL
    assert "suggestions" in SHELL
    assert "unknown — available mnemonics:" in SHELL  # unmatched input is never a dead end


# 037's `test_menu_lists_the_registry_for_touch` lived here: it pinned FR-013, the "[+] menu is the
# Quest-browser touch path". Feature 077 SUPERSEDED that requirement by operator decision — the "+"
# launcher was dropped rather than relocated when the top bar was retired, and opening a widget is
# now the command line only (spec 077 FR-025 + Assumptions, which record the accepted headset
# trade-off). The inverse is pinned by `tests/contract/test_web_chrome_source.py`
# ::test_touch_launcher_button_is_gone; asserting it here too would be duplication, so this note is
# the trail instead. The Obi menu that replaced it is a window SWITCHER, not a launcher — its
# contract lives in `specs/077-shell-chrome/contracts/window-switcher.md` and is verified by
# `tests/contract/test_web_switcher_smoke.py`.


def test_launcher_presents_the_catalog_sorted_by_mnemonic() -> None:
    """The command palette lists commands alphabetically so operators can scan; registry.js keeps
    WIDGETS in hand-authored order (that ordering test is unchanged).

    077 note: LAUNCHER_WIDGETS outlived the "+" menu it was named for — it still feeds the palette's
    suggestions and the unknown-?w= help, which is why the constant survives. Since the widget gate
    it feeds them THROUGH launcherWidgets(), the gate-filtered view of the same sorted catalog.
    """

    assert re.search(
        r"LAUNCHER_WIDGETS = \[\.\.\.WIDGETS\]\.sort\(\(a, b\) =>"
        r" a\.mnemonic\.localeCompare\(b\.mnemonic\)\)",
        SHELL,
    )
    # every launcher render path funnels through the sorted, gate-filtered view, never raw WIDGETS
    assert "for (const w of WIDGETS)" not in SHELL
    assert "if (!q) return launcherWidgets();" in SHELL


def test_launcher_ranks_mnemonic_prefix_matches_first() -> None:
    """Front-first matching: a mnemonic-prefix match outranks an interior mnemonic/title match
    (`RA` lists RADIO before AUDIT, whose "audit tRAil" title merely contains it); ties keep the
    alphabetical catalog order (stable sort). The rank lives in registry.js — the one place the
    launcher decides what to show."""

    rank_fn = re.search(r"export function widgetQueryRank\(widget, query\).*?\n\}", REGISTRY, re.S)
    assert rank_fn, "expected registry.widgetQueryRank()"
    assert "mnemonic.startsWith(q)" in rank_fn.group(0)
    # rank refines, never widens, a match
    assert "widgetMatchesQuery(widget, q)" in rank_fn.group(0)
    # the shell's palette sorts by that rank
    matches_fn = re.search(r"function matches\(query\).*?\n  \}", SHELL, re.S)
    assert matches_fn and "widgetQueryRank" in matches_fn.group(0)
    assert re.search(r"\.sort\(\(\[a\], \[b\]\) => a - b\)", matches_fn.group(0))


# --- gated widgets: AUDIT + RADIO behind the unlisted enable/disable commands --------------------


def test_gate_flags_exactly_audit_and_radio() -> None:
    """`gated: true` in the registry hides a widget behind the shell's gate; exactly RADIO and
    AUDIT carry it (the registry stays pure data — the flag is the registry's whole part)."""

    catalog = REGISTRY.split("export const WIDGETS")[1].split("];")[0]
    gated = re.findall(r"mnemonic: '(\w+)',[^{}]*?gated: true", catalog, re.S)
    assert sorted(gated) == ["AUDIT", "RADIO"]


def test_gate_commands_are_unlisted_and_persist() -> None:
    """`enable`/`disable` are typed-only: they act on Enter (before any mnemonic resolution),
    never render as suggestion or menu rows (they are not registry mnemonics — the catalog test
    pins that list), and the gate persists per-browser under its own localStorage key."""

    assert "const GATE_KEY = 'obi-web-gated'" in SHELL
    hidden = re.search(r"function runHiddenCommand\(value\).*?\n  \}", SHELL, re.S)
    assert hidden, "expected the shell's runHiddenCommand()"
    assert "'ENABLE'" in hidden.group(0) and "'DISABLE'" in hidden.group(0)
    enter = SHELL.split("event.key === 'Enter'")[1]
    assert enter.index("runHiddenCommand") < enter.index("widgetByMnemonic")


def test_gate_covers_every_resolution_surface() -> None:
    """Hidden means hidden everywhere: palette matches, exact-mnemonic Enter, the unknown
    listing, the Obi menu's launcher section, the ?w= page, and layout restore all pass the one
    visibility predicate (or the launcherWidgets() view built from it)."""

    assert re.search(r"function widgetVisible\(def\)", SHELL)
    assert "widgetVisible(exact)" in SHELL  # Enter's exact-mnemonic path
    assert "if (def && widgetVisible(def)) await openWindow(def, entry);" in SHELL  # restore
    assert "if (!widgetVisible(def))" in SHELL  # the ?w= fullscreen page
    assert SHELL.count("launcherWidgets()") >= 4  # help text + 2 suggestion paths + menu section


def test_disable_closes_open_gated_windows() -> None:
    """`disable` removes, not just hides: any open gated window (snapshot pop-outs included —
    a snapshot wears its source window's mnemonic) is closed, so it also leaves the persisted
    layout; the transient toast is the commands' only visible trace."""

    hidden = re.search(r"function runHiddenCommand\(value\).*?\n  \}", SHELL, re.S)
    assert hidden
    assert "GATED_MNEMONICS.has(entry.mnemonic)" in hidden.group(0)
    assert "wm.get(entry.id)?.close()" in hidden.group(0)
    assert "#gate-toast" in THEME


def test_single_instance_open_or_focus() -> None:
    """FR-010: launching an open widget focuses it (restoring from the dock if minimized).
    047: the focus-or-open logic moved into focusOrNull() so router windows share it."""

    focus_fn = re.search(r"function focusOrNull\(id\).*?\n  \}", SHELL, flags=re.S)
    assert focus_fn, "expected the shell's focusOrNull()"
    assert "existing" in focus_fn.group(0)
    assert "restoreFromMin" in focus_fn.group(0)
    open_fn = re.search(r"function open\(id\).*?\n  \}", SHELL, flags=re.S)
    assert open_fn and "focusOrNull" in open_fn.group(0)  # catalog opens delegate to it too


# --- layout persistence (US4, workspace-urls.md schema) -------------------------------------------


def test_layout_key_and_version_are_the_contract_values() -> None:
    assert "const LAYOUT_KEY = 'obi-web-layout'" in SHELL
    assert "const LAYOUT_VERSION = 1" in SHELL


def test_the_sanctioned_localstorage_owners_are_the_only_users_in_the_bundle() -> None:
    """One persisted artifact per owner: widgets must not grow their own storage keys.
    (sessionStorage stays the XR widget's pre-existing pref channel — different store.)
    044 added bands.js (band visibility, specs/044-web-link-bands/contracts/viewer-bands.md);
    046 adds kit.js (per-widget-kind column widths under obi-web-cols:<id>,
    specs/046-web-table-polish/contracts/kit-table-polish.md) — widgets persist through the
    kit's persist option, never touching storage directly. 052 adds widgets/ospf.js (dragged
    node positions under obi-ospf-positions-v1, the 4th sanctioned owner)."""

    for asset in STATIC_DIR.glob("**/*"):
        if asset.suffix not in (".js", ".html") or "vendor" in asset.parts:
            continue
        source = asset.read_text(encoding="utf-8")
        if asset.name not in ("shell.js", "bands.js", "kit.js", "ospf.js"):
            assert "localStorage" not in source, asset.name


def test_ospf_widget_is_the_fourth_sanctioned_localstorage_owner() -> None:
    """052: the OSPF map persists dragged node positions under the contract key — the ONLY new
    client-side persistence, validated against the snapshot's node ids on load."""

    ospf = (STATIC_DIR / "widgets" / "ospf.js").read_text(encoding="utf-8")
    assert "obi-ospf-positions-v1" in ospf
    assert "localStorage" in ospf
    assert "prunePositions" in ospf  # stored ids not in the snapshot are dropped


def test_restore_validates_and_degrades_to_defaults() -> None:
    """FR-016: parse errors, wrong versions, unknown widgets — skipped or defaulted,
    never a blank workspace."""

    load = re.search(r"function loadLayout\(\).*?\n\}", SHELL, flags=re.S)
    assert load, "expected loadLayout()"
    body = load.group(0)
    assert "JSON.parse" in body and "return null" in body
    # 047: resolution now goes through defForLayoutId (unknown/retired ids like `inv` filtered,
    # `stat`→`ros` remapped, `ros:<address>` router windows reconstructed).
    assert "defForLayoutId" in body
    assert "Number.isFinite" in body
    assert "defaultLayout" in SHELL


def test_viewport_resize_reclamps_and_resaves() -> None:
    assert "clampAll" in SHELL


# --- widgets stay thin adapters (constitution III) ------------------------------------------------


def test_map_widget_wraps_the_2d_viewer() -> None:
    source = (STATIC_DIR / "widgets" / "map.js").read_text(encoding="utf-8")
    assert re.search(r"""from\s+['"]\.\./viewer2d\.js['"]""", source)
    assert "start2D" in source
    assert "dispose" in source  # close/reopen cycles must not leak the poll timers


def test_xr_widget_reuses_the_session_prefs_and_the_untouched_viewer() -> None:
    source = (STATIC_DIR / "widgets" / "xr.js").read_text(encoding="utf-8")
    for key in ("obi-ar-site", "obi-ar-eye"):
        assert key in source, key
    # 119: the AR/VR choice left this per-tab pair for a normal viewer preference, so it now
    # survives a tab close like every other viewer setting and has exactly ONE owner shared
    # with the in-session panel row. Both go through bands.js — the widget still touches no
    # storage itself (the owners rule above).
    assert "obi-ar-mode" not in source
    assert "loadBackdropOpaque" in source
    assert "saveBackdropOpaque" in source
    assert "startAR" in source
    assert "drawMap" not in source  # the setup page's debug map retired — MAP is that map


def test_no_widget_does_geodesy() -> None:
    for name in ("map.js", "talk.js", "xr.js"):
        source = (STATIC_DIR / "widgets" / name).read_text(encoding="utf-8")
        for forbidden in ("6371", "haversine", "toRadians", "Math.PI / 180"):
            assert forbidden not in source, f"{name} contains geodesy marker: {forbidden}"


# --- theme tokens are the styling contract (FR-007) -----------------------------------------------


def test_theme_defines_the_token_palette() -> None:
    for token in ("--bg:", "--panel:", "--border:", "--text:", "--muted:",
                  "--ok:", "--warn:", "--down:", "--accent:", "--font-mono:"):
        assert token in THEME, token


def test_theme_is_monospace_first() -> None:
    assert "ui-monospace" in THEME
    assert "font-family: var(--font-mono)" in THEME


def test_the_2d_viewer_styles_migrated_into_the_theme() -> None:
    """The inline index.html block moved here (research R10) — the MAP widget's detail
    panel, status box, and canvas keep their selectors."""

    for selector in ("#canvas2d", "#status2d", "#detail2d"):
        assert selector in THEME, selector
    assert selector_rule_count(INDEX) == 0


def selector_rule_count(html: str) -> int:
    """CSS rules remaining inline in the landing (should be none — theme.css owns style)."""

    style = re.search(r"<style>(.*?)</style>", html, re.S)
    return len(re.findall(r"\{", style.group(1))) if style else 0


# --- the widget kit + LOGS (feature 038, contracts/widget-kit.md) --------------------------------

KIT = (STATIC_DIR / "kit.js").read_text(encoding="utf-8")
LOGS_WIDGET = (STATIC_DIR / "widgets" / "logs.js").read_text(encoding="utf-8")


def test_kit_exports_the_contract_surface() -> None:
    for export in ("createFeed", "createFilterRow", "createPoller", "severityColor"):
        assert f"export function {export}(" in KIT, export


def test_kit_carries_nothing_widget_specific() -> None:
    """The stamping guarantee (FR-009): the kit knows no widget, no route, no mnemonic."""

    lowered = KIT.lower()
    for forbidden in ("logs", "/api/", "'map'", "'talk'", "'xr'"):
        assert forbidden not in lowered, forbidden


def test_kit_renders_data_as_text_not_markup() -> None:
    assert "textContent" in KIT
    assert "innerHTML" not in KIT.split("root.innerHTML")[1]  # only the static scaffold


def test_kit_poller_guards_against_overlap() -> None:
    assert "running" in KIT
    poller = KIT.split("export function createPoller")[1]
    assert "if (running" in poller


def test_kit_style_classes_exist_in_the_theme() -> None:
    for selector in (".kit-feed", ".kit-row", ".kit-filters", ".kit-state", ".kit-live",
                     ".kit-c-down", ".kit-c-warn"):
        assert selector in THEME, selector


def test_logs_widget_consumes_the_kit_and_only_its_route() -> None:
    assert re.search(r"""from\s+['"]\.\./kit\.js['"]""", LOGS_WIDGET)
    for symbol in ("createFeed", "createFilterRow", "createPoller", "fetchJson", "severityColor"):
        assert symbol in LOGS_WIDGET, symbol
    assert "/api/logs.json" in LOGS_WIDGET
    # 068: the raw fetch now lives in kit.fetchJson; the widget makes one call, to its own route.
    assert "fetch(" not in LOGS_WIDGET
    assert LOGS_WIDGET.count("fetchJson(") == 1  # one route via the shared kit fetch, nothing else


def test_logs_widget_does_no_timezone_math() -> None:
    """Timestamps are displayed by slicing the store's localized ISO strings."""

    for forbidden in ("getTimezoneOffset", "toLocaleString", "Intl."):
        assert forbidden not in LOGS_WIDGET, forbidden


def test_logs_widget_filters_match_the_api_vocabulary() -> None:
    for key in ("'source'", "'device'", "'severity'", "'q'", "'window'"):
        assert key in LOGS_WIDGET, key
    # feature 050: the device-syslog family option that 038 deferred ("a NOT-filter belongs in core,
    # later") — resolved by exclusion of the controller tags in logstore._filter_clauses. The option
    # value is the reserved word `syslog`, NOT `routeros` (the store tags rows by collector host).
    assert "'syslog'" in LOGS_WIDGET
    assert "'routeros'" not in LOGS_WIDGET


def test_logs_widget_unmount_releases_its_timers() -> None:
    unmount = LOGS_WIDGET.split("unmount()")[1]
    assert "poller.stop()" in unmount
    assert "clearInterval" in unmount


# --- the board driver + the boards (features 040/043) ---------------------------------------------

BOARDS = (STATIC_DIR / "boards.js").read_text(encoding="utf-8")
BOARD_WIDGETS = ("uisp", "links", "ros", "sik", "unifi", "nbx", "watch", "hlt")


def test_boards_driver_is_widget_agnostic() -> None:
    """The stamping guarantee: the driver knows no tool, no snapshot name, no mnemonic."""

    lowered = BOARDS.lower()
    for forbidden in ("uisp", "siklu", "unifi", "netbox", "routeros", "watch", "inventory",
                      "'ros'", "'nbx'"):
        assert forbidden not in lowered, forbidden
    assert "/snapshots/" not in BOARDS and "/api/" not in BOARDS  # sources come from specs


def test_board_widgets_are_stamp_sized() -> None:
    """A board module contributes ONLY its spec: no fetch/poll/feed machinery of its own."""

    for widget_id in BOARD_WIDGETS:
        source = (STATIC_DIR / "widgets" / f"{widget_id}.js").read_text(encoding="utf-8")
        assert re.search(r"""from\s+['"]\.\./boards\.js['"]""", source), widget_id
        assert "createBoard" in source, widget_id
        for forbidden in ("fetch(", "createPoller", "createFeed", "setInterval"):
            assert forbidden not in source, f"{widget_id}: {forbidden}"


def test_snapshot_boards_point_at_their_snapshot_routes(tmp_path: Path) -> None:
    # 043: LINKS merges TWO snapshots (the set source kind); every other board reads one.
    routes = _allowlist(STATIC_DIR, tmp_path / "links.json", tmp_path / "snapshots")
    for widget_id, snapshot in (("uisp", "uisp"), ("links", "uisp-links"), ("links", "siklu"),
                                ("ros", "status"), ("sik", "siklu"),
                                ("unifi", "unifi"), ("nbx", "netbox-drift")):
        source = (STATIC_DIR / "widgets" / f"{widget_id}.js").read_text(encoding="utf-8")
        assert f"/snapshots/{snapshot}.json" in source, widget_id
        assert f"/snapshots/{snapshot}.json" in routes, snapshot


def test_boards_driver_snapshot_set_is_generic_and_stale_honest() -> None:
    """043's multi-snapshot kind: per-source last-good, all-absent empty state, and the badge
    age = the OLDEST present source (merged feeds never look fresher than their staler half)."""

    assert "'snapshot-set'" in BOARDS
    set_body = BOARDS.split("function refreshSet")[1].split("async function refresh(")[0]
    assert "404" in set_body  # a missing file is that source absent, not an error
    assert "previous[key]" in set_body  # failed poll keeps that key's last-good
    assert "every((entry) => entry === null)" in set_body  # empty state only when ALL absent
    assert "sort()[0]" in set_body  # oldest generated_at wins the badge


LINKS_WIDGET = (STATIC_DIR / "widgets" / "links.js").read_text(encoding="utf-8")


def test_links_board_merges_both_vendors_and_degrades_per_source() -> None:
    """The backhaul board (043): one row-space for UISP wireless links + Siklu paired links,
    each vendor optional — an absent snapshot is named in the counts line, never an error."""

    assert "kind: 'snapshot-set'" in LINKS_WIDGET
    for vendor in ("uisp", "siklu"):
        assert f"payload.{vendor} ? " in LINKS_WIDGET, vendor  # rows survive the other's absence
        assert f"'{vendor}: no snapshot'" in LINKS_WIDGET, vendor  # the gap is named
    assert "state === 'up'" in LINKS_WIDGET  # siklu pair state feeds the cross-vendor counts
    assert "Math.min" in LINKS_WIDGET  # siklu signal = worst end


def test_local_store_boards_point_at_their_api_routes() -> None:
    watch = (STATIC_DIR / "widgets" / "watch.js").read_text(encoding="utf-8")
    assert "/api/watch.json" in watch
    hlt = (STATIC_DIR / "widgets" / "hlt.js").read_text(encoding="utf-8")
    assert "/api/health.json" in hlt  # 096: the fleet-health board


# --- MET + CFG (feature 041): the bespoke-UI widgets ------------------------------------------

CHART = (STATIC_DIR / "chart.js").read_text(encoding="utf-8")
MET = (STATIC_DIR / "widgets" / "met.js").read_text(encoding="utf-8")
CFG = (STATIC_DIR / "widgets" / "cfg.js").read_text(encoding="utf-8")
CONF = (STATIC_DIR / "widgets" / "conf.js").read_text(encoding="utf-8")  # 051: per-device window


def test_chart_is_widget_agnostic_and_gap_honest() -> None:
    lowered = CHART.lower()
    for forbidden in ("metrics", "configs", "/api/", "fetch("):
        assert forbidden not in lowered, forbidden
    assert "n > 0" in CHART  # gap buckets (n == 0) break the line, never interpolated
    assert "devicePixelRatio" in CHART
    assert "legend" in lowered  # >= 2 series always get a legend


def test_chart_uses_the_validated_fixed_order_palette() -> None:
    """Dataviz rule: categorical hues in fixed order, assigned to the entity, never cycled
    fresh per render — the color map persists across renders."""

    assert "SERIES_COLORS" in CHART
    assert "'#3987e5'" in CHART  # slot 1 of the validated dark categorical order
    assert "colorBySeries" in CHART


def test_met_widget_is_the_first_canvas_data_widget() -> None:
    entry = next(e for e in registry_entries() if e["id"] == "met")
    assert entry["render"] == "canvas"
    assert re.search(r"""from\s+['"]\.\./chart\.js['"]""", MET)
    assert "/api/metrics.json" in MET
    for metric in ("ifHCInOctets", "ifOutErrors", "cpuLoad", "memUsedKb"):
        assert metric in MET, metric


def test_met_interface_picker_is_a_store_fed_dropdown() -> None:
    """The iface filter offers what the readings actually name (a dropdown, not a guess-the-
    spelling text box) and hides for device-scalar metrics (cpu/memory have no ports)."""

    assert "{ key: 'interface', kind: 'select'" in MET
    assert "setOptions('interface'" in MET  # options learned from unfiltered responses
    assert "ifacesByDevice" in MET  # cached per device for quick switching
    assert "SCALAR_METRICS" in MET
    assert MET.count("setHidden('interface'") >= 2  # on change AND at mount


def test_met_series_render_busiest_first() -> None:
    """The chart caps drawn series: sorting by activity keeps a many-port router's live
    links visible instead of an arbitrary first-by-ifindex slice (the tamarack-internet-gw
    flat-chart bug)."""

    assert "sort((a, b) => b.activity - a.activity)" in MET


def test_met_displays_ops_units() -> None:
    """Traffic reads in bits/s (the MRTG/router-page convention), memory in bytes — a
    presentation transform; the store stays exact bytes/kB."""

    assert "factor: 8, unit: 'bits/s'" in MET
    assert "factor: 1024, unit: 'B'" in MET


def test_chart_measures_its_y_labels() -> None:
    """The left margin is measured from the widest tick label — never a fixed width that
    clips '104.43 Mbits/s' into invisibility; hover math shares the measured margin."""

    assert "measureText" in CHART
    assert CHART.count("plotLeft") >= 3  # set in draw, read by the pointermove hit-test


def test_kit_filter_row_supports_dynamic_selects() -> None:
    assert "setOptions(key, options, value)" in KIT
    assert "setHidden(key, hidden)" in KIT
    assert ".kit-filter[hidden]" in THEME  # inline-flex would otherwise defeat hidden


def test_cfg_widget_is_coverage_only_and_opens_config_windows() -> None:
    # 051: CFG shrank to the coverage board; a row click opens that device's config window
    # (widgets/conf.js). The device dropdown and the in-window diff/history moved out.
    assert "/api/configs.json" in CFG
    assert "textContent" in CFG and "innerHTML" not in CFG  # config content is never markup
    assert "onRowClick" in CFG and "ctx.openConfig" in CFG  # a row opens that device's window
    assert "createFilterRow" not in CFG  # the device dropdown is gone
    for gone in ("view=diff", "view=show", "diff-add"):
        assert gone not in CFG, gone  # the detail view lives in conf.js now


def test_conf_window_renders_history_and_diffs_as_text() -> None:
    # 051: the per-device config window holds what left cfg.js — history + diff, config as TEXT.
    assert re.search(r"export async function mount\(", CONF)
    assert "ctx.params" in CONF and "device" in CONF  # keyed by the device it was opened for
    assert "/api/configs.json" in CONF
    assert "textContent" in CONF and "innerHTML" not in CONF  # config content is never markup
    for marker in ("diff-add", "diff-del", "view=diff"):
        assert marker in CONF, marker


# --- table polish (feature 046, contracts/kit-table-polish.md) ------------------------------------


def kit_head_rule() -> str:
    return re.search(r"\.kit-head\s*\{([^}]*)\}", THEME).group(1)


def kit_row_rule() -> str:
    return re.search(r"\.kit-row\s*\{([^}]*)\}", THEME).group(1)


def test_kit_cols_is_the_one_template_writer() -> None:
    """US1: the template is set once on the feed root; head and rows consume the same var,
    and no per-element grid template remains in the kit."""

    assert "setProperty('--kit-cols'" in KIT
    assert "gridTemplateColumns" not in KIT
    assert "grid-template-columns: var(--kit-cols)" in kit_head_rule()
    assert "grid-template-columns: var(--kit-cols)" in kit_row_rule()


def test_kit_head_and_rows_resolve_ch_tracks_identically() -> None:
    """US1: ch tracks resolve against the grid container's font — the two containers must
    declare the SAME font-size (the smaller label look lives on the head spans)."""

    head_font = re.search(r"font-size:\s*([\d.]+rem)", kit_head_rule()).group(1)
    row_font = re.search(r"font-size:\s*([\d.]+rem)", kit_row_rule()).group(1)
    assert head_font == row_font
    assert re.search(r"\.kit-head\s*>\s*span\s*\{[^}]*font-size", THEME)


def test_kit_head_and_body_reserve_the_scrollbar_gutter() -> None:
    """US1: a body scrollbar must never skew rows relative to their headers."""

    assert "scrollbar-gutter: stable" in kit_head_rule()
    body_rule = re.search(r"\.kit-body\s*\{([^}]*)\}", THEME).group(1)
    assert "scrollbar-gutter: stable" in body_rule


def test_kit_sorting_cycles_and_compares_correctly() -> None:
    """US2: three-state cycle, numeric-aware stable comparator, empty values last."""

    assert "function cycleSort(" in KIT
    assert "sortKey = null; // third click: back to natural order" in KIT
    assert "aria-sort" in KIT
    assert "Number.isFinite(an) && Number.isFinite(bn)" in KIT  # numeric branch
    assert "localeCompare" in KIT and "numeric: true" in KIT  # natural text branch
    assert "aEmpty ? 1 : -1" in KIT  # empties last in both directions
    assert "column.sort ?? column.key" in KIT  # per-column raw sort-key opt-in


def test_kit_sorted_view_suspends_the_tail() -> None:
    """US2: follow (and its chip) suspend while a sort is active; eviction stays
    arrival-ordered regardless of the displayed order."""

    assert "if (sortKey !== null) return; // follow machinery is suspended" in KIT
    set_follow_body = KIT.split("function setFollow")[1].split("}")[0]
    assert "sortKey !== null" in set_follow_body  # chip stays hidden while sorted
    trim = KIT.split("function trimModel")[1].split("}")[0]
    assert "rows.splice(0" in trim


def test_logs_time_column_sorts_on_the_full_timestamp() -> None:
    """The displayed clock time is ambiguous across days — sort uses the raw ISO value."""

    assert "sort: 'fullTs'" in LOGS_WIDGET


CFG_SOURCE = (STATIC_DIR / "widgets" / "cfg.js").read_text(encoding="utf-8")


def test_kit_resize_handles_are_separate_from_the_sort_target() -> None:
    """US3: pointer-captured drag with a floor and a fr absorber; a drag never sorts."""

    handle = KIT.split("function makeHandle")[1].split("const headCells")[0]
    assert "setPointerCapture" in handle
    assert "stopPropagation" in handle
    assert "dblclick" in handle  # boundary double-click resets to spec defaults
    assert "MIN_COL_PX = 24" in KIT
    assert "'minmax(0, 1fr)'" in handle  # the LAST track absorbs slack, grid always fits
    assert "col-resize" in THEME


def test_kit_width_persistence_is_guarded_and_sig_checked() -> None:
    """US3: obi-web-cols:<id> records carry a column signature; stale or corrupt records
    are discarded, and storage failure degrades to session-only widths."""

    assert "obi-web-cols:" in KIT
    load = KIT.split("function loadTracks")[1].split("let tracks")[0]
    assert "raw?.sig === sig" in load
    assert "catch" in load
    save = KIT.split("function saveTracks")[1].split("const root")[0]
    assert "catch" in save
    assert "persist = null" in KIT  # opting out (no id) means session-only widths


def test_consumers_thread_their_persist_ids() -> None:
    """The kit stays widget-agnostic; ids come from the callers (boards via the mount
    context's widget kind, LOGS and CFG explicitly)."""

    assert "persist: ctx.id" in BOARDS
    assert "persist: 'logs'" in LOGS_WIDGET
    assert "persist: 'cfg'" in CFG_SOURCE
    assert re.search(r"id:\s*def\.id", SHELL)  # both mount contexts carry the widget kind


# --- ROS board + per-router live detail windows (feature 047) ---------------------------------

ROS = (STATIC_DIR / "widgets" / "ros.js").read_text(encoding="utf-8")
ROUTER = (STATIC_DIR / "widgets" / "router.js").read_text(encoding="utf-8")


def test_inv_and_stat_widgets_are_gone_ros_is_served(tmp_path: Path) -> None:
    """INV retired, STAT renamed to ROS: the module files and their allowlist routes follow."""

    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    assert not (STATIC_DIR / "widgets" / "inv.js").exists()
    assert not (STATIC_DIR / "widgets" / "stat.js").exists()
    assert "/widgets/inv.js" not in routes
    assert "/widgets/stat.js" not in routes
    assert "/widgets/ros.js" in routes
    assert "/widgets/router.js" in routes  # the per-router detail widget joins the allowlist


def test_ros_board_adds_model_and_memory_and_drills_down() -> None:
    """ROS reads the same status snapshot as STAT did, with richer columns and a row click that
    opens the router's live detail window."""

    assert "/snapshots/status.json" in ROS
    for key in ("'model'", "'mem'", "'cpu'", "'version'", "'uptime'"):
        assert key in ROS, key
    assert "onRowClick" in ROS
    assert "ctx.openRouter" in ROS  # the click drills into the router window


def test_router_widget_reads_the_live_route_and_uses_the_shared_primitive() -> None:
    """The detail widget fetches the ONE live device-read route and renders on the kit feed;
    it never persists its own storage and degrades on every non-ok outcome."""

    assert re.search(r"export async function mount\(", ROUTER)
    assert "/api/router.json?device=" in ROUTER
    assert "createFeed" in ROUTER  # interfaces + addresses on the 046 primitive
    assert "ctx.params" in ROUTER  # the address/identity to query
    # honest states + last-good retention (FR-008)
    for token in ("unreachable", "timeout", "unknown_device", "absent", "lastGood"):
        assert token in ROUTER, token
    assert "localStorage" not in ROUTER  # widths ride the kit's persist option, nothing bespoke


def test_registry_mints_router_defs_that_are_not_catalog_entries() -> None:
    """Router windows are dynamic (one per address), keyed by `ros:<address>`, and never appear
    in the launcher/menu catalog (data-model.md)."""

    assert "export function routerWidgetDef(" in REGISTRY
    assert "export function defForLayoutId(" in REGISTRY
    assert "ros:" in REGISTRY
    assert "id === 'stat' ? 'ros'" in REGISTRY  # a pre-047 STAT layout entry returns as ROS
    # the catalog list itself carries no router entry (it is minted on demand)
    assert "routerWidgetDef" not in REGISTRY.split("export const WIDGETS")[1].split("]", 1)[0]


def test_shell_opens_router_windows_by_click_and_by_command() -> None:
    """US2 + US3: a row click and `/ROS <name|ip>` both open a per-router window; the window
    ctx carries the query params and an openRouter capability; single-instance-by-address."""

    assert "function openRouterWindow(" in SHELL
    assert "routerWidgetDef(" in SHELL
    # 051 generalized the ROS-only command into deviceArg (ROS + CONF); ROS still opens a router.
    assert "function deviceArg(" in SHELL and "function parseCommand(" in SHELL
    assert "'ROS'" in SHELL  # ROS + arg = open that router
    assert "openRouter:" in SHELL  # threaded into the widget ctx
    assert "params: def.params" in SHELL
    assert "setName:" in SHELL  # a restored (address-keyed) window renames itself once read


def test_router_open_converges_name_and_ip_on_one_address_keyed_window() -> None:
    """A typed name/ip resolves to the canonical ADDRESS via the status snapshot (a local file —
    device-blind), so `/ROS core-gw-1`, `/ROS <ip>`, and a row click all open ONE window."""

    assert "function openRouterByToken(" in SHELL
    assert "function rosResolveMap(" in SHELL
    assert "/snapshots/status.json" in SHELL  # the name↔address source (not a device read)
    assert "openRouterByToken(token)" in SHELL  # the launcher resolves before opening
    # resolution keys by both address and identity; the opened window is address-keyed
    assert "match.address" in SHELL


def test_wm_supports_dynamic_window_rename() -> None:
    assert "setName(text)" in WM  # 047: a router window learns its identity from the read


def test_kit_feed_supports_opt_in_row_clicks() -> None:
    """The one primitive gains an opt-in row-click drill-down; rows in the body are separate
    from the header sort/resize targets, so clicks never cross-fire (kit contract)."""

    assert "onRowClick" in KIT
    assert "kit-row-clickable" in KIT
    assert "kit-row-clickable" in THEME


# --- PING / TRACE net-tool widgets (feature 048) ----------------------------------------------

NETTOOL = (STATIC_DIR / "nettool.js").read_text(encoding="utf-8")
PING_WIDGET = (STATIC_DIR / "widgets" / "ping.js").read_text(encoding="utf-8")
TRACE_WIDGET = (STATIC_DIR / "widgets" / "trace.js").read_text(encoding="utf-8")


def test_net_tool_widgets_and_helper_are_allowlisted(tmp_path: Path) -> None:
    """048: the two widgets AND the shared helper they import are served (no drift)."""

    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    for path in ("/widgets/ping.js", "/widgets/trace.js", "/nettool.js"):
        assert path in routes, path
        assert routes[path].is_file(), path


def test_net_tool_widgets_call_their_real_obi_tools() -> None:
    """048 FR-003/FR-004: each widget targets the /api/tool bridge with the EXACT tool name of
    the existing obi tool — never a reimplementation."""

    for source in (PING_WIDGET, TRACE_WIDGET):
        assert "../nettool.js" in source  # the widgets are thin wrappers over the shared form
    assert "routeros_ping" in PING_WIDGET
    assert "routeros_traceroute" in TRACE_WIDGET


def test_net_tool_helper_posts_to_the_bridge_and_reads_inventory() -> None:
    """048: the shared form POSTs {tool, args} to /api/tool and populates its source dropdown
    from the existing local-data inventory route."""

    assert "/api/tool" in NETTOOL
    assert "method: 'POST'" in NETTOOL
    assert "/api/inventory.json" in NETTOOL
    # Result/device text is rendered via textContent (data is never markup — the kit discipline).
    assert "innerHTML" not in _device_population(NETTOOL)


def _device_population(source: str) -> str:
    return source.split("function populateDevices")[1].split("function showError")[0]


def test_web_allowlist_matches_the_widget_tool_names() -> None:
    """048 contract §Invariants: the server-side allowlist is exactly the two net tools the
    widgets drive — the security boundary and the widgets can never silently diverge."""

    from obi.web.tool_api import WEB_TOOL_NAMES, validate_web_tools

    assert set(WEB_TOOL_NAMES) == {"routeros_ping", "routeros_traceroute"}
    validate_web_tools()  # every allowlisted name is a real, read-only obi tool


# --- no unservable module imports (regression: "Failed to fetch dynamically imported module") ---

# Every RELATIVE import in a served JS module resolves to a path; the browser will fetch that path
# as ES-module. If the target is not allowlisted, the fetch 404s and the *importing* module fails to
# load ("Failed to fetch dynamically imported module: <importer>") — even though the importer itself
# is served. This bit feature 048: `widgets/{ping,trace}.js` statically import `../nettool.js`, so a
# helper omitted from the allowlist (or a widget module missing from it) breaks the widget in the
# browser while existing tests (which only check each registry entry's OWN module) stay green.
_IMPORT_SPEC_RE = re.compile(
    r"""(?:import|export)[^;'"()]*?\bfrom\s*['"]([^'"]+)['"]"""  # `import|export … from '<spec>'`
    r"""|import\(\s*['"]([^'"]+)['"]\s*\)"""                      # `import('<literal>')`
)


def _module_specifiers(source: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _IMPORT_SPEC_RE.finditer(source)]


def test_every_served_module_import_is_allowlisted(tmp_path: Path) -> None:
    """Every relative import in a served JS module must itself be an allowlisted, on-disk file.

    Guards the code side of the 048 failure mode (a widget importing an un-allowlisted helper, or a
    module missing from the allowlist) — the only side a test can guard; a stale running server that
    predates the file is an operational restart concern. Bare specifiers (e.g. ``three``) resolve
    via the index importmap, not the allowlist, and are skipped."""

    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    problems: list[str] = []
    for url_path, target in routes.items():
        if not url_path.endswith(".js") or not target.is_file():
            continue
        for spec in _module_specifiers(target.read_text(encoding="utf-8")):
            if not spec.startswith(("./", "../")):
                continue  # bare specifier → importmap's job, not the allowlist
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(url_path), spec))
            if resolved not in routes:
                problems.append(f"{url_path} imports {spec!r} -> {resolved} (NOT allowlisted)")
            elif not routes[resolved].is_file():
                problems.append(f"{url_path} imports {spec!r} -> {resolved} (allowlisted, no file)")
    detail = "\n".join(problems)
    assert not problems, f"unservable module imports (would 404 the browser's import):\n{detail}"


# --- copy & CSV export (feature 049, contracts/kit-copy-export.md) --------------------------------

NETTOOL = (STATIC_DIR / "nettool.js").read_text(encoding="utf-8")
AUDIT_WIDGET = (STATIC_DIR / "widgets" / "audit.js").read_text(encoding="utf-8")
WATCH_WIDGET = (STATIC_DIR / "widgets" / "watch.js").read_text(encoding="utf-8")


def test_kit_exports_the_copy_and_export_surface() -> None:
    for export in ("createCopyButton", "rowsToMatrix", "toTSV", "toCSV", "downloadCsv"):
        assert f"export function {export}(" in KIT, export


def test_kit_copy_button_degrades_without_the_clipboard_api() -> None:
    """A pane/table copy must not throw where the Clipboard API is unavailable (FR-008)."""
    copy = KIT.split("export function createCopyButton")[1].split("\nexport function")[0]
    assert "navigator.clipboard" in copy  # primary path
    assert "getText()" in copy  # reads the LIVE content at click time, never a snapshot
    assert "execCommand('copy')" in KIT  # hidden-textarea fallback


def test_kit_csv_is_rfc4180() -> None:
    """toCSV must quote comma/quote/CR/LF cells and double embedded quotes (FR-011)."""
    csv = KIT.split("export function toCSV")[1].split("\nexport function")[0]
    assert r'/[",\r\n]/' in csv
    assert 'replace(/"/g, \'""\')' in csv


def test_kit_feed_exposes_copy_and_csv_controls() -> None:
    """Every table inherits copy (TSV) + CSV from the ONE primitive (FR-007/FR-010/FR-012)."""
    assert "kit-tools" in KIT
    assert "createCopyButton(" in KIT and "toTSV(rowsToMatrix(" in KIT
    assert "downloadCsv(" in KIT and "toCSV(rowsToMatrix(" in KIT
    assert "kit-csv" in KIT  # the CSV button


def test_copy_control_styles_exist_in_the_theme() -> None:
    for selector in (".kit-tools", ".kit-copy", ".kit-csv", ".kit-copied"):
        assert selector in THEME, selector


def test_text_panes_wire_the_shared_copy_primitive() -> None:
    """Each text pane wires the ONE shared copy helper, not bespoke clipboard code (FR-006/9).
    051: the config diff/full-config panes moved from cfg.js to the per-device conf.js window."""
    for name, source in (("nettool", NETTOOL), ("audit", AUDIT_WIDGET),
                         ("watch", WATCH_WIDGET), ("conf", CONF)):
        assert "createCopyButton" in source, name


def test_conf_full_config_viewer() -> None:
    """049 in the 051 conf window: diff/full-config toggle, a view=show read, an inline filter."""
    assert "view=show" in CONF and "&at=" in CONF
    assert "full config" in CONF  # the toggle label
    assert "cfg-filter" in CONF and "cfg-config" in CONF
    assert "createCopyButton" in CONF
    assert "createTabs" in CONF  # 076: the diff⇄full-config toggle is the shared kit tab strip
    for selector in (".kit-tab", ".cfg-filter", ".cfg-config"):
        assert selector in THEME, selector


# --- per-device config windows (feature 051, contracts/config-window.md) --------------------------


def test_registry_mints_config_window_defs_not_catalog_entries() -> None:
    """Config windows are dynamic (one per device), keyed by `conf:<device>`, and never appear in
    the launcher/menu catalog — the 047 router-window pattern applied to configs."""
    assert "export function configWidgetDef(" in REGISTRY
    assert "CONFIG_ID_PREFIX" in REGISTRY and "'conf:'" in REGISTRY
    assert "startsWith(CONFIG_ID_PREFIX)" in REGISTRY  # defForLayoutId restores conf:<device>
    assert "'./widgets/conf.js'" in REGISTRY
    # the catalog list itself carries no config-window entry (it is minted on demand)
    assert "configWidgetDef" not in REGISTRY.split("export const WIDGETS")[1].split("]", 1)[0]


def test_shell_opens_config_windows_by_click_and_by_command() -> None:
    """A CFG row click (ctx.openConfig) and `/CONF <device>` both open a per-device config window;
    keyed by identity so many coexist and a re-open focuses the existing one."""
    assert "function openConfigWindow(" in SHELL
    assert "configWidgetDef(" in SHELL
    assert "openConfig:" in SHELL  # threaded into the widget ctx
    assert "key === 'CONF'" in SHELL  # CONF + arg = open that device's config window
    assert "openConfigWindow(arg)" in SHELL


def test_config_window_module_is_served() -> None:
    """The new per-device config window module is registered in the server allowlist (else the
    browser's dynamic import 404s)."""
    routes = _allowlist(STATIC_DIR, Path("links.json"))
    assert "/widgets/conf.js" in routes
    assert routes["/widgets/conf.js"].name == "conf.js"


# --- OSPF map + per-router drill-down windows (feature 052, contracts/ospf-widget.md) ----------

OSPF = (STATIC_DIR / "widgets" / "ospf.js").read_text(encoding="utf-8")
OSPF_ROUTER = (STATIC_DIR / "widgets" / "ospf_router.js").read_text(encoding="utf-8")


def test_registry_mints_ospf_window_defs_not_catalog_entries() -> None:
    """OSPF drill-down windows are dynamic (one per router), keyed by `ospf:<device>`, and never
    appear in the launcher/menu catalog — the 047/051 pattern applied to OSPF."""
    assert "export function ospfWidgetDef(" in REGISTRY
    assert "OSPF_ID_PREFIX" in REGISTRY and "'ospf:'" in REGISTRY
    assert "startsWith(OSPF_ID_PREFIX)" in REGISTRY  # defForLayoutId restores ospf:<device>
    assert "'./widgets/ospf_router.js'" in REGISTRY
    # the catalog list carries the MAP entry but not the drill-down def (minted on demand).
    catalog = REGISTRY.split("export const WIDGETS")[1].split("];", 1)[0]
    assert "ospfWidgetDef" not in catalog
    assert "id: 'ospf'" in catalog and "mnemonic: 'OSPF'" in catalog  # 053: the protocol's name


def test_shell_opens_ospf_windows_by_click_and_by_command() -> None:
    """A map node click (ctx.openOspf) and `/OSPF <device>` both open a per-router OSPF window;
    keyed by identity so many coexist and a re-open focuses the existing one. 053: the command
    is the protocol's own name — bare OSPF launches the map, OSPF + arg opens the detail."""
    assert "function openOspfWindow(" in SHELL
    assert "ospfWidgetDef(" in SHELL
    assert "openOspf:" in SHELL  # threaded into the widget ctx
    assert "key === 'OSPF'" in SHELL  # OSPF + arg = open that router's OSPF window
    assert "openOspfByToken(" in SHELL  # resolves name/address/router-id via the snapshot
    assert "/snapshots/ospf.json" in SHELL  # the resolution source (device-blind, a local file)
    assert "openOspf() {}" in SHELL  # the full-screen page stubs the opener to a no-op


def test_ospf_lint_is_its_own_window_opened_from_the_map() -> None:
    """053: the lint list is a separate openable/closable window (single instance, layout-
    restorable) reached from the map's `lint` button — reusing widgets/ospf.js with
    params.view='lint' so no new served file (deploy needs no server restart)."""
    assert "OSPF_LINT_ID" in REGISTRY and "'ospf-lint'" in REGISTRY
    assert "export function ospfLintWidgetDef(" in REGISTRY
    assert "view: 'lint'" in REGISTRY  # same module, second mount mode
    assert REGISTRY.count("module: './widgets/ospf.js'") == 2  # the map entry + the lint def
    assert "id === OSPF_LINT_ID" in REGISTRY  # defForLayoutId restores the lint window
    assert "function openOspfLintWindow(" in SHELL
    assert "openOspfLint:" in SHELL  # threaded into the widget ctx (the map's lint button)
    assert "openOspfLint() {}" in SHELL  # the full-screen page stubs it
    catalog = REGISTRY.split("export const WIDGETS")[1].split("];", 1)[0]
    assert "ospfLintWidgetDef" not in catalog  # never a launcher/menu entry


def test_ospf_widget_modules_are_served() -> None:
    routes = _allowlist(STATIC_DIR, Path("links.json"))
    for path in ("/widgets/ospf.js", "/widgets/ospf_router.js"):
        assert path in routes and routes[path].is_file(), path


def test_ospf_map_renders_precomputed_analysis_only() -> None:
    """The map does ZERO routing logic (research R3): it looks up spf[root], never computes a
    shortest path; no geodesy; polls the one snapshot route."""
    assert "/snapshots/ospf.json" in OSPF
    assert "spf[" in OSPF or "topology.spf" in OSPF  # re-root is a lookup
    assert "edgeOnTree" in OSPF  # tree membership by predicate, not by pathfinding
    for forbidden in ("dijkstra", "Dijkstra", "tarjan", "Tarjan"):
        assert forbidden not in OSPF, forbidden
    for geodesy in ("6371", "haversine", "toRadians", "Math.PI / 180"):
        assert geodesy not in OSPF, geodesy


def test_ospf_map_styles_are_named_constants() -> None:
    """Cost-tier + ghost styling are contract-testable named constants (ospf-widget.md)."""
    assert "TIER_STYLE" in OSPF
    for tier in ("1:", "100:", "200:", "300:", "1000:"):
        assert tier in OSPF, tier
    assert "TIER_NEUTRAL" in OSPF  # unknown cost renders neutrally
    assert "GHOST_STYLE" in OSPF
    for cls in ("'one-sided'", "broken", "unpaired"):
        assert cls in OSPF, cls


def test_ospf_map_marks_spof_ghosts_externals_and_default() -> None:
    for token in ("node.spof", "node.is_asbr", "bridge", "pseudonode", "default_route", "conflict"):
        assert token in OSPF, token
    assert "openOspf" in OSPF  # node click drills down (US5)
    assert "focusFinding" in OSPF  # lint select-to-focus (SC-006)


def test_ospf_drilldown_reads_the_snapshot_only() -> None:
    """The drill-down filters the SAME ospf snapshot to one router — no new live device route."""
    assert re.search(r"export async function mount\(", OSPF_ROUTER)
    assert "ctx.params" in OSPF_ROUTER and "device" in OSPF_ROUTER
    assert "/snapshots/ospf.json" in OSPF_ROUTER
    assert "/api/" not in OSPF_ROUTER  # no live route
    assert "createFeed" in OSPF_ROUTER  # neighbors + interfaces on the shared primitive
    assert "localStorage" not in OSPF_ROUTER  # widths ride the kit's persist, nothing bespoke
    for section in ("neighbors", "interfaces", "lint"):
        assert section in OSPF_ROUTER, section
    assert "unknown router" in OSPF_ROUTER  # the standard unknown-device message


# --- LINK detail window (feature 058) ---------------------------------------------------------

LINK_WIDGET = (STATIC_DIR / "widgets" / "link.js").read_text(encoding="utf-8")
VIEWER2D = (STATIC_DIR / "viewer2d.js").read_text(encoding="utf-8")
BANDS = (STATIC_DIR / "bands.js").read_text(encoding="utf-8")


def test_registry_mints_link_window_defs_not_catalog_entries() -> None:
    """Per-link detail windows are dynamic (one per link), keyed by `link:<key>` (the endpoint-
    pair), and never appear in the launcher/menu — the 047/051/052 synthetic-window pattern
    applied to a LINK."""
    assert "export function linkWidgetDef(" in REGISTRY
    assert "LINK_ID_PREFIX" in REGISTRY and "'link:'" in REGISTRY
    assert "startsWith(LINK_ID_PREFIX)" in REGISTRY  # defForLayoutId restores link:<key>
    assert "'./widgets/link.js'" in REGISTRY
    assert "mnemonic: 'LINK'" in REGISTRY
    # the catalog list carries no LINK entry (minted on demand) and no LINK launcher mnemonic.
    catalog = REGISTRY.split("export const WIDGETS")[1].split("];", 1)[0]
    assert "linkWidgetDef" not in catalog
    assert "'LINK'" not in catalog


def test_link_identity_is_the_shared_endpoint_pair_key() -> None:
    """The MAP, the LINKS board, and the LINK window all key on bands.js linkKey — the sorted,
    normalized endpoint-identity pair — so a click from either surface converges on one window."""
    assert "export function linkKey(" in BANDS
    assert ".sort()" in BANDS  # order-independent (a link is the same from either end)
    assert "toLowerCase()" in BANDS  # normalized
    assert "linkKey" in VIEWER2D  # the MAP produces the key on a tap
    assert "linkKey" in LINK_WIDGET  # the window resolves the link by the key


def test_shell_opens_link_windows_click_only_no_command() -> None:
    """A MAP link tap (ctx.openLink) and a LINKS row click both open a per-link window, keyed so
    many coexist and a re-open focuses the existing one. Deliberately NO command-line launch —
    unlike ROS/CONF/OSPF there is no deviceArg branch (link names are impractical to type)."""
    assert "function openLinkWindow(" in SHELL
    assert "linkWidgetDef(" in SHELL
    assert "openLink:" in SHELL  # threaded into the widget ctx
    assert "openLink() {}" in SHELL  # the full-screen page stubs the opener to a no-op
    # no command resolves to a link window: deviceArg mints ROS/CONF/OSPF, never LINK.
    darg = SHELL.split("function deviceArg(")[1].split("\n  }", 1)[0]
    assert "openLinkWindow" not in darg
    assert "LINK" not in darg


def test_link_window_reads_snapshots_only_no_live_route() -> None:
    """The window filters links.json + terrain.json to one link — the same inputs the MAP panel
    renders — with no new live device route (constitution III)."""
    assert re.search(r"export async function mount\(", LINK_WIDGET)
    assert "ctx.params" in LINK_WIDGET and "key" in LINK_WIDGET
    assert "links.json" in LINK_WIDGET
    assert "terrain.json" in LINK_WIDGET  # the elevation profile, fetched once
    assert "/api/" not in LINK_WIDGET  # no live route
    assert "not in the current export" in LINK_WIDGET  # honest absence


def test_map_opens_the_link_window_but_full_screen_keeps_its_panel() -> None:
    """In a workspace the MAP hands viewer2d ctx.openLink (a link tap opens a real window); the
    full-screen ?w=map page passes none, so it keeps its in-canvas detail panel (unchanged)."""
    mapw = (STATIC_DIR / "widgets" / "map.js").read_text(encoding="utf-8")
    # 076 D6: gate on ctx.mode, NOT truthiness — the fullscreen ctx carries a truthy no-op openLink
    # stub, so passing it through opened NOTHING; passing null in fullscreen restores the panel.
    assert "ctx.mode === 'window' ? ctx.openLink : null" in mapw
    assert "openLink }" in VIEWER2D  # start2D destructures the opener
    assert "if (openLink) openLink(linkKey(" in VIEWER2D  # tap opens the window when available
    assert "else showDetail(best)" in VIEWER2D  # full-screen fallback: the in-canvas panel


def test_links_board_row_click_opens_the_link_window() -> None:
    """A LINKS-board row click opens that link's window via the endpoint-pair key stashed on the
    row; a row whose endpoints didn't resolve carries no key and simply doesn't open."""
    links = (STATIC_DIR / "widgets" / "links.js").read_text(encoding="utf-8")
    assert "onRowClick:" in links
    assert "ctx.openLink?.(" in links
    assert "_key" in links  # the endpoint-pair key rides the row as metadata (never displayed)
    assert "linkKey(" in links  # both vendors compute it from their endpoint identities


def test_link_widget_module_is_served() -> None:
    routes = _allowlist(STATIC_DIR, Path("links.json"))
    assert "/widgets/link.js" in routes and routes["/widgets/link.js"].is_file()
