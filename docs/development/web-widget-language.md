# The obi web widget UI language

*Normative. Feature 076. Read this before building web widget #21 — a widget that follows the
language here is consistent with the fleet by default, and the retrofit that produced this doc
(076) exists precisely because 22 widgets had drifted into divergent hand-rolled chrome.*

The rule of thumb: **presentation chrome lives in the kit** (`src/obi/web/static/kit.js` +
`theme.css`), the widget contributes only its data spec and its own content. If you find yourself
hand-rolling a panel, a toolbar, a button, a status line, an empty-state box, a tooltip, or a
key/value fact row, stop — there is a kit primitive or class for it. Add a new one only when the
audit truly finds no owner (that is how `.kit-btn`/`.kit-toolbar`/`.kit-message`/`.kit-facts` and
`createTooltip`/`createStatusLine`/`createRefreshButton` were born).

The one real-browser check before a field pass is the screenshot harness
(`tests/web_harness/shots/`, see §Verification) — the 075 invisible-faceplate lesson, mechanized.

---

## Primitives (kit.js)

### `createPanel(host, ctx, { modal, onClose }) → { open, close, isOpen, destroy }`

The ONE panel language: a floating (default) or modal quick-reference surface whose head rhymes
with `.win-title` (a panel reads as a small window). It owns the chrome — a title, an optional
pop-out button, a close ✕, and a scoped Escape — and you supply only the body node.

- **`open({ title, body, popOut })`** — `body` is an Element you built with `textContent` (the
  panel never interpolates data into markup). Re-calling `open()` while open replaces the content
  in place (position kept) — that is how a widget refreshes a pinned panel across a data poll,
  guarded by `isOpen()`.
- **Escape scoping** — while open, the panel holds exactly one document-level keydown listener.
  Escape closes it *only* when the event target is not a typing field
  (`kit.isTypingTarget`) AND the panel's host window is the focused one
  (`host.closest('.win')?.classList.contains('focused') ?? true`; a fullscreen `?w=` page has no
  `.win` and is always "focused"). Two windows with open panels → Escape closes only the focused
  one.
- **`modal: true`** — a dimmed backdrop over the host, click-to-dismiss (the long-form reading
  mode; the watcher report uses it). Non-modal (default) floats bottom-right and leaves the host
  content interactive.
- **`onClose`** — fires on any open→closed transition (✕ / Escape / backdrop / a programmatic
  `close()`), for a widget to reconcile its own selection state (deselect the port, the OSPF node).
  It must not itself call `close()`.
- **`destroy()`** is mandatory from the owning widget's `unmount()`.

### Pop-out (per `open()`) + the snapshot lifecycle

The pop-out button appears only in a real workspace window that has a pop-out path
(`ctx.mode === 'window'` AND a declared `popOut` OR `ctx.openSnapshot`). Its click:

- **`popOut: { open: () => ctx.openX(...) }`** — a thunk wrapping a bespoke live-window opener
  (`openOspf`/`openLink`/`openConfig`/`openRouter`/`openOspfLint`). Declare it when the panel's
  subject has a real window kind; the shell's `focusOrNull` gives focus-not-duplicate for free.
  Then the panel closes.
- **No `popOut`** → `ctx.openSnapshot(title, body)` opens an *ephemeral, inert snapshot* window: a
  deep clone of the body with every control disabled, `href`s stripped, ids stripped
  (`kit.inertClone`). A snapshot is minted `snap:<uuid>` with `ephemeral: true`, so
  `wm.serialize()` skips it — snapshots are never persisted and **vanish on reload** (belt and
  braces: `defForLayoutId('snap:…')` also returns null). A snapshot can never masquerade as live
  data.

### `ctx.mode` — never truthiness-test a fullscreen stub (D6)

A fullscreen `?w=` page's ctx carries **truthy no-op** `open*` stubs and **no** `openSnapshot` at
all. Branch on `ctx.mode === 'window'` (or test `ctx.openSnapshot`'s presence), never on the
truthiness of an opener. The bug this rule prevents: the fullscreen map's link tap once hit a
truthy no-op `openLink` and opened nothing.

### `createTooltip() → { show(text, event), move(event), hide(), destroy() }`

The one floating rich-hover surface (a single `.kit-tooltip` on `document.body`,
`pointer-events:none`, pointer-offset with a viewport-edge nudge). Use it for rich hovers on
SVG/canvas (the PHY faceplate). For table cells, keep the feed's native `title=` column mechanism —
native titles are right for single-value control hints; don't churn them into DOM tooltips.
`destroy()` from `unmount()`.

### `createStatusLine(host) → { set(text, tone), element }`

A one-line status readout (`.kit-status`, tone ∈ `ok|warn|down|muted`). `set(null)` hides it.
Replaces the phy/router `.ok/.warn/.down/.muted` lines.

### `createRefreshButton(onRefresh) → button`

A `⟳ refresh` `.kit-btn` that disables itself while the promise from `onRefresh()` is pending
(a slow read can't stack). `onRefresh` should return the read's promise.

### `createTabs(host, { tabs, persist, onActivate })`

A tab strip (`.kit-tabs` / `.kit-tab`). The active tab persists **per widget kind** under
`obi-web-tab:<persist>` — kit.js is the sanctioned owner of that key family, so all windows of a
kind share the last-selected tab (the 046 twin-window rule). `onActivate(id)` fires on init
(restoring the persisted tab) and on every switch.

### `createFilterRow(host, fields, onChange)` and `createCopyButton(getText, opts)`

The sanctioned filter row (`.kit-filters`) and the one copy control (`.kit-copy`); a feed also gives
you `csv` for free. Every text pane and table reuses `createCopyButton` — don't hand-roll clipboard
code.

### boards.js — `createBoard(host, ctx, spec)` and `labeledFields(row, columns)`

A snapshot/API board is a spec (source, columns, row mapping, counts, filters) over the 038 feed —
contribute the spec, nothing else. `labeledFields(row, columns)` is the honest visible projection
of a row (`label: value` lines, `_`-prefixed metadata excluded) that every right-click "ask obi"
describe hook reuses. `formatAge(iso)` is the **one** age formatter — import it, never re-implement
it (four copies were deduplicated in 076).

---

## Element-class standards (theme.css)

The standard for each element kind, and the retired anti-patterns by name. When in doubt, use the
kit class; a per-widget one-off is drift.

| Element | Standard | Retired anti-patterns |
|---|---|---|
| Panel / detail overlay | `kit.createPanel` | `boards.createDetailPanel` + `board-detail*`; the OSPF/`#detail2d` absolute overlays + their `head/close`; the un-dismissable `phy-inspector` card |
| Tooltip | `createTooltip` for SVG/canvas; native `title=` for control hints | `phy-tooltip` |
| Toolbar | `.kit-filters` (createFilterRow) / `.kit-tools` (feeds) / `.kit-tabs` (createTabs) / **`.kit-toolbar`** (a plain flex control row) | `phy-toolbar`, `router-toolbar`, `cfg`'s hand-rolled tab strip, `ospf-toolbar` button set, `audit-bar`, `nettool-form` layout |
| Button | `.kit-copy`/`.kit-csv`/`.kit-tab` + **`.kit-btn`** (the one plain text-button) + **`createRefreshButton`** | `phy-refresh`, `router-refresh`, `cfg-tab`, `.ospf-*-button`, `nettool-run`, `audit-back`, close-✕ duplicates |
| Severity | `kit-c-{down,warn,ok,muted}` via `severityColor` | the parallel `sev-warn`/`sev-info` lint vocabulary. **Sanctioned exceptions (NOT drift):** the PHY device-LED mimic, `bands.js HEALTH_COLORS`, the OSPF canvas tier/ghost palette, the conf diff markers, the chart palette |
| Status line | `.kit-state` feed banner + the window `.badge` + **`createStatusLine`** | `phy-status`, `router-status`, the `phy-state` clone, `ospf-status`, `met-state` |
| Empty / error (non-feed) | `showState('empty'\|'error')` for feeds; **`.kit-message`** (the one muted text box) for non-feed widgets | `cfg-message`, `audit-message`, `linkw-message`, `ospfr-clean` |
| Fact rows (k/v) | `boards.labeledFields` + **`.kit-facts`** (one label/value pair; the container decides flow — a wrapping header row or a stacked column) | `#detail2d`/`.map-detail .row`, `.linkw-body .row`, `router-fact`, `phy-fact`, `ospfr-fact`, `ospf-row` |
| Age text | `boards.formatAge` | the local copies in logs.js, met.js, ospf.js, viewer2d.js |

### The empty-state copy convention

A non-feed empty/error `.kit-message` states the situation **and the CLI action that fixes it** —
e.g. `"no OSPF snapshot yet — run: obi web snapshot (cron)"`. An empty state is never a blank pane.
Input-driven widgets (ping/trace/talk) render their input and stay content-blank until asked —
that is honest, not empty.

### The two persistences the language touches

The language adds no new localStorage owner. It touches two existing, sanctioned ones: the tab
choice (via `createTabs`, key family `obi-web-tab:*`, kit-owned) and the OSPF legend/default-panel
visibility (the `__ui__` key inside the OSPF widget's own position store). Snapshots are the only
"window" the language creates, and they are deliberately excluded from `obi-web-layout`.

---

## Verification

- **Node DOM-stub harnesses** (`tests/web_harness/*_headless.mjs`) test widget *logic* — the panel
  Esc guard, inert-ization, pop-out branch selection, a widget's mount/render. They cannot see a
  browser *layout* failure.
- **The screenshot harness** (`tests/web_harness/shots/`, `capture.py --label <name>`) renders every
  registered widget in real headless Chrome from synthetic fixtures, one PNG each, for human
  before/after review. Run `capture.py --only <id>` for a real-browser check of your widget before
  its field pass. This is the mechanized 075 lesson — a widget that passes every grep and DOM-stub
  test can still ship a blank canvas or an off-screen panel; only a real browser catches that.
