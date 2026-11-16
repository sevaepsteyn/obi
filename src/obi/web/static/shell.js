// obi workspace shell (feature 037) — the terminal landing.
//
// Thin adapter discipline unchanged (constitution III): the shell arranges windows and
// launches widgets; every number it shows is read from the export (`generated_at`) or the
// wall clock. Boot modes (one HTML route, client-side selection — the 033 pattern):
//   ?w=<id>       -> full-screen single-widget host (no chrome; "the tool's own page")
//   ?version=2d   -> legacy (033) -> location.replace('?w=map')
//   ?version=3d   -> legacy (022-033) -> location.replace('?w=xr')
//   anything else -> the workspace (unknown values never dead-end)

import {
  MULTI_INSTANCE_BASE_IDS,
  TALK_ID_PREFIX,
  WIDGETS,
  configWidgetDef,
  defForLayoutId,
  linkWidgetDef,
  mintedToolDef,
  ospfLintWidgetDef,
  ospfWidgetDef,
  routerWidgetDef,
  snapshotWidgetDef,
  talkWidgetDef,
  widgetById,
  widgetByMnemonic,
  widgetQueryRank,
} from './registry.js';
import { CONTEXT_PROP, inertClone, isTypingTarget } from './kit.js';
import { createWindowManager } from './wm.js';

const LAYOUT_KEY = 'obi-web-layout';
const LAYOUT_VERSION = 1;
// The launcher (command palette + touch menu) presents the catalog alphabetically by mnemonic
// so operators can scan for a command; registry.js keeps WIDGETS in hand-authored order for
// everything else. Copy, don't mutate the shared export.
const LAUNCHER_WIDGETS = [...WIDGETS].sort((a, b) => a.mnemonic.localeCompare(b.mnemonic));

// --- gated widgets (registry `gated: true` — currently AUDIT + RADIO) ---------------------------
// Hidden from every launcher surface — palette suggestions, the unknown-mnemonic listing, the Obi
// menu's widget section, the unknown-?w= help — and from mnemonic/?w=/layout-restore resolution,
// until the operator types the UNLISTED `enable` command; `disable` re-hides them and closes any
// that are open. The two commands are deliberately absent from every list themselves: hidden means
// hidden, so the palette keeps answering "unknown" even while one is being typed. The gate is
// per-browser, persisted in localStorage beside the layout (the shell already owns that store).
const GATE_KEY = 'obi-web-gated';
const GATED_MNEMONICS = new Set(WIDGETS.filter((w) => w.gated).map((w) => w.mnemonic));

function gateEnabled() {
  try {
    return localStorage.getItem(GATE_KEY) === 'enabled';
  } catch (err) {
    return false; // storage denied ⇒ the gate stays closed (hidden is the safe default)
  }
}

function storeGate(on) {
  try {
    if (on) localStorage.setItem(GATE_KEY, 'enabled');
    else localStorage.removeItem(GATE_KEY);
  } catch (err) {
    /* storage denied: nothing to persist — the widgets simply stay hidden */
  }
}

// Every resolution path funnels through one visibility predicate; minted defs (ros:/conf:/…)
// carry no `gated` flag and always pass.
function widgetVisible(def) {
  return Boolean(def) && (def.gated !== true || gateEnabled());
}

// The launcher surfaces render THIS view, never LAUNCHER_WIDGETS raw: same sorted catalog,
// minus the gated entries while the gate is closed. Read live on every render, so an
// enable/disable takes effect without a reload.
function launcherWidgets() {
  return LAUNCHER_WIDGETS.filter((w) => widgetVisible(w));
}
const POLL_MS = 5 * 60 * 1000; // the export cadence every viewer shares (022)
const WINDOW_STATES = new Set(['normal', 'min', 'max', 'snapped']);
// 056: the transport cap for the right-click context part (== server MAX_CONTEXT_CHARS). The
// seed is truncated to it with a VISIBLE marker so the chip shows exactly what will be sent.
const SEED_CONTEXT_MAX = 4000;
// 056 FR-007: the data windows a right-click covers ("ask obi about THIS"). Others (canvas maps,
// charts, net-tool forms) still offer plain "ask obi" with window identity only; chat windows keep
// native behavior. CONF covers both the coverage board (cfg) and the per-device window (conf).
const COVERED_MNEMONICS = new Set([
  'LOGS', 'UISP', 'LINKS', 'ROS', 'SIKLU', 'UNIFI', 'NBX', 'WATCH', 'CONF', 'HEALTH', 'AUDIT', 'RADIO',
]);

// --- WebXR detection (ported verbatim from the 033 landing: affirmative-only) ----------------

async function immersiveSupported() {
  try {
    if (!navigator.xr) return false;
    const vr = await navigator.xr.isSessionSupported('immersive-vr').catch(() => false);
    const ar = await navigator.xr.isSessionSupported('immersive-ar').catch(() => false);
    return vr === true || ar === true;
  } catch (err) {
    return false;
  }
}

// --- shared helpers ---------------------------------------------------------------------------

function shellMessage(text, { error = false } = {}) {
  const box = document.createElement('div');
  box.className = 'shell-message' + (error ? ' error' : '');
  box.textContent = text;
  document.body.appendChild(box);
  return box;
}

async function mountWidget(def, host, ctx) {
  host.classList.add(`widget-${def.id}`);
  const module = await import(def.module);
  return module.mount(host, ctx);
}

// --- full-screen single-widget host (?w=<id>) ---------------------------------------------------

async function bootFullscreen(id) {
  const def = widgetById(id);
  // A gated widget's page answers exactly like an unknown one while the gate is closed (and its
  // row is absent from the help listing) — "removed" means removed from every surface.
  if (!widgetVisible(def)) {
    const box = shellMessage(
      `unknown widget "${id}" — valid pages:\n` +
        launcherWidgets().map((w) => `  ?w=${w.id}  (${w.mnemonic} — ${w.title})`).join('\n') + '\n\n',
      { error: true },
    );
    const back = document.createElement('a');
    back.href = './';
    back.textContent = 'open the workspace';
    box.appendChild(back);
    return;
  }
  document.title = `obi — ${def.title}`;
  const host = document.createElement('div');
  host.id = 'fullscreen-host';
  const badge = document.createElement('div');
  badge.id = 'fullscreen-badge';
  document.body.append(host, badge);
  const ctx = {
    mode: 'fullscreen',
    id: def.id, // the widget kind — lets shared machinery persist per-kind view state (046)
    params: def.params ?? null,
    autofocus: true, // the widget owns the whole page — there is no other caret to steal

    setTitle(suffix) {
      document.title = suffix ? `obi — ${def.title} — ${suffix}` : `obi — ${def.title}`;
    },
    setName() {}, // no title-bar to rename on the full-screen page
    setBadge(text) {
      badge.textContent = text ?? '';
    },
    openRouter() {}, // a full-screen page has no workspace to open a router window into
    openConfig() {}, // 051: likewise no workspace to open a config window into
    openOspf() {}, // 052: likewise no workspace to open an OSPF drill-down window into
    openOspfLint() {}, // 053: likewise no workspace to open the OSPF lint window into
    openLink() {}, // 058: no workspace here — the full-screen MAP keeps its in-canvas panel
    // 076: openSnapshot is DELIBERATELY ABSENT here (not a no-op stub). A fullscreen page has no
    // window manager to open a snapshot into, and a panel decides whether to show its pop-out by
    // testing `ctx.openSnapshot`'s presence + `ctx.mode` — the D6 lesson that a truthy no-op stub
    // is a trap. Its absence keeps the pop-out button hidden on the fullscreen page (FR-010).
    requestClose() {}, // a full-screen page has nothing to close into
  };
  try {
    const handle = await mountWidget(def, host, ctx);
    window.addEventListener('resize', () => handle?.onResize?.());
  } catch (err) {
    shellMessage(`the ${def.mnemonic} widget failed to start:\n${err}`, { error: true });
  }
}

// --- layout persistence (contracts/workspace-urls.md) --------------------------------------------

function loadLayout() {
  let raw = null;
  try {
    raw = localStorage.getItem(LAYOUT_KEY);
  } catch (err) {
    return null;
  }
  if (!raw) return null;
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    return null; // corrupt -> defaults, silently (FR-016)
  }
  if (!parsed || parsed.version !== LAYOUT_VERSION || !Array.isArray(parsed.windows)) return null;
  const windows = parsed.windows.filter(
    (entry) =>
      entry &&
      defForLayoutId(entry.widget) && // unknown/retired widgets (e.g. inv) are skipped; stat→ros
      [entry.x, entry.y, entry.w, entry.h].every(Number.isFinite) &&
      WINDOW_STATES.has(entry.state),
  );
  return { windows };
}

function defaultLayout(workspace, withXr) {
  // First visit (or unusable saved state): MAP + TALK side by side; the XR entry point
  // joins on headset browsers (FR-021 - surfaced where it can actually be used).
  const W = Math.max(workspace.clientWidth, 480);
  const H = Math.max(workspace.clientHeight, 320);
  const gap = 16;
  const talkW = Math.min(500, Math.floor(W * 0.4));
  const mapW = Math.min(Math.floor(W * 0.6), W - talkW - gap * 3);
  const mainH = Math.min(560, H - gap * 2);
  const windows = [
    { widget: 'map', x: gap, y: gap, w: mapW, h: mainH, state: 'normal', snapZone: null, restore: null },
    { widget: 'talk', x: gap * 2 + mapW, y: gap, w: talkW, h: mainH, state: 'normal', snapZone: null, restore: null },
  ];
  if (withXr) {
    windows.push({
      widget: 'xr',
      x: Math.max(gap, Math.floor(W / 2) - 210),
      y: Math.max(gap, Math.floor(H / 2) - 200),
      w: 420, h: Math.min(500, H - gap * 2),
      state: 'normal', snapZone: null, restore: null,
    });
  }
  return { windows };
}

// --- the workspace ---------------------------------------------------------------------------------

async function bootWorkspace() {
  document.title = 'obi terminal';
  // 077: one bar, at the bottom. Everything the operator needs is in it, left to right: the Obi
  // wordmark (its menu switches windows), the command line, the clock, the reachability indicator.
  // Nothing above the workspace — the strip a top bar used to hold is windows now.
  document.body.innerHTML = `
    <div id="workspace"></div>
    <div id="dock"></div>
    <footer id="cmdline">
      <button id="obi" type="button" title="open windows">Obi</button>
      <span class="prompt">&gt;</span>
      <input id="cmd" type="text" autocomplete="off" spellcheck="false"
             placeholder="type a mnemonic (MAP, CHAT, XR) — press / to focus" />
      <span class="seg" id="clock"></span>
      <span id="conn" title="whether this browser reached the obi server on its last probe — not a freshness signal; each widget wears its own age"></span>
    </footer>`;

  const workspace = document.getElementById('workspace');
  const dock = document.getElementById('dock');

  // --- persistence wiring (US4): debounced save on every WM mutation --------------------------
  //
  // `restoring` holds the write off while the boot loop rebuilds the saved layout. That loop
  // creates windows one at a time and awaits each mount, and creation now notifies — so without
  // the guard a mount slower than the debounce would let a HALF-REBUILT workspace overwrite the
  // saved one, and the windows not yet reached would be gone for good.
  let saveTimer = 0;
  let restoring = true;
  function writeLayout() {
    saveTimer = 0;
    try {
      localStorage.setItem(
        LAYOUT_KEY,
        JSON.stringify({ version: LAYOUT_VERSION, windows: wm.serialize() }),
      );
    } catch (err) {
      /* storage full/denied: the workspace still works, it just won't persist */
    }
  }
  function scheduleSave() {
    if (restoring) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(writeLayout, 250);
  }
  // A reload inside the debounce window would otherwise drop the change that prompted the save —
  // exactly the case this persistence exists for (open a window, reload, find it gone).
  window.addEventListener('pagehide', () => {
    if (saveTimer) {
      clearTimeout(saveTimer);
      writeLayout();
    }
  });

  const wm = createWindowManager(workspace, dock, {
    onMutate: scheduleSave,
    onClose: (win) => {
      try {
        win.handle?.unmount?.();
      } catch (err) {
        /* a widget that fails to unmount must not wedge the workspace */
      }
    },
  });

  // 056: map a window's root element → its record, so the right-click handler can resolve the
  // window under the pointer (its def + params) without any WM surgery. WeakMap = auto-cleaned.
  const winByEl = new WeakMap();

  async function openWindow(def, saved = null) {
    const win = wm.create(def, saved);
    winByEl.set(win.el, win);
    const ctx = {
      mode: 'window',
      id: def.id, // the widget kind — lets shared machinery persist per-kind view state (046)
      params: def.params ?? null, // 047: a router window's {address, identity}
      // Whether this window may take the keyboard on mount (CHAT focuses its ask box). True only
      // for a window the operator just opened; a layout rebuilt at boot (`saved`) never steals it.
      autofocus: saved === null,
      setTitle: (suffix) => win.setSuffix(suffix),
      // 056: keep the def's title in sync with a rename so a later minimize's dock chip shows the
      // real title (wm reads win.def.title at chip time). The fix lives on the minted def object,
      // not in WM surgery — only minted defs (talk:/ros:/conf:) ever call setName, and each is a
      // fresh per-open object, so a catalog widget's shared def is never mutated.
      setName: (name) => {
        win.setName(name);
        if (name) win.def.title = name;
      },
      setBadge: (text) => win.setBadge(text),
      openRouter: (address, identity) => openRouterWindow(address, identity),
      openConfig: (device) => openConfigWindow(device),
      openOspf: (device) => openOspfWindow(device),
      openOspfLint: () => openOspfLintWindow(),
      openLink: (linkKey, name) => openLinkWindow(linkKey, name),
      // 076: pop out a panel with no live window kind as an ephemeral, inert snapshot window.
      // Present ONLY on the workspace ctx — deliberately ABSENT from the fullscreen ctx (below),
      // so a panel checks `ctx.mode`/the function's presence, never a truthy no-op stub (the D6
      // lesson). openSnapshot deep-clones + inert-izes the body and mints a snap:<uuid> window.
      openSnapshot: (subject, bodyNode) => openSnapshotWindow(subject, bodyNode, def.mnemonic),
      requestClose: () => win.close(),
    };
    try {
      win.handle = await mountWidget(def, win.body, ctx);
    } catch (err) {
      win.body.textContent = `the ${def.mnemonic} widget failed to start: ${err}`;
    }
    return win;
  }

  function focusOrNull(id) {
    const existing = wm.get(id);
    if (!existing) return false;
    // Single instance (FR-010): launching an already-open window focuses it.
    if (existing.state === 'min') existing.restoreFromMin();
    else existing.focus();
    return true;
  }

  function open(id) {
    const def = widgetById(id);
    if (!def) return false;
    // 056: CHAT is exempt from single-instance — every launch (menu, mnemonic, suggestion row)
    // opens a NEW chat window bound to a fresh conversation (US1).
    if (def.id === 'talk') {
      openChatWindow();
      return true;
    }
    // PING/TRACE likewise open a fresh window each launch (several probes at once).
    if (MULTI_INSTANCE_BASE_IDS.includes(def.id)) {
      openWindow(mintedToolDef(def.id));
      return true;
    }
    if (focusOrNull(id)) return true;
    openWindow(def);
    return true;
  }

  // 056: the single entry point for opening a chat window — every CHAT launch path and the
  // right-click "ask obi" menu (US2) both come here. `seed` ({subject, contextText}) is present
  // only for a right-click-minted window; talk.js renders its chip and titles `re: <subject>`.
  function openChatWindow(seed = null) {
    return openWindow(talkWidgetDef(crypto.randomUUID(), seed));
  }

  // 047: open (or focus) a per-router detail window, keyed by device ADDRESS so many can coexist
  // and so every entry point (a row click, `/ROS <name>`, `/ROS <ip>`) converges on ONE window.
  function openRouterWindow(address, identity = '') {
    const addr = String(address || '').trim();
    if (!addr) return false;
    const def = routerWidgetDef(addr, identity);
    if (focusOrNull(def.id)) return true;
    openWindow(def);
    return true;
  }

  // 051: open (or focus) a per-device config-history window, keyed by device IDENTITY so many can
  // coexist and a row click, `/CONF <device>`, or a restored layout all converge on ONE window.
  // The token is the identity directly (configs are stored by identity — no address resolution).
  function openConfigWindow(device) {
    const id = String(device || '').trim();
    if (!id) return false;
    const def = configWidgetDef(id);
    if (focusOrNull(def.id)) return true;
    openWindow(def);
    return true;
  }

  // 052: open (or focus) a per-router OSPF drill-down window, keyed by device IDENTITY (the CONF
  // pattern — it reads the ospf snapshot, filtered to that router). A map node click, `/OSPF
  // <device>`, or a restored layout all converge on ONE window.
  function openOspfWindow(device) {
    const id = String(device || '').trim();
    if (!id) return false;
    const def = ospfWidgetDef(id);
    if (focusOrNull(def.id)) return true;
    openWindow(def);
    return true;
  }

  // 076: open an ephemeral snapshot window from a panel pop-out that has no live window kind. The
  // body is deep-cloned + inert-ized (kit.inertClone) so it can never masquerade as live data, and
  // the window is minted `snap:<uuid>` with ephemeral:true (wm.serialize skips it — gone on reload).
  // Always-new (uuid), like ping/trace: two snapshots of the same subject are both valid.
  function openSnapshotWindow(subject, bodyNode, mnemonic = null) {
    if (!bodyNode) return false;
    const rect = bodyNode.getBoundingClientRect?.() ?? null;
    const size = rect ? [rect.width, rect.height] : null;
    // The source window's mnemonic rides along so the snapshot reads "OSPF … — snapshot · HH:MM",
    // not an anonymous "SNAP …" (operator field-pass feedback).
    openWindow(snapshotWidgetDef(subject, inertClone(bodyNode), Date.now(), size, mnemonic));
    return true;
  }

  // 053: the OSPF lint window — single instance, opened from the map widget's `lint` button.
  function openOspfLintWindow() {
    const def = ospfLintWidgetDef();
    if (focusOrNull(def.id)) return true;
    openWindow(def);
    return true;
  }

  // 058: open (or focus) a per-link detail window, keyed by the endpoint-pair KEY so a MAP link
  // tap and a LINKS-board row click converge on ONE window and a restored layout rebuilds it.
  // No command-line launch (link names are impractical to type) — clicks only.
  function openLinkWindow(linkKey, name = '') {
    const key = String(linkKey || '').trim();
    if (!key) return false;
    const def = linkWidgetDef(key, name);
    if (focusOrNull(def.id)) return true;
    openWindow(def);
    return true;
  }

  // Resolve a typed token (identity, address, or router-id) to the canonical roster IDENTITY via
  // the ospf snapshot (a LOCAL file — the server stays device-blind), so `/OSPF core-gw-1`, `/OSPF
  // 192.0.2.1`, and a node click all open the same identity-keyed window. Absent snapshot / unknown
  // token ⇒ the raw token is used, and the drill-down widget reports it unknown.
  let ospfResolveCache = { at: 0, byKey: null };
  async function ospfResolveMap() {
    const FRESH_MS = 60 * 1000;
    if (ospfResolveCache.byKey && Date.now() - ospfResolveCache.at < FRESH_MS) {
      return ospfResolveCache.byKey;
    }
    const byKey = new Map();
    try {
      const response = await fetch('/snapshots/ospf.json', { cache: 'no-store' });
      if (response.ok) {
        const body = await response.json();
        for (const node of body.data?.nodes ?? []) {
          const identity = node.identity ?? node.router_id;
          if (!identity) continue;
          byKey.set(identity.toLowerCase(), identity);
          if (node.router_id) byKey.set(node.router_id.toLowerCase(), identity);
          if (node.address) byKey.set(node.address.toLowerCase(), identity);
        }
      }
    } catch {
      /* snapshot absent/unreachable: resolution falls back to the raw token */
    }
    ospfResolveCache = { at: Date.now(), byKey };
    return byKey;
  }

  async function openOspfByToken(token) {
    const raw = String(token || '').trim();
    if (!raw) return;
    const match = (await ospfResolveMap()).get(raw.toLowerCase());
    openOspfWindow(match || raw); // unknown to the snapshot ⇒ the widget's read reports it
  }

  // Resolve a typed token (a device NAME or an IP) to its canonical address, so `/ROS core-gw-1`
  // and `/ROS 192.0.2.5` open the same window a click on that row does. The name↔address map is
  // the status snapshot (a LOCAL file — the web server stays device-blind); absent snapshot ⇒ the
  // token is used as-is and the detail widget's own read resolves it (or reports it unknown).
  let rosResolveCache = { at: 0, byKey: null };
  async function rosResolveMap() {
    const FRESH_MS = 60 * 1000;
    if (rosResolveCache.byKey && Date.now() - rosResolveCache.at < FRESH_MS) {
      return rosResolveCache.byKey;
    }
    const byKey = new Map();
    try {
      const response = await fetch('/snapshots/status.json', { cache: 'no-store' });
      if (response.ok) {
        const body = await response.json();
        for (const device of body.data?.devices ?? []) {
          if (!device.address) continue;
          const entry = { address: device.address, identity: device.identity ?? device.address };
          byKey.set(device.address.toLowerCase(), entry);
          if (device.identity) byKey.set(device.identity.toLowerCase(), entry);
        }
      }
    } catch {
      /* snapshot absent/unreachable: resolution falls back to the raw token */
    }
    rosResolveCache = { at: Date.now(), byKey };
    return byKey;
  }

  async function openRouterByToken(token) {
    const raw = String(token || '').trim();
    if (!raw) return;
    const match = (await rosResolveMap()).get(raw.toLowerCase());
    if (match) openRouterWindow(match.address, match.identity);
    else openRouterWindow(raw, raw); // unknown to the snapshot; the widget's read resolves/reports it
  }

  // --- restore or default layout ---------------------------------------------------------------
  const layout = loadLayout() ?? defaultLayout(workspace, await immersiveSupported());
  try {
    for (const entry of layout.windows) {
      // 047: `defForLayoutId` maps a saved id to a def — catalog widgets (remapping the retired
      // `stat`→`ros`) and `ros:<address>` router windows reconstructed from their address.
      // A gated widget saved while enabled never resurrects while the gate is closed (belt-and-
      // braces: `disable` already closes its windows, so a saved layout normally has none).
      const def = defForLayoutId(entry.widget);
      if (def && widgetVisible(def)) await openWindow(def, entry);
    }
  } finally {
    // From here the workspace is the operator's again and every mutation persists. `finally`, so
    // a window that fails to build can never leave the whole session unable to save.
    restoring = false;
  }

  window.addEventListener('resize', () => {
    wm.clampAll(); // restored/parked windows must stay reachable (FR-017)
    scheduleSave();
  });

  // --- the bar's right end: clock + server reachability (FR-005; 077 moved it off the top) -------
  // Data freshness is each widget's own concern (MAP wears its age on the title-bar badge —
  // operator field feedback, 2026-07-09); the bar answers one question: can this browser
  // still reach the obi server? The probe is /voice.json — the one route that answers on
  // every server, export or no export, voice on or off.
  //
  // 077 renamed the success state "live" -> "connected". "live" read as a claim about the DATA,
  // which this probe knows nothing about; it only ever meant "the last probe got an answer". At a
  // 5-minute cadence it can also be up to POLL_MS stale — one more reason not to call it live.
  const clock = document.getElementById('clock');
  const conn = document.getElementById('conn');

  function tickClock() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    clock.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }

  async function pollConnection() {
    try {
      const response = await fetch('/voice.json', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      conn.textContent = '● connected';
      conn.className = 'connected';
    } catch (err) {
      conn.textContent = '○ server unreachable';
      conn.className = 'failing';
    }
  }

  tickClock();
  setInterval(tickClock, 1000);
  await pollConnection();
  setInterval(pollConnection, POLL_MS);

  // --- launcher, both forms (US3): command line + menu -------------------------------------------
  const cmd = document.getElementById('cmd');
  let suggestions = null;
  let activeIndex = 0;

  // A command is a mnemonic optionally followed by an argument: `ROS`, `ROS core-gw-1`,
  // `ROS 192.0.2.5` (US3). Only the mnemonic drives suggestion matching.
  function parseCommand(value) {
    const trimmed = value.trim();
    const i = trimmed.search(/\s/);
    if (i === -1) return { head: trimmed, arg: '' };
    return { head: trimmed.slice(0, i), arg: trimmed.slice(i + 1).trim() };
  }

  // A device command is a mnemonic + argument that opens a per-device window: `ROS <name|ip>`
  // (047) or `CONF <device>` (051). Returns the display bits + opener, or null when the head is
  // not a device command (a plain mnemonic launch).
  function deviceArg(value) {
    const { head, arg } = parseCommand(value);
    if (!arg) return null;
    const key = head.toUpperCase();
    if (key === 'ROS') {
      return { mn: 'ROS', label: `open router "${arg}"`, open: () => openRouterByToken(arg) };
    }
    if (key === 'CONF') {
      return { mn: 'CONF', label: `open config "${arg}"`, open: () => openConfigWindow(arg) };
    }
    if (key === 'OSPF') {
      // 053: the command is the protocol's own name — `OSPF <name|ip>` opens that router's
      // OSPF detail window (bare `OSPF` stays a plain mnemonic launch of the map).
      return { mn: 'OSPF', label: `open OSPF "${arg}"`, open: () => openOspfByToken(arg) };
    }
    return null;
  }

  // Front-first ordering: a mnemonic-prefix match outranks an interior mnemonic/title match
  // (`RA` → RADIO before AUDIT's "audit tRAil"); within a rank the alphabetical catalog order
  // holds (sort is stable). The gate applies first — a hidden widget never matches anything.
  function matches(query) {
    const q = parseCommand(query).head;
    if (!q) return launcherWidgets();
    return launcherWidgets()
      .map((w) => [widgetQueryRank(w, q), w])
      .filter(([rank]) => rank >= 0)
      .sort(([a], [b]) => a - b)
      .map(([, w]) => w);
  }

  // The gate's own commands (unlisted — see the module-level gate block): typed exactly, any
  // case. `enable` surfaces the gated widgets; `disable` re-hides them AND closes any open ones
  // (their snapshot pop-outs included — a snapshot wears its source window's mnemonic). Neither
  // ever renders as a suggestion, a menu row, or an "available mnemonics" entry, and the palette
  // keeps saying "unknown" while one is being typed — the only trace is the transient toast.
  function runHiddenCommand(value) {
    const head = parseCommand(value).head.toUpperCase();
    if (head !== 'ENABLE' && head !== 'DISABLE') return false;
    const on = head === 'ENABLE';
    storeGate(on);
    if (!on) {
      for (const entry of wm.list()) {
        if (GATED_MNEMONICS.has(entry.mnemonic)) wm.get(entry.id)?.close();
      }
    }
    const names = [...GATED_MNEMONICS].sort().join(' + ');
    gateToast(on ? `${names} enabled` : `${names} hidden`);
    return true;
  }

  let gateToastEl = null;
  let gateToastTimer = 0;
  function gateToast(text) {
    gateToastEl?.remove();
    gateToastEl = document.createElement('div');
    gateToastEl.id = 'gate-toast';
    gateToastEl.textContent = text;
    document.body.appendChild(gateToastEl);
    clearTimeout(gateToastTimer);
    gateToastTimer = setTimeout(() => {
      gateToastEl?.remove();
      gateToastEl = null;
    }, 2500);
  }

  function hideSuggestions() {
    suggestions?.remove();
    suggestions = null;
  }

  function renderSuggestions(found, { unknown = false } = {}) {
    hideSuggestions();
    suggestions = document.createElement('div');
    suggestions.id = 'suggestions';
    const darg = deviceArg(cmd.value);
    if (darg) {
      // `/ROS <token>` or `/CONF <device>`: one actionable suggestion — open that device's window.
      const row = document.createElement('div');
      row.className = 'row active';
      row.innerHTML = `<span class="mn"></span><span class="ti"></span>`;
      row.querySelector('.mn').textContent = darg.mn;
      row.querySelector('.ti').textContent = darg.label;
      row.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        launchDevice(darg);
      });
      suggestions.appendChild(row);
      document.body.appendChild(suggestions);
      return;
    }
    if (unknown) {
      const none = document.createElement('div');
      none.className = 'none';
      none.textContent = 'unknown — available mnemonics:';
      suggestions.appendChild(none);
      found = launcherWidgets();
      activeIndex = -1;
    }
    found.forEach((w, index) => {
      const row = document.createElement('div');
      row.className = 'row' + (index === activeIndex ? ' active' : '');
      row.innerHTML = `<span class="mn"></span><span class="ti"></span>`;
      row.querySelector('.mn').textContent = w.mnemonic;
      row.querySelector('.ti').textContent = w.title;
      row.addEventListener('pointerdown', (event) => {
        event.preventDefault(); // keep the input focused
        launch(w);
      });
      suggestions.appendChild(row);
    });
    document.body.appendChild(suggestions);
  }

  function launch(def) {
    open(def.id);
    cmd.value = '';
    hideSuggestions();
    cmd.blur();
  }

  function launchDevice(darg) {
    darg.open(); // ROS → resolve name/ip then open; CONF → open by identity
    cmd.value = '';
    hideSuggestions();
    cmd.blur();
  }

  cmd.addEventListener('input', () => {
    activeIndex = 0;
    const found = matches(cmd.value);
    if (found.length) renderSuggestions(found);
    else renderSuggestions([], { unknown: true });
  });
  cmd.addEventListener('focus', () => {
    activeIndex = 0;
    renderSuggestions(matches(cmd.value));
  });
  cmd.addEventListener('blur', () => setTimeout(hideSuggestions, 120));
  cmd.addEventListener('keydown', (event) => {
    const found = matches(cmd.value);
    const darg = deviceArg(cmd.value);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (darg) return; // the device-open suggestion is the only choice — nothing to page through
      const step = event.key === 'ArrowDown' ? 1 : -1;
      activeIndex = (activeIndex + step + found.length) % Math.max(found.length, 1);
      renderSuggestions(found);
    } else if (event.key === 'Enter') {
      // The unlisted gate commands win first — they are not mnemonics and never suggest.
      if (runHiddenCommand(cmd.value)) {
        cmd.value = '';
        hideSuggestions();
        cmd.blur();
        return;
      }
      if (darg) {
        launchDevice(darg);
        return;
      }
      // An exact mnemonic only resolves while visible: a gated widget's mnemonic is unknown
      // until the operator enables the gate (found is already gate-filtered by matches()).
      const exact = widgetByMnemonic(parseCommand(cmd.value).head);
      const chosen = (widgetVisible(exact) ? exact : null) ?? found[Math.max(activeIndex, 0)] ?? null;
      if (chosen) launch(chosen);
      else renderSuggestions([], { unknown: true });
    } else if (event.key === 'Escape') {
      cmd.value = '';
      hideSuggestions();
      cmd.blur();
    }
  });

  // `/` focuses the command line from anywhere — except while typing in a text field
  // (FR-014: TALK's ask box, XR's eye-height input, the command line itself).
  window.addEventListener('keydown', (event) => {
    if (event.key === '/' && !isTypingTarget(document.activeElement)) {
      event.preventDefault();
      cmd.focus();
      cmd.select();
    }
  });

  // --- the Obi menu: switch to any open window (077) + open any widget (078 round 5) -------------
  //
  // The wordmark is the button. It answers "what do I have open?" and switches to the answer —
  // and, since the 2026-07-17 headset pass, it also carries the predicted cheap fix for the
  // retired "+" launcher: a second section listing LAUNCHER_WIDGETS, because a headset/touch
  // user otherwise has NO tap path to open a widget their layout doesn't already hold (the Quest
  // browser persists its own layout — widgets present on desktop were simply never openable
  // there). Launch goes through the same open() the command line uses, so singleton-focus and
  // CHAT/tool multi-instance semantics stay identical.
  //
  // wm owns the switcher semantics (contracts/window-switcher.md): this renders wm.list() and
  // calls wm.activate(). It never touches a window object, so it cannot get restore-vs-focus
  // wrong.
  const obiButton = document.getElementById('obi');
  let menu = null;
  function hideMenu() {
    menu?.remove();
    menu = null;
  }
  function menuRow(entry) {
    const item = document.createElement('button');
    item.type = 'button';
    if (entry.minimized) item.className = 'min';
    item.innerHTML = `<span class="mn"></span><span class="ti"></span>`;
    item.querySelector('.mn').textContent = entry.mnemonic;
    // A minimized window is open but off-screen; say so, in place — the operator's positional
    // memory of the list is worth more than grouping them (FR-018). The word carries the meaning
    // and the class only tones it: the marking must not be color-only.
    item.querySelector('.ti').textContent = entry.minimized
      ? `${entry.title} — minimized`
      : entry.title;
    item.addEventListener('click', () => {
      wm.activate(entry.id);
      hideMenu();
    });
    return item;
  }
  obiButton.addEventListener('click', (event) => {
    event.stopPropagation();
    if (menu) {
      hideMenu();
      return;
    }
    menu = document.createElement('div');
    menu.id = 'menu';
    // Built fresh on every open (FR-014/FR-023): whatever opened, closed, renamed or minimized
    // since last time is simply reflected — no bookkeeping, no stale entry to invalidate.
    const entries = wm.list();
    if (entries.length === 0) {
      const none = document.createElement('div');
      none.className = 'none';
      none.textContent = 'no open windows';
      menu.appendChild(none);
    } else {
      for (const entry of entries) menu.appendChild(menuRow(entry));
    }
    // The launcher section (078 round 5): every catalog widget, tap to open — through the
    // gate-filtered view, so a hidden widget has no tap path either.
    const sect = document.createElement('div');
    sect.className = 'sect';
    sect.textContent = 'open a widget';
    menu.appendChild(sect);
    for (const w of launcherWidgets()) {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'launch';
      item.innerHTML = `<span class="mn"></span><span class="ti"></span>`;
      item.querySelector('.mn').textContent = w.mnemonic;
      item.querySelector('.ti').textContent = w.title;
      item.addEventListener('click', () => {
        open(w.id);
        hideMenu();
      });
      menu.appendChild(item);
    }
    document.body.appendChild(menu);
  });
  window.addEventListener('pointerdown', (event) => {
    if (menu && !menu.contains(event.target) && event.target !== obiButton) hideMenu();
  });

  // --- right-click "ask obi about this" (056 US2) ----------------------------------------------
  // One workspace-wide contextmenu listener (research R7): Shift bypasses to the native menu;
  // inputs and chat windows keep native selection/copy; a covered data window seeds a chat with a
  // visible, removable context chip (window identity + the clicked row/log line); background opens
  // a plain chat. The shell is the identity authority (it owns windows + defs); kit annotations
  // (widget-agnostic) carry the clicked element, the widget's describe hook gives it meaning.

  function formatParams(params) {
    if (!params) return '';
    return Object.entries(params)
      .filter(([key, value]) => key !== 'seed' && value != null && value !== '')
      .map(([key, value]) => `${key}=${value}`)
      .join(', ');
  }

  function resolveElement(node, winEl) {
    // Nearest ancestor (within this window) carrying a kit context annotation → its described
    // {subject, elementText}. No annotation ⇒ window-level context only (FR-012).
    let el = node;
    while (el && winEl.contains(el)) {
      const annotation = el[CONTEXT_PROP];
      if (annotation && typeof annotation.contextFor === 'function') {
        try {
          return annotation.contextFor({
            row: annotation.row,
            columns: annotation.columns,
            rows: annotation.getRows ? annotation.getRows() : [],
          });
        } catch (err) {
          // A broken describe hook must not break right-click everywhere: degrade to
          // window-level context only (the ospf-smoke lesson — a runtime bug stays local).
          return null;
        }
      }
      el = el.parentElement;
    }
    return null;
  }

  function composeSeed(win, target, winEl) {
    const def = win.def;
    const lines = [`[window] ${def.mnemonic} — ${def.title}`];
    const params = formatParams(def.params);
    if (params) lines.push(`params: ${params}`);
    let subject = def.title;
    const described = resolveElement(target, winEl);
    if (described && described.elementText) {
      subject = described.subject || subject;
      lines.push('[element]', described.elementText);
    }
    let contextText = lines.join('\n');
    if (contextText.length > SEED_CONTEXT_MAX) {
      // Visible truncation — the chip shows EXACTLY what is sent, never silently clipped.
      contextText = `${contextText.slice(0, SEED_CONTEXT_MAX - 16).trimEnd()}\n…[truncated]`;
    }
    return { subject, contextText, hasElement: Boolean(described && described.elementText) };
  }

  let askMenu = null;
  function hideAskMenu() {
    askMenu?.remove();
    askMenu = null;
  }
  function showAskMenu(x, y, label, seed) {
    hideAskMenu();
    askMenu = document.createElement('div');
    askMenu.id = 'ask-menu';
    const item = document.createElement('button');
    item.type = 'button';
    item.textContent = label;
    item.addEventListener('click', () => {
      hideAskMenu();
      openChatWindow(seed);
    });
    askMenu.appendChild(item);
    // Position at the pointer, then nudge back inside the viewport if it would overflow.
    askMenu.style.left = `${x}px`;
    askMenu.style.top = `${y}px`;
    document.body.appendChild(askMenu);
    const box = askMenu.getBoundingClientRect();
    if (box.right > window.innerWidth) askMenu.style.left = `${Math.max(0, x - box.width)}px`;
    if (box.bottom > window.innerHeight) askMenu.style.top = `${Math.max(0, y - box.height)}px`;
  }

  document.addEventListener('contextmenu', (event) => {
    if (event.shiftKey) return; // FR-006: Shift+right-click → the browser's native menu
    if (isTypingTarget(event.target)) return; // inputs keep native selection/copy
    const winEl = event.target.closest?.('.win');
    const win = winEl ? winByEl.get(winEl) : null;
    if (win && String(win.def.id).startsWith(TALK_ID_PREFIX)) return; // chat windows: native
    event.preventDefault();
    if (!win) {
      showAskMenu(event.clientX, event.clientY, 'ask obi', null); // background: plain new chat
      return;
    }
    const seed = composeSeed(win, event.target, winEl);
    const covered = COVERED_MNEMONICS.has(win.def.mnemonic);
    const label = covered || seed.hasElement ? 'ask obi about this' : 'ask obi';
    showAskMenu(event.clientX, event.clientY, label, { subject: seed.subject, contextText: seed.contextText });
  });

  window.addEventListener('pointerdown', (event) => {
    if (askMenu && !askMenu.contains(event.target)) hideAskMenu();
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideAskMenu();
  });
}

// --- boot ---------------------------------------------------------------------------------------

export async function boot() {
  const params = new URLSearchParams(location.search);
  const version = params.get('version');
  if (version === '2d') {
    location.replace('?w=map'); // legacy 033 2D page -> the MAP full-screen page
    return;
  }
  if (version === '3d') {
    location.replace('?w=xr'); // legacy 022-033 3D setup page -> the XR widget's page
    return;
  }
  const widget = params.get('w');
  if (widget !== null) {
    await bootFullscreen(widget);
    return;
  }
  await bootWorkspace();
}
