"""Contract tests: dynamic chat windows + the right-click "ask obi" menu (feature 056).

Asset-source assertions — the repo's no-build-frontend pattern (interaction is verified live per
quickstart.md). Each test pins a sentence from contracts/chat-windows.md, contracts/ask-obi-menu.md,
or contracts/turn-context.md so the registry/shell/kit/talk contracts cannot drift silently.
"""

from __future__ import annotations

import re

from obi.web.server import STATIC_DIR

REGISTRY = (STATIC_DIR / "registry.js").read_text(encoding="utf-8")
SHELL = (STATIC_DIR / "shell.js").read_text(encoding="utf-8")
VOICE = (STATIC_DIR / "voice.js").read_text(encoding="utf-8")
KIT = (STATIC_DIR / "kit.js").read_text(encoding="utf-8")
BOARDS = (STATIC_DIR / "boards.js").read_text(encoding="utf-8")
TALK = (STATIC_DIR / "widgets" / "talk.js").read_text(encoding="utf-8")
THEME = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")

# widgets covered by a describe hook + the shared kit annotation (FR-007 coverage v1)
COVERED_BOARDS = ("ros", "uisp", "unifi", "links", "sik", "nbx", "watch", "cfg", "audit", "conf")


# --- US1: dynamic talk windows (contracts/chat-windows.md) ------------------------------------


def test_registry_mints_talk_defs_not_catalog_windows() -> None:
    """Talk windows are dynamic (one per launch/seed), keyed by `talk:<uuid>`; the catalog `talk`
    entry stays a launch TEMPLATE, never a window id (the 047/051 minted-def pattern)."""

    assert "export const TALK_ID_PREFIX = 'talk:'" in REGISTRY
    assert "export function talkWidgetDef(" in REGISTRY
    assert "params: { seed }" in REGISTRY  # seed rides only on right-click-minted windows
    # the catalog list still carries the CHAT template but not the minted def
    catalog = REGISTRY.split("export const WIDGETS")[1].split("];", 1)[0]
    assert "id: 'talk'" in catalog and "mnemonic: 'CHAT'" in catalog
    assert "talkWidgetDef" not in catalog


def test_registry_resolves_talk_ids_and_migrates_legacy_talk() -> None:
    """defForLayoutId: `talk:<uuid>` → a fresh SEEDLESS def (chips never restored); a pre-056
    saved `'talk'` id → a minted uuid window (muscle memory, no migration code)."""

    defn = re.search(r"export function defForLayoutId\(id\).*?\n\}", REGISTRY, re.S)
    assert defn, "expected defForLayoutId()"
    body = defn.group(0)
    assert "id === 'talk'" in body and "talkWidgetDef(crypto.randomUUID())" in body
    assert "startsWith(TALK_ID_PREFIX)" in body
    # the restored branch reconstructs from the uuid alone — no seed argument
    restore = body.split("startsWith(TALK_ID_PREFIX)")[1].split("if (")[0]
    assert "talkWidgetDef(uuid)" in restore
    assert "seed" not in restore


def test_shell_chat_launch_always_opens_a_new_window() -> None:
    """FR-001: every CHAT launch mints a NEW window (never focus-or-reuse); openChatWindow is
    the single entry point shared by the launch paths and the right-click menu."""

    assert "function openChatWindow(" in SHELL
    assert "talkWidgetDef(crypto.randomUUID()" in SHELL
    # open() special-cases talk BEFORE focusOrNull (the one widget exempt from single-instance)
    open_fn = re.search(r"function open\(id\).*?\n  \}", SHELL, re.S).group(0)
    assert "def.id === 'talk'" in open_fn and "openChatWindow()" in open_fn


def test_shell_keeps_the_def_title_in_sync_for_dock_chips() -> None:
    """Minted-def title propagation (research R8): a rename updates win.def.title so a later
    minimize's dock chip shows the real title — the fix lives on the def object, not WM surgery."""

    set_name = re.search(r"setName:\s*\(name\)\s*=>\s*\{.*?\}", SHELL, re.S)
    assert set_name, "expected the shell's setName to update the def title"
    assert "win.def.title = name" in set_name.group(0)


def test_voice_arbiter_is_single_owner() -> None:
    """056 R3 (CaptureArbiter): the module keeps ONE recorder; ownership is an explicit token so
    exactly one control records at a time and a loser can name the owning window."""

    assert "let recordOwner = null" in VOICE
    acquire = VOICE.split("function acquireRecording")[1].split("function stopRecordingOwned")[0]
    assert "recordOwner && recordOwner !== token" in acquire  # a second owner is refused
    assert "recordOwner = token" in acquire
    assert "ownerLabel" in VOICE  # the losing window reads the owner's label


def test_voice_control_exposes_both_idioms_and_context() -> None:
    """The control surfaces the raw PTT primitives (XR), the tap-or-hold machine (2D), context
    attach/clear (US2), and the busy/hint states (US3) — data-model TalkControlState."""

    for symbol in ("holdStart", "holdEnd", "press", "release", "escapeCancel",
                   "attachContext", "clearContext", "isRecording"):
        assert symbol in VOICE, symbol
    assert "HOLD_THRESHOLD_MS" in VOICE  # the tap-vs-hold threshold, unified with MIN_HOLD_SECONDS
    for state in ("busy-elsewhere", "hint"):
        assert f'"{state}"' in VOICE, state


def test_talk_widget_auto_titles_once_and_reports_its_owner_label() -> None:
    """FR-005 + US3: a window auto-titles exactly once (seed subject, or the first question,
    truncated), and reports its live title as the mic ownerLabel."""

    assert "ownerLabel: () => windowTitle" in TALK
    assert "function maybeTitle(" in TALK
    assert "if (titled) return" in TALK  # titled exactly once
    assert "ctx.setName(windowTitle)" in TALK and "re: ${seed.subject}" in TALK  # seeded title
    assert "TITLE_MAX" in TALK


def test_talk_widget_focuses_its_ask_box_when_the_operator_opens_it() -> None:
    """FR-008, generalized: EVERY operator-opened chat window (mnemonic, menu, right-click) starts
    with the caret in the ask box — no click needed. A window rebuilt from a saved layout at boot
    is not an operator action, so the shell withholds autofocus and several restored chats cannot
    race for the keyboard."""

    assert "if (ctx.autofocus !== false) askInput.focus()" in TALK
    # the shell tells a window which it is: freshly opened (saved === null) vs restored
    open_fn = re.search(r"async function openWindow\(def, saved = null\).*?\n  \}", SHELL, re.S)
    assert open_fn, "expected the shell's openWindow()"
    assert "autofocus: saved === null" in open_fn.group(0)


def test_talk_widget_release_of_ownership_on_unmount() -> None:
    """Spec edge case: a window closed mid-record/mid-playback drops its capture (never
    auto-submits) and stops its playback; other windows are unaffected."""

    unmount = TALK.split("unmount()")[-1]  # the real handler (a voice-off stub uses the name too)
    assert "control.cancel()" in unmount
    assert "removeEventListener('keydown'" in unmount


# --- US2: the right-click "ask obi" menu + context providers (ask-obi-menu.md) ----------------


def test_kit_annotates_rows_generically_without_widget_knowledge() -> None:
    """Element resolution: kit's createFeed annotates every rendered row with {row, columns, rows}
    as a non-enumerable property — generic, composes no text; the kit stays widget-agnostic."""

    assert "contextFor" in KIT  # the describe hook is forwarded, not interpreted
    assert "defineProperty" in KIT  # non-enumerable annotation (never leaks into export/filter)
    assert "__obiContext" in KIT
    # the kit purity guarantee still holds — no widget names, routes, or composed context text
    lowered = KIT.lower()
    for forbidden in ("logs", "/api/", "'map'", "'talk'", "[window]", "[element]"):
        assert forbidden not in lowered, forbidden


def test_boards_forward_the_context_descriptor_generically() -> None:
    """The board driver forwards spec.contextFor to the feed and supplies a widget-agnostic
    default describe (subject from the identity column, labeled visible fields) — no tool names."""

    assert "contextFor" in BOARDS
    lowered = BOARDS.lower()
    for forbidden in ("uisp", "siklu", "unifi", "routeros", "'ros'"):
        assert forbidden not in lowered, forbidden


def test_shell_context_menu_bypasses_shift_and_excludes_chat_windows() -> None:
    """Gesture handling (ask-obi-menu.md): Shift+right-click → native menu; a right-click inside a
    chat window keeps native behavior; a covered window / background offers the ask item."""

    assert re.search(r"""addEventListener\(\s*['"]contextmenu['"]""", SHELL)
    assert "shiftKey" in SHELL  # Shift bypass → native menu (FR-006)
    assert re.search(r"""closest\??\.\(\s*['"]\.win['"]""", SHELL)  # window resolution
    assert "TALK_ID_PREFIX" in SHELL  # chat-window exclusion (native selection/copy)
    assert "openChatWindow(" in SHELL and "seed" in SHELL  # the item seeds a new chat


def test_shell_seed_is_bounded_and_labeled() -> None:
    """Seed composition: [window] + [element] blocks, truncated to the 4000-char transport cap
    with a VISIBLE marker (the chip shows exactly what will be sent — consent, FR-010)."""

    assert "[window]" in SHELL and "[element]" in SHELL
    assert "4000" in SHELL  # the visible truncation bound == MAX_CONTEXT_CHARS


def test_talk_chip_is_visible_removable_and_consumed_once() -> None:
    """The chip (data-model ContextAttachment): header subject, expandable body = the exact
    contextText, ✕ removes before sending; the first submit consumes it, later turns send none."""

    assert "renderChip" in TALK and "consumeChip" in TALK
    assert "control.attachContext(seed.contextText)" in TALK  # rides the first submit
    assert "control.clearContext()" in TALK  # ✕ removes the payload (FR-011)
    assert "talk-chip-body" in TALK and "seed.contextText" in TALK  # body == exactly what is sent


def test_covered_widgets_declare_a_context_describe_hook() -> None:
    """FR-007 coverage v1: every covered board/detail table + the log panes name their element
    via a contextFor / describe hook. Canvas widgets (MAP, OSPF, MET) declare none by design."""

    for widget in COVERED_BOARDS:
        source = (STATIC_DIR / "widgets" / f"{widget}.js").read_text(encoding="utf-8")
        assert "contextFor" in source, widget
    for pane in ("logs", "router"):
        source = (STATIC_DIR / "widgets" / f"{pane}.js").read_text(encoding="utf-8")
        assert "contextFor" in source, pane
    # canvas surfaces are explicitly out of scope — no describe hook
    for canvas in ("map", "ospf", "met"):
        source = (STATIC_DIR / "widgets" / f"{canvas}.js").read_text(encoding="utf-8")
        assert "contextFor" not in source, canvas


def test_menu_and_chip_styles_exist_in_the_theme() -> None:
    for selector in ("#ask-menu", ".talk-chip", ".talk-chip-body", ".talk-mic.listening",
                     ".talk-cancel"):
        assert selector in THEME, selector


def test_discard_button_matches_the_mic_size() -> None:
    """The ✕ discard control is the same size as the 🎤 it sits beside (no smaller sibling)."""

    def dims(selector: str) -> str:
        block = THEME.split(selector)[1].split("}")[0]
        return "".join(line for line in block.splitlines() if "width" in line or "height" in line)

    assert dims(".talk-mic {") == dims(".talk-cancel {")


def test_chat_has_unlisted_typed_aliases() -> None:
    """TALK and VOICE are honored when typed but are not their own palette rows (056 follow-up):
    an alias resolves to CHAT via widgetByMnemonic, and never appears as a WIDGETS entry."""

    # 096 added HLT → HEALTH to the same alias table; the 056 contract still pins CHAT's two.
    assert "MNEMONIC_ALIASES = { TALK: 'CHAT', VOICE: 'CHAT', HLT: 'HEALTH' }" in REGISTRY
    # widgetByMnemonic resolves aliases before the catalog lookup
    by_mn = re.search(r"export function widgetByMnemonic\(mnemonic\).*?\n\}", REGISTRY, re.S)
    assert by_mn and "MNEMONIC_ALIASES[wanted]" in by_mn.group(0)
    # the aliases are NOT catalog mnemonics (they must never clutter the listed palette)
    entries = re.search(r"export const WIDGETS = \[(.*?)\n\];", REGISTRY, re.S).group(1)
    assert "'TALK'" not in entries and "'VOICE'" not in entries


def test_ping_and_trace_open_multiple_windows() -> None:
    """PING/TRACE mint a fresh `ping:<uuid>`/`trace:<uuid>` window per launch (like CHAT), and a
    saved id round-trips through defForLayoutId — never single-instance focus-or-reuse."""

    assert "MULTI_INSTANCE_BASE_IDS = ['ping', 'trace']" in REGISTRY
    assert "export function mintedToolDef(" in REGISTRY
    open_fn = re.search(r"function open\(id\).*?\n  \}", SHELL, re.S).group(0)
    assert "MULTI_INSTANCE_BASE_IDS.includes(def.id)" in open_fn
    assert "mintedToolDef(def.id)" in open_fn
    defn = re.search(r"export function defForLayoutId\(id\).*?\n\}", REGISTRY, re.S).group(0)
    assert "MULTI_INSTANCE_BASE_IDS" in defn  # restored ping:/trace: ids resolve back


def test_launcher_does_not_annotate_open_windows() -> None:
    """The `(open)` marker is gone from both the suggestion list and the menu (056 follow-up)."""

    assert "(open)" not in SHELL
