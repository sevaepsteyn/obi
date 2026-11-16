// Headless smoke harness for the DASH status board (widgets/dash.js, feature 095).
//
// Two jobs, one node run: (1) exercise the PURE view logic (isStale / sortItems / badgeText /
// tileState) with fixed clocks — the rules the browser must apply, testable without a DOM; and
// (2) mount the REAL widget against a stub DOM + stubbed /api/dash.json to catch runtime-only
// errors (the link_headless.mjs rationale). Prints one JSON line.
//
// Usage: node dash_headless.mjs <path-to-dash.js> <payload.json>
//   payload.json is the full route payload ({store, hint, board}) the stubbed fetch serves.
import { readFileSync } from 'node:fs';

const modulePath = process.argv[2];
const payloadPath = process.argv[3];
const payload = JSON.parse(readFileSync(payloadPath, 'utf8'));

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    children: [],
    className: '',
    textContent: '',
    append(...cs) { this.children.push(...cs); },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
    addEventListener() {}, removeEventListener() {}, remove() {},
  };
  return el;
}

globalThis.document = { createElement: makeEl };
globalThis.window = { devicePixelRatio: 1 };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => payload });

function findByClass(root, cls, out = []) {
  for (const child of root.children ?? []) {
    if (typeof child === 'object') {
      if (String(child.className).split(' ').includes(cls)) out.push(child);
      findByClass(child, cls, out);
    }
  }
  return out;
}

const mod = await import(modulePath);

// --- (1) pure view logic, fixed clocks ---------------------------------------------------------
const NOW_MS = 1_787_000_000_000; // fixed "now"
const fresh = { generated_at_us: (NOW_MS - 60_000) * 1000, state: 'yellow', tiles: [], notes: [] };
const old = { generated_at_us: (NOW_MS - 600_000) * 1000, state: 'green', tiles: [], notes: [] };
const isoOnly = { generated_at: new Date(NOW_MS - 30_000).toISOString(), state: 'green' };
const pure = {
  freshIsStale: mod.isStale(fresh, NOW_MS),
  oldIsStale: mod.isStale(old, NOW_MS),
  isoFallbackIsStale: mod.isStale(isoOnly, NOW_MS),
  sortedKeys: mod
    .sortItems([
      { key: 'b', label: 'b', state: 'green' },
      { key: 'c', label: 'c', state: 'red' },
      { key: 'a', label: 'a', state: 'green' },
      { key: 'd', label: 'd', state: 'stale' },
      { key: 'e', label: 'e', state: 'yellow' },
    ])
    .map((i) => i.key),
  badgeFresh: mod.badgeText(fresh, NOW_MS),
  badgeStale: mod.badgeText(old, NOW_MS),
  badgeDegraded: mod.badgeText(fresh, NOW_MS, { degraded: true }),
  tileStateForced: mod.tileState({ state: 'green' }, true),
  // 135: maintenance partition + banner text + whole-signal tile state
  partition: (() => {
    const p = mod.partitionItems([
      { key: 'm', label: 'm', state: 'yellow', maintenance: 'config' },
      { key: 'r', label: 'r', state: 'red' },
      { key: 'g', label: 'g', state: 'green' },
    ]);
    return { problems: p.problems.map((i) => i.key), maint: p.maint.map((i) => i.key), ok: p.ok.map((i) => i.key) };
  })(),
  maintLine: mod.maintenanceLine({
    sites: ['magnolia', 'manzanita'], signals: ['mesh'], uisp_flagged: 21, reason: 'teardown',
    unmatched: ['site shorline'],
  }),
  maintLineNone: mod.maintenanceLine(null),
  tileStateMaint: mod.tileState({ state: 'red', maintenance: 'config' }, false),
  tileStateMaintStale: mod.tileState({ state: 'red', maintenance: 'config' }, true),
};

// --- (2) mount the real widget against the stubbed route ---------------------------------------
const host = makeEl('div');
let badge = null;
const ctx = { mode: 'window', setBadge: (text) => { badge = text; } };

let mountError = null;
let handle = null;
try {
  handle = await mod.mount(host, ctx);
  await new Promise((resolve) => setTimeout(resolve, 20)); // let the poller's first refresh land
} catch (err) {
  mountError = String((err && err.stack) || err);
}

const tiles = findByClass(host, 'dash-tile').map((el) =>
  String(el.className).replace('dash-tile', '').trim(),
);
const message = findByClass(host, 'kit-message')[0]?.textContent ?? null;
const banner = findByClass(host, 'dash-stale-banner').length > 0;
const noteCount = findByClass(host, 'dash-note').length;
const maintBanner = findByClass(host, 'dash-maint-banner')[0]?.textContent ?? null;
const maintGroups = findByClass(host, 'dash-maint-group').map((el) => el.textContent);
const counts = findByClass(host, 'dash-tile-count').map((el) => el.textContent);
try { handle?.unmount?.(); } catch { /* unmount must never throw, but the smoke prints anyway */ }

console.log(
  JSON.stringify({
    mountError, tiles, message, banner, noteCount, badge, pure, maintBanner, maintGroups, counts,
  }),
);
