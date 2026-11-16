// Headless logic harness for the 078 XR-link-view vocabulary in bands.js — runs the REAL pure
// helpers in node and asserts their behavior contract (kit_panel_headless/viewer2d_headless
// pattern): the two color ramps' endpoint/midpoint/clamp values, signalT null-in→null-out, the
// modeColor3d precedence table (maintenance > down > metric > no-data gray@0.45; health mode ≡
// HEALTH_COLORS_3D), coreScale, the viewer-pref round-trip/corrupt/throws degrade, and the
// link-list paging math. These are math + persistence — executing the code path is the only
// thing that catches an interpolation or precedence bug. Prints one JSON summary line and exits
// non-zero if any check failed.
//
// Usage: node bands_modes_headless.mjs [static-dir]   (defaults to src/obi/web/static beside repo)
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const staticDir = process.argv[2] ?? resolve(HERE, '../../src/obi/web/static');

// --- a swappable localStorage stub (normal / corrupt / throw-on-access) --------------------------

function normalStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    _store: store,
  };
}
function throwingStorage() {
  return {
    getItem() { throw new Error('storage disabled (private mode)'); },
    setItem() { throw new Error('storage disabled (private mode)'); },
    removeItem() { throw new Error('storage disabled (private mode)'); },
  };
}
globalThis.localStorage = normalStorage();

// --- test scaffold -------------------------------------------------------------------------------

const bands = await import(`${staticDir}/bands.js`);
const {
  SIGNAL_RAMP, USAGE_RAMP, rampColor, signalT, modeColor3d, coreScale, linkListPage,
  loadViewerPref, saveViewerPref, hexToInt, HEALTH_COLORS_3D, MAINTENANCE_COLOR_3D,
  MODE_HEALTH, MODE_SIGNAL, MODE_UTILIZATION, MODE_FRESNEL, CLEARANCE_COLORS_3D,
} = bands;

const failures = [];
let checks = 0;
function ok(cond, label) {
  checks += 1;
  if (!cond) failures.push(label);
}
function eq(a, b, label) { ok(a === b, `${label} (got ${a}, want ${b})`); }

// 1) SIGNAL_RAMP endpoints / midpoint / clamp ----------------------------------------------------
{
  eq(rampColor(SIGNAL_RAMP, 0), hexToInt('#22c55e'), 'SIGNAL_RAMP t=0 is green');
  eq(rampColor(SIGNAL_RAMP, 0.5), hexToInt('#eab308'), 'SIGNAL_RAMP t=0.5 is yellow');
  eq(rampColor(SIGNAL_RAMP, 1), hexToInt('#ef4444'), 'SIGNAL_RAMP t=1 is red');
  eq(rampColor(SIGNAL_RAMP, -3), hexToInt('#22c55e'), 'SIGNAL_RAMP clamps below 0 to green');
  eq(rampColor(SIGNAL_RAMP, 7), hexToInt('#ef4444'), 'SIGNAL_RAMP clamps above 1 to red');
  // an in-between value interpolates strictly between the two bracketing stops
  const mid = rampColor(SIGNAL_RAMP, 0.25);
  ok(mid !== hexToInt('#22c55e') && mid !== hexToInt('#eab308'),
    'SIGNAL_RAMP t=0.25 interpolates between green and yellow');
}

// 2) USAGE_RAMP endpoints / midpoint -------------------------------------------------------------
{
  eq(rampColor(USAGE_RAMP, 0), hexToInt('#3b82f6'), 'USAGE_RAMP t=0 is steel blue');
  eq(rampColor(USAGE_RAMP, 0.5), hexToInt('#eab308'), 'USAGE_RAMP t=0.5 is yellow');
  eq(rampColor(USAGE_RAMP, 1), hexToInt('#ef4444'), 'USAGE_RAMP t=1 is red');
  // blue at idle must NOT collide with healthy-green (R2)
  ok(rampColor(USAGE_RAMP, 0) !== HEALTH_COLORS_3D.healthy,
    'USAGE_RAMP idle-blue is not healthy-green');
}

// 3) signalT: −45→0 (strong) … −70→1 (weak), clamp, null-in→null-out -----------------------------
{
  eq(signalT(-45), 0, 'signalT(-45) = 0 (strong)');
  eq(signalT(-57.5), 0.5, 'signalT(-57.5) = 0.5 (mid)');
  eq(signalT(-70), 1, 'signalT(-70) = 1 (weak)');
  eq(signalT(-41), 0, 'signalT(-41) clamps to 0 (stronger than anchor)');
  eq(signalT(-80), 1, 'signalT(-80) clamps to 1 (weaker than anchor)');
  eq(signalT(null), null, 'signalT(null) = null');
  eq(signalT(undefined), null, 'signalT(undefined) = null');
  eq(signalT(NaN), null, 'signalT(NaN) = null');
  eq(signalT('x'), null, 'signalT(non-number) = null');
}

// 4) modeColor3d precedence table (maintenance > down > metric > no-data) -------------------------
const NO_DATA_GRAY = HEALTH_COLORS_3D.unknown; // #9ca3af (R3)
function link(extra) {
  return { a: 'x', b: 'y', band: '60 GHz', health: 'healthy', maintenance: false, ...extra };
}
for (const mode of [MODE_HEALTH, MODE_SIGNAL, MODE_UTILIZATION, MODE_FRESNEL]) {
  // maintenance wins in EVERY mode, even with a live signal + usage present
  const maint = modeColor3d(link({
    maintenance: true, health: 'down', signal_dbm: -45,
    capacity_down_bps: 1e9, usage_down_bps: 5e8,
  }), mode);
  eq(maint.color, MAINTENANCE_COLOR_3D, `maintenance wins in ${mode}`);
  eq(maint.opacity, 1, `maintenance opaque in ${mode}`);

  // down wins over the metric ramp in EVERY mode (a dead link never masquerades as a datapoint)
  const down = modeColor3d(link({
    health: 'down', signal_dbm: -45, capacity_down_bps: 1e9, usage_down_bps: 5e8,
  }), mode);
  eq(down.color, HEALTH_COLORS_3D.down, `down wins over metric in ${mode}`);
  eq(down.opacity, 1, `down opaque in ${mode}`);
}

// signal mode: healthy link ramps by signal; missing signal → no-data gray @0.45
{
  const strong = modeColor3d(link({ signal_dbm: -45 }), MODE_SIGNAL);
  eq(strong.color, hexToInt('#22c55e'), 'signal mode: strong signal is green');
  eq(strong.opacity, 1, 'signal mode: metric datapoint is opaque');
  const weak = modeColor3d(link({ signal_dbm: -70 }), MODE_SIGNAL);
  eq(weak.color, hexToInt('#ef4444'), 'signal mode: weak signal is red');
  const missing = modeColor3d(link({ signal_dbm: null }), MODE_SIGNAL);
  eq(missing.color, NO_DATA_GRAY, 'signal mode: no reading → no-data gray');
  eq(missing.opacity, 0.45, 'signal mode: no-data drops opacity to 0.45');
}

// utilization mode: healthy link ramps by worst-direction usage; no usage → no-data gray @0.45
{
  const idle = modeColor3d(link({ capacity_down_bps: 1e9, usage_down_bps: 0 }), MODE_UTILIZATION);
  eq(idle.color, hexToInt('#3b82f6'), 'utilization mode: idle link is steel blue');
  eq(idle.opacity, 1, 'utilization mode: usage datapoint is opaque');
  const busy = modeColor3d(link({ capacity_down_bps: 1e9, usage_down_bps: 1e9 }), MODE_UTILIZATION);
  eq(busy.color, hexToInt('#ef4444'), 'utilization mode: saturated link is red');
  const siklu = modeColor3d(link({ capacity_down_bps: 1e9 }), MODE_UTILIZATION); // capacity, no usage
  eq(siklu.color, NO_DATA_GRAY, 'utilization mode: no usage (Siklu) → no-data gray');
  eq(siklu.opacity, 0.45, 'utilization mode: no-data drops opacity to 0.45');
  // over-capacity usage still clamps to the red end, never past it
  const over = modeColor3d(link({ capacity_down_bps: 1e9, usage_down_bps: 3e9 }), MODE_UTILIZATION);
  eq(over.color, hexToInt('#ef4444'), 'utilization mode: over-capacity clamps to red');
}

// health mode ≡ today's colors (HEALTH_COLORS_3D + maintenance override), always opaque
{
  eq(modeColor3d(link({ health: 'healthy' }), MODE_HEALTH).color, HEALTH_COLORS_3D.healthy,
    'health mode: healthy is green');
  eq(modeColor3d(link({ health: 'degraded' }), MODE_HEALTH).color, HEALTH_COLORS_3D.degraded,
    'health mode: degraded is yellow');
  eq(modeColor3d(link({ health: 'down' }), MODE_HEALTH).color, HEALTH_COLORS_3D.down,
    'health mode: down is red');
  eq(modeColor3d(link({ health: 'weird' }), MODE_HEALTH).color, HEALTH_COLORS_3D.unknown,
    'health mode: unknown health → unknown gray');
  eq(modeColor3d(link({ health: 'healthy' }), MODE_HEALTH).opacity, 1,
    'health mode: always opaque (never the metric no-data fade)');
  // health mode never fabricates a metric fade for a signalless link — it is opaque unknown/health
  eq(modeColor3d(link({ health: 'healthy', signal_dbm: null }), MODE_HEALTH).opacity, 1,
    'health mode: a signalless link is still opaque (health, not no-data)');
}

// 4b) fresnel view: categorical clearance-verdict colors (2026-07-17 field amendment) ------------
{
  eq(modeColor3d(link({ clearance_verdict: 'clear' }), MODE_FRESNEL).color,
    CLEARANCE_COLORS_3D.clear, 'fresnel view: clear is green');
  eq(modeColor3d(link({ clearance_verdict: 'marginal' }), MODE_FRESNEL).color,
    CLEARANCE_COLORS_3D.marginal, 'fresnel view: marginal is yellow');
  eq(modeColor3d(link({ clearance_verdict: 'obstructed' }), MODE_FRESNEL).color,
    CLEARANCE_COLORS_3D.obstructed, 'fresnel view: obstructed is red');
  eq(modeColor3d(link({ clearance_verdict: 'clear' }), MODE_FRESNEL).opacity, 1,
    'fresnel view: a verdict renders opaque');
  const noVerdict = modeColor3d(link({}), MODE_FRESNEL);
  eq(noVerdict.color, NO_DATA_GRAY, 'fresnel view: no verdict → no-data gray');
  eq(noVerdict.opacity, 0.45, 'fresnel view: no verdict → no-data opacity');
  const bogus = modeColor3d(link({ clearance_verdict: 'sideways' }), MODE_FRESNEL);
  eq(bogus.color, NO_DATA_GRAY, 'fresnel view: an unknown verdict string → no-data, never a guess');
}

// 5) coreScale: 0.15+0.75·f radius, f length, null→null, clamp -----------------------------------
{
  const zero = coreScale(0);
  ok(zero && Math.abs(zero.radiusFactor - 0.15) < 1e-9 && zero.lengthFactor === 0,
    'coreScale(0) = {0.15, 0}');
  const full = coreScale(1);
  ok(full && Math.abs(full.radiusFactor - 0.9) < 1e-9 && full.lengthFactor === 1,
    'coreScale(1) = {0.9, 1}');
  const half = coreScale(0.5);
  ok(half && Math.abs(half.radiusFactor - 0.525) < 1e-9 && half.lengthFactor === 0.5,
    'coreScale(0.5) = {0.525, 0.5}');
  eq(coreScale(null), null, 'coreScale(null) = null (no core)');
  eq(coreScale(undefined), null, 'coreScale(undefined) = null');
  eq(coreScale(NaN), null, 'coreScale(NaN) = null');
  const over = coreScale(2);
  ok(over && Math.abs(over.radiusFactor - 0.9) < 1e-9 && over.lengthFactor === 1,
    'coreScale(2) clamps to {0.9, 1}');
}

// 6) loadViewerPref / saveViewerPref: round-trip, corrupt JSON, storage throws -------------------
{
  globalThis.localStorage = normalStorage();
  eq(loadViewerPref('obi.viewer.mode', 'health'), 'health', 'load: absent key → fallback');
  saveViewerPref('obi.viewer.mode', 'signal');
  eq(loadViewerPref('obi.viewer.mode', 'health'), 'signal', 'round-trip: string pref');
  saveViewerPref('obi.viewer.zones', true);
  eq(loadViewerPref('obi.viewer.zones', false), true, 'round-trip: boolean pref');
  saveViewerPref('obi.viewer.bands', ['60 GHz', '5 GHz']);
  const arr = loadViewerPref('obi.viewer.bands', []);
  ok(Array.isArray(arr) && arr.length === 2 && arr[0] === '60 GHz', 'round-trip: array pref');

  // corrupt stored JSON degrades to the fallback, never throws
  globalThis.localStorage._store.set('obi.viewer.mode', '{not json');
  eq(loadViewerPref('obi.viewer.mode', 'health'), 'health', 'corrupt JSON → fallback');

  // storage entirely unavailable (private mode): load falls back, save is a no-op, neither throws
  globalThis.localStorage = throwingStorage();
  let threw = false;
  let val;
  try { val = loadViewerPref('obi.viewer.mode', 'health'); } catch { threw = true; }
  ok(!threw && val === 'health', 'storage throws on get → fallback, no throw');
  threw = false;
  try { saveViewerPref('obi.viewer.mode', 'signal'); } catch { threw = true; }
  ok(!threw, 'storage throws on set → save swallows, no throw');
  globalThis.localStorage = normalStorage();
}

// 7) linkListPage clamp / reset math (page size 8) -----------------------------------------------
{
  const list = Array.from({ length: 19 }, (_, i) => `link-${i}`);
  const p0 = linkListPage(list, 0);
  ok(p0.page === 0 && p0.pageCount === 3 && p0.start === 0 && p0.end === 8 && p0.items.length === 8,
    'linkListPage: page 0 of 19 → 8 items, 3 pages');
  const p2 = linkListPage(list, 2);
  ok(p2.page === 2 && p2.start === 16 && p2.end === 19 && p2.items.length === 3,
    'linkListPage: last page → the 3 remainder items');
  eq(linkListPage(list, 9).page, 2, 'linkListPage: out-of-range page clamps down to last');
  eq(linkListPage(list, -4).page, 0, 'linkListPage: negative page clamps up to first');
  const empty = linkListPage([], 0);
  ok(empty.page === 0 && empty.pageCount === 1 && empty.items.length === 0,
    'linkListPage: empty list → one empty page, never a division blowup');
  const exact = linkListPage(Array.from({ length: 8 }, (_, i) => i), 0);
  eq(exact.pageCount, 1, 'linkListPage: exactly one full page is a single page (no empty page 2)');
}

// 8) compatibility: a pre-044 link (no band/signal/capacity/usage keys) — health normal, metric
//    modes no-data, no core, no throw (contracts/view-modes.md §Compatibility, T022) --------------
{
  const legacy = { a: 'x', b: 'y', health: 'healthy', name: 'old link' };
  let threw = false;
  try {
    // health mode ≡ the pre-078 healthy color, opaque
    const h = modeColor3d(legacy, MODE_HEALTH);
    eq(h.color, HEALTH_COLORS_3D.healthy, 'pre-044: health mode is normal (healthy green)');
    eq(h.opacity, 1, 'pre-044: health mode opaque');
    // both metric modes fall to the no-data treatment (no metric to read)
    const sig = modeColor3d(legacy, MODE_SIGNAL);
    eq(sig.color, NO_DATA_GRAY, 'pre-044: signal mode → no-data gray');
    eq(sig.opacity, 0.45, 'pre-044: signal mode no-data opacity');
    const utl = modeColor3d(legacy, MODE_UTILIZATION);
    eq(utl.color, NO_DATA_GRAY, 'pre-044: utilization mode → no-data gray');
    eq(utl.opacity, 0.45, 'pre-044: utilization mode no-data opacity');
    const fre = modeColor3d(legacy, MODE_FRESNEL);
    eq(fre.color, NO_DATA_GRAY, 'pre-031: fresnel view without a verdict → no-data gray');
    // no usage ⇒ no fraction ⇒ no inner core
    eq(bands.usageFraction(legacy), null, 'pre-044: usageFraction is null');
    eq(coreScale(bands.usageFraction(legacy)), null, 'pre-044: no inner core');
  } catch {
    threw = true;
  }
  ok(!threw, 'pre-044: a legacy link renders through the whole vocabulary without throwing');
}

// 9) canonicalLinkName (094 round 4): the "A <-> B (band)" pair alphabetizes so one site
//    pair's hops sit together in a sorted list; anything else passes through verbatim ------------
{
  const canon = bands.canonicalLinkName;
  eq(canon('Tamarack <-> BRC - NOC (11 GHz)'), 'BRC - NOC <-> Tamarack (11 GHz)',
    'canonical: the pair alphabetizes and the band suffix stays put');
  eq(canon('BRC - NOC <-> Tamarack (11 GHz)'), 'BRC - NOC <-> Tamarack (11 GHz)',
    'canonical: an already-ordered pair is unchanged');
  eq(canon('Basswood Relay - West <-> Tamarack (70 GHz)'),
    'Basswood Relay - West <-> Tamarack (70 GHz)',
    'canonical: ordered e-band pair unchanged');
  eq(canon('Tamarack <-> Basswood Relay - West (60 GHz)'),
    'Basswood Relay - West <-> Tamarack (60 GHz)',
    'canonical: the same site pair lands on the same prefix whichever way UISP wrote it');
  eq(canon('Hotel <-> Basswood Relay - West'), 'Basswood Relay - West <-> Hotel',
    'canonical: a name without a band suffix still canonicalizes cleanly');
  eq(canon('backhaul ring alpha'), 'backhaul ring alpha',
    'canonical: a name without the pair shape passes through verbatim');
  eq(canon(null), '', 'canonical: a missing name degrades to empty, never throws');
}

// 10) the 119 backdrop decision: which session can offer the toggle, and which terrain-style
//     key/default each backdrop owns. Executed rather than grepped because NO test loads
//     viewer.js — the r160 "a Color background renders nothing in an alpha-blend session" bug
//     (research R1) passes every text tripwire and fails only in the headset. Same reasoning as
//     080's nav harness. Contract: specs/119-xr-backdrop/contracts/xr-backdrop.md C3, C6, C8.
{
  const { backdropAvailable, terrainStyleKeyFor, terrainStyleDefaultFor, BACKDROP_KEY } = bands;

  // C3: only a genuine passthrough composite can offer both backdrops.
  eq(backdropAvailable('alpha-blend'), true,
    'backdrop: alpha-blend is the one mode that can show BOTH backdrops');
  eq(backdropAvailable('opaque'), false,
    'backdrop: an opaque composite has no passthrough to reveal');
  eq(backdropAvailable('additive'), false,
    'backdrop: additive displays cannot render an opaque backdrop at all');
  for (const absent of [undefined, null, '', 'ALPHA-BLEND', 'alphablend', 'unknown', 0, {}]) {
    eq(backdropAvailable(absent), false,
      `backdrop: unknown/absent blend mode ${JSON.stringify(absent)} never offers the row`);
  }

  // C6: the per-kind terrain style survives a RUNTIME backdrop, so the key is derived, not
  //     captured. 'vr' ⇔ opaque, 'ar' ⇔ passthrough — key names kept from 078 so stored
  //     operator choices are not silently discarded.
  eq(terrainStyleKeyFor(true), 'obi.viewer.terrainStyle.vr',
    'backdrop: opaque reads the vr terrain-style key');
  eq(terrainStyleKeyFor(false), 'obi.viewer.terrainStyle.ar',
    'backdrop: passthrough reads the ar terrain-style key');
  eq(terrainStyleDefaultFor(true), 'shaded',
    'backdrop: opaque defaults to the lit shaded relief');
  eq(terrainStyleDefaultFor(false), 'wire',
    'backdrop: passthrough defaults to the see-through wireframe');
  ok(terrainStyleKeyFor(true) !== terrainStyleKeyFor(false),
    'backdrop: the two backdrops never share a terrain-style key');

  // C6: one owner for the backdrop choice, on localStorage like every other viewer pref.
  eq(BACKDROP_KEY, 'obi.viewer.backdrop', 'backdrop: one canonical preference key');

  // C6 round-trip + FR-015 degrade. The picker and the in-session row share these two
  // functions precisely so they cannot disagree; the default must land on opaque (today's VR
  // default) for every unreadable, absent, or unrecognised value.
  const { loadBackdropOpaque, saveBackdropOpaque } = bands;
  globalThis.localStorage = normalStorage();
  eq(loadBackdropOpaque(), true, 'backdrop: absent preference defaults to opaque');
  saveBackdropOpaque(false);
  eq(loadBackdropOpaque(), false, 'backdrop: passthrough round-trips');
  saveBackdropOpaque(true);
  eq(loadBackdropOpaque(), true, 'backdrop: opaque round-trips');

  globalThis.localStorage._store.set(BACKDROP_KEY, '{not json');
  eq(loadBackdropOpaque(), true, 'backdrop: a corrupt stored value degrades to opaque');
  globalThis.localStorage._store.set(BACKDROP_KEY, '"sideways"');
  eq(loadBackdropOpaque(), true, 'backdrop: an unrecognised literal degrades to opaque');

  globalThis.localStorage = throwingStorage();
  let threw = false;
  try {
    eq(loadBackdropOpaque(), true, 'backdrop: unreadable storage degrades to opaque');
    saveBackdropOpaque(false);
  } catch {
    threw = true;
  }
  ok(!threw, 'backdrop: a private-mode browser never throws out of the backdrop helpers');
  globalThis.localStorage = normalStorage();
}

console.log(JSON.stringify({ target: 'bands_modes', checks, failures }));
process.exit(failures.length ? 1 : 0);
