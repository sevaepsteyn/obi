// obi viewer band vocabulary (feature 044, contracts/viewer-bands.md) — the ONE module both
// viewer surfaces (viewer.js immersive, viewer2d.js map) import, so grouping rules, the
// toggle order, and persistence behave identically. Pure presentation helpers: every value
// consumed here was computed by `obi web export` — nothing is derived from frequencies or
// capacities beyond display mapping, and every read is nullish-safe so pre-044 exports
// render exactly as before (FR-014).

// 058: the canonical link identity — the normalized, sorted endpoint-identity pair. Vendor-
// neutral (UISP + Siklu), unique per link (parallel links between the same sites use distinct
// radios), and restore-safe (a `link:<key>` window rebuilds from the key alone). The MAP, the
// LINKS board, and the LINK detail window all key on this, so a click from either surface
// converges on one window. Empty/missing ends → a stable (possibly empty) key, never a throw.
export function linkKey(a, b) {
  return [a, b].map((end) => String(end ?? '').trim().toLowerCase()).sort().join('~');
}

// 063: the ONE link-health palette. Previously re-declared in viewer.js (as 0x ints for three.js),
// viewer2d.js, and widgets/link.js (as # strings) — 044 already had to hand-nudge "degraded" in all
// three. Canonical as hex strings; the 3D scene derives its numeric form via `hexToInt`. Operator
// "maintenance" (044 US3) wins the color over any health; the underlying health stays in the label.
export const HEALTH_COLORS = {
  healthy: '#22c55e',
  degraded: '#eab308', // 044: nudged yellowward so the maintenance orange reads as its own state
  down: '#ef4444',
  unknown: '#9ca3af',
};
export const MAINTENANCE_COLOR = '#f97316';

export function linkColor(link) {
  if (link.maintenance === true) return MAINTENANCE_COLOR;
  return HEALTH_COLORS[link.health] ?? HEALTH_COLORS.unknown;
}

// three.js line materials take a numeric color, not a CSS string. Derive the 0x form from the one
// string palette above so the immersive and flat surfaces can never drift (viewer.js imports these).
export function hexToInt(hex) {
  return parseInt(String(hex).slice(1), 16);
}
export const HEALTH_COLORS_3D = Object.fromEntries(
  Object.entries(HEALTH_COLORS).map(([state, hex]) => [state, hexToInt(hex)]),
);
export const MAINTENANCE_COLOR_3D = hexToInt(MAINTENANCE_COLOR);

// Fixed toggle order (FR-002). A link whose exported `band` is not a known label — a raw
// naming token, or absent entirely — belongs to the catch-all 'other' group, so no link is
// ever unreachable through the toggles (FR-001).
export const BAND_ORDER = ['5 GHz', '11 GHz', '24 GHz', '60 GHz', '70 GHz', 'other'];

export function bandOf(link) {
  const band = link && typeof link.band === 'string' ? link.band : null;
  return BAND_ORDER.includes(band) ? band : 'other';
}

// The toggle set is data-driven: only bands present in the payload get a chip (FR-002).
export function bandsPresent(links) {
  const present = new Set((links ?? []).map(bandOf));
  return BAND_ORDER.filter((band) => present.has(band));
}

// Persistence (FR-005): hidden bands only ({label: false}); absent = visible; stale labels
// (not in BAND_ORDER) are dropped on load, so a vocabulary change never wedges a band off.
// localStorage failures (private mode, disabled storage) degrade to session-only toggles.
export function loadHiddenBands(key) {
  try {
    const raw = JSON.parse(localStorage.getItem(key) ?? '{}');
    return new Set(BAND_ORDER.filter((band) => raw?.[band] === false));
  } catch {
    return new Set();
  }
}

export function saveHiddenBands(key, hidden) {
  try {
    localStorage.setItem(
      key,
      JSON.stringify(Object.fromEntries([...hidden].map((band) => [band, false])))
    );
  } catch {
    // storage unavailable — the toggle still applies for this session
  }
}

// 055: the MAP's terrain-underlay toggle persists per surface with the same try/catch degrade
// as band visibility. bands.js is the sanctioned localStorage owner for the viewer surfaces, so
// the widget passes a key and never touches storage itself (test_web_workspace's owners rule).
// Terrain is now ON by default, so the stored state is inverted — absent means the default (on)
// and only an explicit opt-out is persisted, as the string 'off'. Operators who had earlier turned
// it on carry a stale 'on'; that reads as "not off" and stays on, exactly as intended.
export function loadTerrainOn(key) {
  try {
    return localStorage.getItem(key) !== 'off'; // default on; only an explicit opt-out suppresses
  } catch {
    return true; // private mode — default on; session-only toggling still works
  }
}

export function saveTerrainOn(key, on) {
  try {
    if (on) localStorage.removeItem(key); // on is the default — nothing to persist
    else localStorage.setItem(key, 'off'); // remember only the operator's explicit opt-out
  } catch {
    // storage unavailable — the toggle applies for this session only
  }
}

// --- capacity encoding (044 US2, contracts/viewer-bands.md §Thickness/§Labels) ----------------
// ONE scale for both surfaces: sqrt keeps the fleet's ~31× capacity spread readable
// (thinnest always visible, thickest bounded), and the clamp ceiling sits just above the
// largest live pipe (2.7 Gbps). A link without capacity renders at the neutral default —
// today's visual weight, never a fabricated size.

export const CAP_MAX_BPS = 3e9;

export function capacityNorm(link) {
  const capacity = link?.capacity_down_bps ?? link?.capacity_up_bps;
  if (typeof capacity !== 'number' || !Number.isFinite(capacity) || capacity <= 0) return null;
  return Math.sqrt(Math.min(capacity, CAP_MAX_BPS) / CAP_MAX_BPS);
}

export function lineWidth2d(link) {
  const norm = capacityNorm(link);
  return norm === null ? 2 : 1.5 + norm * 4.5; // CSS px
}

export function linkRadius3d(link) {
  const norm = capacityNorm(link);
  return norm === null ? 0.7 : 0.4 + norm * 2.1; // scene meters (cylinder radius)
}

// Compact ≤3-significant-digit rate: 2156000000 → "2.16 Gbps". null = nothing to say.
export function formatRate(bps) {
  if (typeof bps !== 'number' || !Number.isFinite(bps) || bps <= 0) return null;
  const sig = (value) =>
    (value >= 100 ? Math.round(value).toString()
      : value >= 10 ? value.toFixed(1) : value.toFixed(2))
      .replace(/\.0+$/, '');
  if (bps >= 999.5e6) return `${sig(bps / 1e9)} Gbps`;
  if (bps >= 999.5e3) return `${sig(bps / 1e6)} Mbps`;
  return `${sig(bps / 1e3)} kbps`;
}

// Worst-direction load fraction (055 US2) — the visual scale for the utilization fill, so an
// upload-saturated link reads as full even when its download is idle. The MAX over the
// directions where BOTH usage and capacity are finite and capacity > 0 (usage ≥ 0); null when
// no direction is computable (absence never renders as zero). UNCLAMPED — the renderer clamps
// min(fraction, 1) at draw time; the detail panel keeps the precise per-direction split.
// Nullish-safe on every read, so pre-044 exports return null rather than throw (FR-014).
export function usageFraction(link) {
  let best = null;
  for (const dir of ['down', 'up']) {
    const usage = link?.[`usage_${dir}_bps`];
    const capacity = link?.[`capacity_${dir}_bps`];
    if (
      typeof usage === 'number' && Number.isFinite(usage) && usage >= 0 &&
      typeof capacity === 'number' && Number.isFinite(capacity) && capacity > 0
    ) {
      const fraction = usage / capacity;
      if (best === null || fraction > best) best = fraction;
    }
  }
  return best;
}

// Down-direction load, as exported (usage ÷ capacity); tiny real traffic reads "<0.1%",
// which is honest without shouting zeros across the scene.
export function usagePercent(link) {
  const usage = link?.usage_down_bps;
  const capacity = link?.capacity_down_bps;
  if (typeof usage !== 'number' || typeof capacity !== 'number' || capacity <= 0 || usage < 0) {
    return null;
  }
  const percent = (usage / capacity) * 100;
  if (percent < 0.1) return '<0.1%';
  return `${percent >= 10 ? Math.round(percent) : percent.toFixed(1)}%`;
}

// The on-link label line: "2.16 Gbps ↓ / 1.75 Gbps ↑ · 2.2% used" — parts appear only when
// the export carries them (FR-008); null when the link has no capacity data at all.
export function capacityLine(link) {
  const down = formatRate(link?.capacity_down_bps);
  const up = formatRate(link?.capacity_up_bps);
  if (!down && !up) return null;
  const parts = [];
  if (down) parts.push(`${down} ↓`);
  if (up) parts.push(`${up} ↑`);
  const used = usagePercent(link);
  return parts.join(' / ') + (used ? ` · ${used} used` : '');
}

// --- 078 XR link-view vocabulary (contracts/view-modes.md) --------------------------------------
// The immersive viewer's three fleet-wide color views, their ramps, the maintenance>down>metric>
// no-data precedence, the utilization inner-core sizing, the paged link list, and the viewer's
// persisted preferences. Pure presentation math shared by viewer.js — nothing here derives a value
// the export did not already carry, and every read is nullish-safe so a pre-044 payload renders in
// health mode exactly as before and no-data in every metric mode (contracts §Compatibility).

export const MODE_HEALTH = 'health';
export const MODE_SIGNAL = 'signal';
export const MODE_UTILIZATION = 'utilization';
// 2026-07-17 field pass: Fresnel zones are a VIEW, not a scene toggle — selecting it shows the
// zone ellipsoids and colors links by their terrain-clearance verdict (the 031 export).
export const MODE_FRESNEL = 'fresnel';
// The exact ordered set (default first). viewer.js validates a stored pref against it.
export const VIEW_MODES = [MODE_HEALTH, MODE_SIGNAL, MODE_UTILIZATION, MODE_FRESNEL];

// Color ramps as ordered {t, hex} stops (R1/R2). Same warm/cool vocabulary the 044 health palette
// uses, so the fleet never grows a second color language: strong/idle at t=0, attention-red at t=1.
export const SIGNAL_RAMP = [
  { t: 0, hex: '#22c55e' },   // −45 dBm or stronger — green
  { t: 0.5, hex: '#eab308' }, // −57.5 dBm — yellow
  { t: 1, hex: '#ef4444' },   // −70 dBm or weaker — red
];
export const USAGE_RAMP = [
  { t: 0, hex: '#3b82f6' },   // 0% — steel blue (deliberately NOT healthy-green: idle ≠ good)
  { t: 0.5, hex: '#eab308' }, // 50% — yellow
  { t: 1, hex: '#ef4444' },   // 100% — red
];

// The signal anchor points (R1): stronger than −45 is fully green, weaker than −70 fully red.
export const SIGNAL_STRONG_DBM = -45;
export const SIGNAL_WEAK_DBM = -70;

// Fresnel-view link colors: the categorical clearance verdicts (031), on the same warm/cool
// vocabulary as everything else. A link with no verdict renders no-data, never "clear".
export const CLEARANCE_COLORS = {
  clear: '#22c55e',
  marginal: '#eab308',
  obstructed: '#ef4444',
};
export const CLEARANCE_COLORS_3D = Object.fromEntries(
  Object.entries(CLEARANCE_COLORS).map(([verdict, hex]) => [verdict, hexToInt(hex)])
);

// The no-data treatment (R3/FR-004): the existing unknown gray at reduced opacity — a point on
// neither ramp, and the same for "Siklu has no usage" and "UISP link missing signal" (Principle IV).
export const NO_DATA_COLOR_3D = HEALTH_COLORS_3D.unknown; // #9ca3af
export const NO_DATA_OPACITY = 0.45;

// Linear interpolation between two hex stops, packed back into a three.js 0x int.
function lerpChannel(a, b, t) {
  return Math.round(a + (b - a) * t);
}
function lerpHex(fromHex, toHex, t) {
  const from = hexToInt(fromHex);
  const to = hexToInt(toHex);
  const r = lerpChannel((from >> 16) & 0xff, (to >> 16) & 0xff, t);
  const g = lerpChannel((from >> 8) & 0xff, (to >> 8) & 0xff, t);
  const b = lerpChannel(from & 0xff, to & 0xff, t);
  return (r << 16) | (g << 8) | b;
}

// rampColor(stops, t): clamp t to 0..1, then interpolate within the bracketing segment. Landing
// exactly on a stop returns that stop's color byte-for-byte (so the documented endpoints are exact).
export function rampColor(stops, t) {
  const clamped = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  for (let i = 0; i < stops.length - 1; i++) {
    const lo = stops[i];
    const hi = stops[i + 1];
    if (clamped <= hi.t) {
      const span = hi.t - lo.t || 1;
      return lerpHex(lo.hex, hi.hex, (clamped - lo.t) / span);
    }
  }
  return hexToInt(stops[stops.length - 1].hex);
}

// signalT(dbm): map a reported RSSI onto the ramp parameter (0 strong … 1 weak), clamped. A link
// with no signal reading (null / non-number) returns null — the caller renders no-data, never 0.
export function signalT(dbm) {
  if (typeof dbm !== 'number' || !Number.isFinite(dbm)) return null;
  const t = (SIGNAL_STRONG_DBM - dbm) / (SIGNAL_STRONG_DBM - SIGNAL_WEAK_DBM);
  return Math.min(1, Math.max(0, t));
}

// modeColor3d(link, mode): the ONE coloring authority for the immersive cylinders, endpoint
// markers, and link-list dots (contracts §Precedence). Precedence in every mode:
//   maintenance (#f97316) > down (#ef4444) > metric ramp > no-data gray@0.45.
// Health mode is the compatibility mode — byte-identical to the pre-078 (HEALTH_COLORS_3D +
// maintenance override), always opaque. Returns { color: 0xRRGGBB, opacity: 0..1 }.
export function modeColor3d(link, mode) {
  if (link?.maintenance === true) return { color: MAINTENANCE_COLOR_3D, opacity: 1 };
  if (link?.health === 'down') return { color: HEALTH_COLORS_3D.down, opacity: 1 };
  if (mode === MODE_SIGNAL) {
    const t = signalT(link?.signal_dbm);
    if (t === null) return { color: NO_DATA_COLOR_3D, opacity: NO_DATA_OPACITY };
    return { color: rampColor(SIGNAL_RAMP, t), opacity: 1 };
  }
  if (mode === MODE_UTILIZATION) {
    const fraction = usageFraction(link);
    if (fraction === null) return { color: NO_DATA_COLOR_3D, opacity: NO_DATA_OPACITY };
    return { color: rampColor(USAGE_RAMP, Math.min(fraction, 1)), opacity: 1 };
  }
  if (mode === MODE_FRESNEL) {
    const color = CLEARANCE_COLORS_3D[link?.clearance_verdict];
    if (color === undefined) return { color: NO_DATA_COLOR_3D, opacity: NO_DATA_OPACITY };
    return { color, opacity: 1 };
  }
  // health (and any unknown mode string) ≡ today's colors, opaque.
  return { color: HEALTH_COLORS_3D[link?.health] ?? HEALTH_COLORS_3D.unknown, opacity: 1 };
}

// coreScale(fraction): the utilization inner-core sizing (R10). A "fill level" inside the capacity
// pipe — radius grows 0.15→0.9 of the outer radius, length is the raw fraction of the chord. null
// fraction (no usage data) ⇒ null: the caller builds NO mesh (absence, never an empty pipe).
export function coreScale(fraction) {
  if (typeof fraction !== 'number' || !Number.isFinite(fraction)) return null;
  const f = Math.min(1, Math.max(0, fraction));
  return { radiusFactor: 0.15 + 0.75 * f, lengthFactor: f };
}

// The panel link list is paged, not drag-scrolled (R7): a canvas hit-grid page is fat-finger-safe
// where a sprite drag would collide with the 029 grab-drag detection.
export const LINK_LIST_PAGE_SIZE = 8;

// linkListPage(list, page, size): clamp the requested page into range and return that slice plus
// the page geometry. Out-of-range (a shrunk fleet after a refresh) clamps to the last page; an
// empty list is one empty page, never a divide-by-zero.
export function linkListPage(list, page, size = LINK_LIST_PAGE_SIZE) {
  const items = Array.isArray(list) ? list : [];
  const pageCount = Math.max(1, Math.ceil(items.length / size));
  const clamped = Math.min(pageCount - 1, Math.max(0, Number.isFinite(page) ? Math.trunc(page) : 0));
  const start = clamped * size;
  const end = Math.min(items.length, start + size);
  return { page: clamped, pageCount, start, end, items: items.slice(start, end) };
}

// canonicalLinkName(name) (094 round 4): UISP names a link by ITS OWN endpoint order, so the
// two hops of one site pair scatter across an alphabetical list ("Showers <-> BRC - NOC"
// under S, "BRC - NOC <-> Basswood Relay - East" under B) — which reads as an unsorted list.
// The viewer's link list shows and sorts the "A <-> B (band)" shape with the pair
// alphabetized instead; a name that doesn't match the shape passes through verbatim, and a
// missing name degrades to '' rather than throwing.
export function canonicalLinkName(name) {
  const match = /^(.+?) <-> (.+?)( \([^)]*\))?$/.exec(String(name ?? ''));
  if (!match) return String(name ?? '');
  const [, left, right, band] = match;
  const pair = [left, right].sort((a, b) => a.localeCompare(b));
  return `${pair[0]} <-> ${pair[1]}${band ?? ''}`;
}

// Generic viewer preference persistence (R9/FR-018): JSON round-trip through localStorage under
// arbitrary keys, with the same try/catch degrade as the band toggles — a private-mode browser or
// a corrupt stored value degrades to the caller's fallback for the session and never throws.
// bands.js is the sanctioned localStorage owner for the viewer surfaces (test_web_workspace's rule).
export function loadViewerPref(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null || raw === undefined) return fallback;
    return JSON.parse(raw);
  } catch {
    return fallback; // storage unavailable or corrupt value — session default
  }
}

export function saveViewerPref(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage unavailable — the setting still applies for this session
  }
}

// --- 119: the backdrop decision (contracts/xr-backdrop.md C3/C6) --------------------------------
// The immersive viewer runs ONE session type (immersive-ar wherever supported) and treats
// "VR" as an opaque backdrop painted over passthrough, flipped at runtime from the settings
// panel. These four helpers are the whole decision, kept here — pure, DOM- and three-free —
// so they can be EXECUTED by bands_modes_headless.mjs. Nothing else in the feature can be:
// no test loads viewer.js, and the failure mode that matters most (a Color background renders
// nothing at all in an alpha-blend session) passes every text tripwire. 080's lesson.

export const BACKDROP_KEY = 'obi.viewer.backdrop'; // 'opaque' | 'passthrough'
export const BACKDROP_OPAQUE = 'opaque';
export const BACKDROP_PASSTHROUGH = 'passthrough';

// C3: only an alpha-blend composite can show BOTH backdrops, so only there does the toggle
// exist. 'opaque' means the runtime gave us no passthrough to reveal (the immersive-vr
// fallback, a desktop browser); 'additive' means a see-through display where black IS
// transparent and an opaque backdrop is physically impossible — three.js even forces
// scene.background to null in that mode. Unknown/absent is treated as "cannot", never as
// "probably fine": a row that cannot act must not exist (098's brcAvailable discipline).
export function backdropAvailable(blendMode) {
  return blendMode === 'alpha-blend';
}

// C6: 078's per-session-kind terrain style, now selected by the LIVE backdrop instead of the
// session type. The 'ar'/'vr' key suffixes are deliberately kept: renaming them would discard
// every operator's stored choice for no functional gain. ar ⇔ passthrough, vr ⇔ opaque.
export function terrainStyleKeyFor(opaque) {
  return `obi.viewer.terrainStyle.${opaque ? 'vr' : 'ar'}`;
}

// Passthrough wants the see-through wireframe (025 discipline: reality stays visible through
// every part of the terrain); an opaque backdrop has no reality to preserve, so the lit
// shaded relief reads better. Both are 2026-07-17 field-pass decisions from 078.
export function terrainStyleDefaultFor(opaque) {
  return opaque ? 'shaded' : 'wire';
}

// One owner for the choice (C6): the setup-form picker and the in-session row both go through
// here, so they cannot disagree. Anything not one of the two literals — absent, corrupt, a
// stale value from an older build — degrades to opaque, which is today's VR default.
export function loadBackdropOpaque() {
  return loadViewerPref(BACKDROP_KEY, BACKDROP_OPAQUE) !== BACKDROP_PASSTHROUGH;
}

export function saveBackdropOpaque(opaque) {
  saveViewerPref(BACKDROP_KEY, opaque ? BACKDROP_OPAQUE : BACKDROP_PASSTHROUGH);
}
