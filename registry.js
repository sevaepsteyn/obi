// obi widget registry (feature 037) — the workspace catalog, PURE DATA.
//
// One entry per tool. Importing this module never loads widget code: `module` is a path
// handed to dynamic import() on first open (the landing's lazy-load idiom since 033).
// Adding a future widget = one entry here + one module + one server allowlist route —
// nothing else changes (contracts/widget-contract.md).
//
// `render` is the forward-design flag for the future in-headset window host: 'canvas'
// content is texture-friendly (drawable onto a plane in the immersive scene); 'dom'
// widgets (forms, text inputs) stay 2D-only until an in-XR input story is field-proven.

export const WIDGETS = [
  {
    id: 'map',
    mnemonic: 'MAP',
    title: '2D link map',
    module: './widgets/map.js',
    defaultSize: [720, 520],
    minSize: [320, 240],
    render: 'canvas',
  },
  {
    id: 'talk',
    mnemonic: 'CHAT',
    title: 'ask obi',
    module: './widgets/talk.js',
    defaultSize: [500, 520],
    minSize: [280, 220],
    render: 'dom',
  },
  {
    id: 'logs',
    mnemonic: 'LOGS',
    title: 'device log tail',
    module: './widgets/logs.js',
    defaultSize: [760, 420],
    minSize: [360, 220],
    render: 'dom',
  },
  {
    id: 'uisp',
    mnemonic: 'UISP',
    title: 'UISP devices',
    module: './widgets/uisp.js',
    defaultSize: [720, 420],
    minSize: [400, 200],
    render: 'dom',
  },
  {
    id: 'links',
    mnemonic: 'LINKS',
    title: 'backhaul RF links',
    module: './widgets/links.js',
    defaultSize: [760, 420],
    minSize: [420, 200],
    render: 'dom',
  },
  {
    id: 'ros',
    mnemonic: 'ROS',
    title: 'RouterOS fleet',
    module: './widgets/ros.js',
    defaultSize: [820, 460],
    minSize: [460, 200],
    render: 'dom',
  },
  {
    id: 'sik',
    mnemonic: 'SIKLU',
    title: 'Siklu radios',
    module: './widgets/sik.js',
    defaultSize: [680, 300],
    minSize: [400, 180],
    render: 'dom',
  },
  {
    id: 'unifi',
    mnemonic: 'UNIFI',
    title: 'UniFi devices',
    module: './widgets/unifi.js',
    defaultSize: [720, 420],
    minSize: [400, 200],
    render: 'dom',
  },
  {
    id: 'nbx',
    mnemonic: 'NBX',
    title: 'NetBox drift',
    module: './widgets/nbx.js',
    defaultSize: [760, 420],
    minSize: [420, 200],
    render: 'dom',
  },
  {
    id: 'watch',
    mnemonic: 'WATCH',
    title: 'watcher findings',
    module: './widgets/watch.js',
    defaultSize: [820, 420],
    minSize: [420, 220],
    render: 'dom',
  },
  {
    id: 'met',
    mnemonic: 'METRICS',
    title: 'metrics charts',
    module: './widgets/met.js',
    defaultSize: [760, 440],
    minSize: [420, 260],
    render: 'canvas',
  },
  {
    id: 'cfg',
    mnemonic: 'CONF',
    title: 'config backups',
    module: './widgets/cfg.js',
    defaultSize: [820, 480],
    minSize: [440, 240],
    render: 'dom',
  },
  {
    id: 'hlt',
    mnemonic: 'HEALTH',
    title: 'fleet health',
    module: './widgets/hlt.js',
    defaultSize: [900, 460],
    minSize: [460, 240],
    render: 'dom',
  },
  {
    // 088: the walkie-talkie channel transcript (its data route is /api/radio.json).
    id: 'radio',
    mnemonic: 'RADIO',
    title: 'radio traffic',
    module: './widgets/radio.js',
    defaultSize: [760, 420],
    minSize: [360, 220],
    render: 'dom',
    // Gated: hidden from every launcher surface until the operator types the unlisted `enable`
    // command (shell.js owns the gate; the registry stays pure data — this is just the flag).
    gated: true,
  },
  {
    id: 'audit',
    mnemonic: 'AUDIT',
    title: 'audit trail',
    module: './widgets/audit.js',
    defaultSize: [860, 480],
    minSize: [440, 240],
    render: 'dom',
    gated: true, // same gate as RADIO — see shell.js
  },
  {
    id: 'xr',
    mnemonic: 'XR',
    title: 'immersive session',
    module: './widgets/xr.js',
    defaultSize: [420, 500],
    minSize: [300, 320],
    render: 'dom',
  },
  // 048: live net diagnostics — run a ping / traceroute FROM a chosen router (the web tool bridge).
  {
    id: 'ping',
    mnemonic: 'PING',
    title: 'ping',
    module: './widgets/ping.js',
    defaultSize: [520, 360],
    minSize: [320, 220],
    render: 'dom',
  },
  {
    id: 'trace',
    mnemonic: 'TRACE',
    title: 'traceroute',
    module: './widgets/trace.js',
    defaultSize: [560, 400],
    minSize: [320, 220],
    render: 'dom',
  },
  // 052: the OSPF topology map (canvas) — polls /snapshots/ospf.json; drills into ospf:<device>.
  // 053: the mnemonic is the protocol's own name — bare `OSPF` opens the map, `OSPF <device>`
  // opens that router's drill-down (see shell.js deviceArg).
  {
    id: 'ospf',
    mnemonic: 'OSPF',
    title: 'OSPF',
    module: './widgets/ospf.js',
    defaultSize: [820, 560],
    minSize: [360, 280],
    render: 'canvas',
  },
  // 095: the green/yellow/red status board (its data route is /api/dash.json — the minute
  // cycle's persisted board, never a device read). `?w=dash` is the wall-display form.
  {
    id: 'dash',
    mnemonic: 'DASH',
    title: 'status dashboard',
    module: './widgets/dash.js',
    defaultSize: [640, 500],
    minSize: [340, 260],
    render: 'dom',
  },
];

export function widgetById(id) {
  return WIDGETS.find((w) => w.id === id) ?? null;
}

// Unlisted mnemonic aliases: honored when typed, but never their own palette/menu rows (they are
// not WIDGETS entries). CHAT answers to TALK and VOICE. Typing an alias resolves to — and surfaces
// — the target widget, so the palette never says "unknown" for an alias that actually works; an
// empty/opened palette still lists only the real mnemonics.
export const MNEMONIC_ALIASES = { TALK: 'CHAT', VOICE: 'CHAT', HLT: 'HEALTH' };

export function widgetByMnemonic(mnemonic) {
  const wanted = String(mnemonic || '').trim().toUpperCase();
  const resolved = MNEMONIC_ALIASES[wanted] ?? wanted;
  return WIDGETS.find((w) => w.mnemonic === resolved) ?? null;
}

// A typed launcher query matches a widget by mnemonic substring, title substring, or an unlisted
// alias prefix (so `TALK`/`VOICE` surface CHAT). The one place the launcher decides what to show.
export function widgetMatchesQuery(widget, query) {
  const q = String(query || '').trim().toUpperCase();
  if (!q) return true;
  if (widget.mnemonic.includes(q) || widget.title.toUpperCase().includes(q)) return true;
  return Object.entries(MNEMONIC_ALIASES).some(
    ([alias, target]) => target === widget.mnemonic && alias.startsWith(q),
  );
}

// Ranking (front-first): a match on the FRONT of the command outranks an interior one — typing
// `RA` lists RADIO (mnemonic prefix) before AUDIT (whose "audit tRAil" title merely contains it).
// 0 = the mnemonic (or an unlisted alias of it) starts with the query; 1 = any other match;
// -1 = no match. Ties within a rank keep the caller's order (the launcher's alphabetical catalog).
export function widgetQueryRank(widget, query) {
  const q = String(query || '').trim().toUpperCase();
  if (!q) return 0;
  if (!widgetMatchesQuery(widget, q)) return -1;
  if (widget.mnemonic.startsWith(q)) return 0;
  const aliasPrefix = Object.entries(MNEMONIC_ALIASES).some(
    ([alias, target]) => target === widget.mnemonic && alias.startsWith(q),
  );
  return aliasPrefix ? 0 : 1;
}

// --- dynamic per-router windows (feature 047) ------------------------------------------------
//
// A router-detail window is NOT a catalog entry: it is minted on demand, one per device,
// keyed by the device ADDRESS (identities can change; the address is what the live read uses).
// Distinct ids → the WM's per-id Map holds many at once (multiple independent router windows).
// `params` carries the address/identity to the detail widget; these defs never appear in the
// launcher/menu (those iterate WIDGETS).

// --- dynamic chat windows (feature 056) ------------------------------------------------------
//
// CHAT stops being a single-instance catalog window: every launch mints a NEW `talk:<uuid>`
// window bound to a fresh conversation (US1) — the 047/051 minted-def pattern. The catalog
// `talk` entry REMAINS as the launcher/menu template + mnemonic lookup, but it is never a window
// id; the shell's CHAT paths call `talkWidgetDef(crypto.randomUUID())`. A uuid (not a counter)
// survives layout restore without collision bookkeeping. `params.seed` is present only on a
// right-click-minted window ({subject, contextText}) and is NEVER restored (chips are ephemeral,
// like transcripts).

export const TALK_ID_PREFIX = 'talk:';

export function talkWidgetDef(uuid, seed = null) {
  const id = String(uuid || '').trim();
  return {
    id: `${TALK_ID_PREFIX}${id}`,
    mnemonic: 'CHAT',
    title: 'ask obi',
    module: './widgets/talk.js',
    defaultSize: [500, 520],
    minSize: [280, 220],
    render: 'dom',
    params: { seed },
  };
}

// --- multi-instance net-tool windows (056 follow-up) -----------------------------------------
//
// PING and TRACE, like CHAT, open a NEW window on every launch — you frequently want several
// probes at once (different targets, or one target from different routers). The catalog entries
// stay the launcher templates + mnemonic lookup; each launch mints a `ping:<uuid>` / `trace:<uuid>`
// window off the template (spread of the catalog def, fresh id). Nothing else about them changes.

export const MULTI_INSTANCE_BASE_IDS = ['ping', 'trace'];

export function mintedToolDef(baseId, uuid = null) {
  const base = widgetById(baseId);
  if (!base) return null;
  const id = String(uuid || '').trim() || crypto.randomUUID();
  return { ...base, id: `${baseId}:${id}` };
}

export const ROUTER_ID_PREFIX = 'ros:';

export function routerWidgetDef(address, identity = '') {
  const addr = String(address || '').trim();
  return {
    id: `${ROUTER_ID_PREFIX}${addr}`,
    mnemonic: 'ROS',
    title: (identity || addr),
    module: './widgets/router.js',
    defaultSize: [640, 520],
    minSize: [360, 300],
    render: 'dom',
    params: { address: addr, identity: String(identity || '') },
  };
}

// --- dynamic per-device config windows (feature 051) -----------------------------------------
//
// The CFG detail view (049) moved into its own window, one per device — the 047 router-window
// pattern applied to config backups. Keyed by device IDENTITY (the 026 config store's key, not an
// address — the read is a local git read, not a live device read). Never a launcher/menu entry.

export const CONFIG_ID_PREFIX = 'conf:';

export function configWidgetDef(device) {
  const id = String(device || '').trim();
  return {
    id: `${CONFIG_ID_PREFIX}${id}`,
    mnemonic: 'CONF',
    title: id,
    module: './widgets/conf.js',
    defaultSize: [720, 520],
    minSize: [420, 280],
    render: 'dom',
    params: { device: id },
  };
}

// --- dynamic per-router OSPF drill-down windows (feature 052) ---------------------------------
//
// The 047 router-window pattern applied to OSPF. Keyed by device IDENTITY (the 051/CONF pattern —
// the drill-down reads the ospf SNAPSHOT, filtered to that router; NOT a live address-keyed read
// like ROS). One per router, many coexist; never a launcher/menu entry.

export const OSPF_ID_PREFIX = 'ospf:';

export function ospfWidgetDef(device) {
  const id = String(device || '').trim();
  return {
    id: `${OSPF_ID_PREFIX}${id}`,
    mnemonic: 'OSPF',
    title: id,
    module: './widgets/ospf_router.js',
    defaultSize: [640, 560],
    minSize: [400, 300],
    render: 'dom',
    params: { device: id },
  };
}

// --- the OSPF lint window (053) ----------------------------------------------------------------
//
// The lint list moved out of the map into its own openable/closable window, reached from the map
// widget's `lint` button (never the launcher/menu). Single instance (one fixed id, the layout
// persists/restores it); it reuses widgets/ospf.js mounted with params.view='lint', so no new
// served file — no server restart on deploy.

export const OSPF_LINT_ID = 'ospf-lint';

export function ospfLintWidgetDef() {
  return {
    id: OSPF_LINT_ID,
    mnemonic: 'OSPF',
    title: 'OSPF lint',
    module: './widgets/ospf.js',
    defaultSize: [560, 480],
    minSize: [340, 240],
    render: 'dom',
    params: { view: 'lint' },
  };
}

// --- dynamic per-link detail windows (feature 058) -------------------------------------------
//
// The MAP's in-canvas link panel promoted to a real window — the 047/051/052 synthetic-window
// pattern applied to a LINK. Keyed by the normalized endpoint-identity PAIR (bands.js linkKey),
// so a click on the MAP and a click on the LINKS board converge on the same window, and a saved
// layout rebuilds it from the key alone. One per link, many coexist. Never a launcher/menu entry
// and — deliberately — no command-line launch (link names are impractical to type).

export const LINK_ID_PREFIX = 'link:';

export function linkWidgetDef(key, name = '') {
  const k = String(key || '').trim();
  return {
    id: `${LINK_ID_PREFIX}${k}`,
    mnemonic: 'LINK',
    title: (name || k),
    module: './widgets/link.js',
    defaultSize: [420, 520],
    minSize: [300, 300],
    render: 'dom',
    params: { key: k },
  };
}

// --- ephemeral snapshot windows (feature 076) ------------------------------------------------
//
// A panel's pop-out with no live window kind opens an EPHEMERAL snapshot: an inert DOM clone of the
// panel body in its own window, labeled with its capture time. Minted like talk/ping (a fresh uuid
// id every time), but flagged `ephemeral: true` so `wm.serialize()` skips it — snapshots are never
// persisted and vanish on reload (belt-and-braces: `defForLayoutId('snap:…')` also returns null, so
// even a stray entry dies on load). params carry the clone + capture time, in memory only (never
// persisted). Sized near the source panel, clamped to sane window bounds. Contract:
// specs/076-widget-language/contracts/snapshot-window.md.

export const SNAP_ID_PREFIX = 'snap:';

function clampSize(value, lo, hi, fallback) {
  return Number.isFinite(value) && value > 0 ? Math.round(Math.max(lo, Math.min(hi, value))) : fallback;
}

export function snapshotWidgetDef(subject, node, takenAt, size = null, mnemonic = null) {
  const [w, h] = Array.isArray(size) ? size : [];
  return {
    id: `${SNAP_ID_PREFIX}${crypto.randomUUID()}`,
    // Operator field-pass call: a snapshot keeps its SOURCE window's mnemonic (an OSPF pop-out
    // reads "OSPF …"), so the title bar says what the content is; the `snapshot · HH:MM` suffix
    // stays the honest not-live marker. 'SNAP' is only the no-source fallback.
    mnemonic: mnemonic || 'SNAP',
    title: String(subject || 'snapshot'),
    module: './widgets/snap.js',
    ephemeral: true, // wm.serialize() skips this window — snapshots never persist
    defaultSize: [clampSize(w + 32, 320, 720, 460), clampSize(h + 72, 240, 640, 420)],
    minSize: [280, 200],
    render: 'dom',
    params: { node, takenAt },
  };
}

// Resolve a saved-layout widget id to a def: catalog widgets by id (remapping the retired
// `stat` id → `ros` so a pre-047 STAT window returns as ROS), `ros:<address>` ids back into a
// synthetic router def, `conf:<device>` ids back into a config-window def, and `link:<key>` ids
// back into a link-detail def. Unknown ids (e.g. the removed `inv`) resolve to null and are
// dropped by the layout filter.
export function defForLayoutId(id) {
  // 056: a restored `talk:<uuid>` returns as a fresh seedless chat (transcripts + chips are
  // ephemeral); a pre-056 saved `'talk'` id remaps to a minted uuid window (the stat→ros
  // precedent) so muscle memory survives with no migration code.
  if (id === 'talk') return talkWidgetDef(crypto.randomUUID());
  if (typeof id === 'string' && id.startsWith(TALK_ID_PREFIX)) {
    const uuid = id.slice(TALK_ID_PREFIX.length);
    return uuid ? talkWidgetDef(uuid) : null;
  }
  if (typeof id === 'string' && id.startsWith(LINK_ID_PREFIX)) {
    const key = id.slice(LINK_ID_PREFIX.length);
    return key ? linkWidgetDef(key) : null;
  }
  // A restored `ping:<uuid>` / `trace:<uuid>` returns as that same window off its catalog template
  // (the id round-trips so the layout is stable across reloads).
  if (typeof id === 'string') {
    for (const baseId of MULTI_INSTANCE_BASE_IDS) {
      if (id.startsWith(`${baseId}:`)) {
        const base = widgetById(baseId);
        return base ? { ...base, id } : null;
      }
    }
  }
  if (typeof id === 'string' && id.startsWith(ROUTER_ID_PREFIX)) {
    const address = id.slice(ROUTER_ID_PREFIX.length);
    return address ? routerWidgetDef(address) : null;
  }
  if (typeof id === 'string' && id.startsWith(CONFIG_ID_PREFIX)) {
    const device = id.slice(CONFIG_ID_PREFIX.length);
    return device ? configWidgetDef(device) : null;
  }
  if (id === OSPF_LINT_ID) return ospfLintWidgetDef();
  if (typeof id === 'string' && id.startsWith(OSPF_ID_PREFIX)) {
    const device = id.slice(OSPF_ID_PREFIX.length);
    return device ? ospfWidgetDef(device) : null;
  }
  return widgetById(id === 'stat' ? 'ros' : id);
}
