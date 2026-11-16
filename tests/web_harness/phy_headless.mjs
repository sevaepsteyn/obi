// Headless smoke harness for the router PHY console (widgets/phy.js, feature 075) + the router.js
// tab shell + the faceplates.js layout module. Mounts the REAL modules in node against a stub DOM
// (SVG-aware, with enough innerHTML/querySelector for the kit feed) and prints one JSON line of what
// actually rendered. Same reason as the viewer2d/ospf/link harnesses: a runtime-only error in
// mount()/render() passes `node --check` and every asset grep but ships a broken window — only
// executing it catches that. Synthetic + captured fixtures only; skipped where node is absent.
//
// Usage:
//   node phy_headless.mjs phy    <static-dir> <payload.json>   → mount phy.js against a payload
//   node phy_headless.mjs router <static-dir> <router.json>    → mount router.js (L3 default) — SC-007
//   node phy_headless.mjs lint   <static-dir>                  → faceplates.js invariant lint
import { readFileSync } from 'node:fs';

const target = process.argv[2];
const staticDir = process.argv[3];
const dataFile = process.argv[4];

// --- a small SVG-aware DOM stub ---------------------------------------------------------------

function parseHTML(html) {
  const els = [];
  const re = /<(\w+)([^>]*)>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const el = makeEl(m[1]);
    const cls = /class="([^"]*)"/.exec(m[2]);
    if (cls) el.className = cls[1];
    if (/\bhidden\b/.test(m[2])) el.hidden = true;
    els.push(el);
  }
  return els;
}

function query(root, sel, all) {
  const results = [];
  const wantClass = sel.startsWith('.') ? sel.slice(1) : null;
  const wantTag = wantClass ? null : sel.toUpperCase();
  const walk = (node) => {
    for (const c of node.children || []) {
      const match = wantClass ? c.classList.contains(wantClass) : c.tagName === wantTag;
      if (match) {
        results.push(c);
        if (!all) return true;
      }
      if (walk(c)) return true;
    }
    return false;
  };
  walk(root);
  return all ? results : results[0] || null;
}

function makeEl(tag, ns) {
  const classes = new Set();
  const el = {
    tagName: String(tag).toUpperCase(),
    namespaceURI: ns || null,
    _attrs: {},
    style: { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; }, getPropertyValue(k) { return this[k] ?? ''; } },
    children: [],
    parent: null,
    hidden: false,
    textContent: '',
    type: '',
    value: '',
    title: '',
    placeholder: '',
    clientWidth: 300,
    get className() { return [...classes].join(' '); },
    set className(v) {
      classes.clear();
      String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
      contains: (c) => classes.has(c),
    },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === 'class') this.className = v; },
    getAttribute(k) { return k === 'class' ? this.className : (this._attrs[k] ?? null); },
    removeAttribute(k) { delete this._attrs[k]; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => { if (c && typeof c === 'object') { c.parent = this; this.children.push(c); } }); },
    replaceChildren(...cs) { this.children = cs; cs.forEach((c) => { if (c) c.parent = this; }); },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); },
    _listeners: null,
    addEventListener(type, fn) { (this._listeners ||= {})[type] = (this._listeners[type] || []).concat(fn); },
    removeEventListener() {},
    _fire(type, ev) { (this._listeners?.[type] || []).forEach((fn) => fn(ev || {})); },
    setPointerCapture() {},
    getBoundingClientRect() { return { width: 80, height: 16, left: 0, top: 0 }; },
    set innerHTML(html) { this.children = parseHTML(html); this.children.forEach((c) => { c.parent = this; }); },
    get innerHTML() { return ''; },
    querySelector(sel) { return query(this, sel, false); },
    querySelectorAll(sel) { return query(this, sel, true); },
    get firstChild() { return this.children[0] || null; },
  };
  return el;
}

globalThis.document = {
  createElement: (t) => makeEl(t),
  createElementNS: (ns, t) => makeEl(t, ns),
  body: makeEl('body'),
  addEventListener() {},
  removeEventListener() {},
};
globalThis.window = { devicePixelRatio: 1 };
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.requestAnimationFrame = (fn) => { fn(); return 0; };

const flush = () => new Promise((r) => setTimeout(r, 10));

// --- inspection helpers -----------------------------------------------------------------------

function findByClass(root, cls) {
  return query(root, `.${cls}`, false);
}
function allByClass(root, cls) {
  return query(root, `.${cls}`, true);
}

// --- lint target: faceplates.js invariants ----------------------------------------------------

if (target === 'lint') {
  const { FACEPLATES, layoutFor, genericLayout, normalizeModel } = await import(`${staticDir}/faceplates.js`);
  const expected = {
    'CRS318-16P-2S+': 18, 'CRS328-24P-4S+': 28, 'CCR2004-1G-12S+2XS': 15,
    'RB5009UPr+S+': 9, 'CRS326-24S+2Q+': 33, // ether1 mgmt + 24 SFP+ + 2 QSFP cages ×4 channels (T036)
  };
  const problems = [];
  const poeModels = new Set(['CRS318-16P-2S+', 'CRS328-24P-4S+', 'RB5009UPr+S+']);
  for (const [key, layout] of Object.entries(FACEPLATES)) {
    if (layout.ports.length !== expected[key]) problems.push(`${key} count ${layout.ports.length} != ${expected[key]}`);
    const ids = new Set();
    const cells = new Set();
    for (const p of layout.ports) {
      if (ids.has(p.id)) problems.push(`${key} dup id ${p.id}`);
      ids.add(p.id);
      const cell = `${p.row},${p.col}`;
      if (cells.has(cell)) problems.push(`${key} dup cell ${cell} (${p.id})`);
      cells.add(cell);
      if (p.row >= layout.rows) problems.push(`${key} ${p.id} row ${p.row} >= rows ${layout.rows}`);
      if (p.poe && !poeModels.has(key)) problems.push(`${key} ${p.id} poe on non-poe model`);
    }
  }
  // normalization + generic fallback
  const norm = normalizeModel('RB5009UPr+S+IN');
  if (norm !== 'RB5009UPr+S+') problems.push(`normalize suffix: ${norm}`);
  const generic = layoutFor('TotallyUnknown-99', [
    { default_name: 'ether1' }, { default_name: 'ether2' }, { default_name: 'sfp-sfpplus1' },
  ]);
  if (generic.curated !== false) problems.push('unknown model should be generic');
  if (generic.layout.ports.length !== 3) problems.push('generic port count');
  console.log(JSON.stringify({ target: 'lint', problems, models: Object.keys(FACEPLATES).length }));
  process.exit(0);
}

// --- router target: mount router.js, assert the L3 pane renders its feeds (SC-007) ------------

if (target === 'router') {
  const routerPayload = JSON.parse(readFileSync(dataFile, 'utf8'));
  globalThis.fetch = async (url) => {
    if (String(url).includes('/api/router.json')) {
      return { ok: true, status: 200, json: async () => routerPayload };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  const host = makeEl('div');
  let mountError = null;
  try {
    const mod = await import(`${staticDir}/widgets/router.js`);
    await mod.mount(host, {
      mode: 'window', id: 'ros:172.25.110.1', params: { address: '172.25.110.1', identity: 'test-gw' },
      setName() {}, setBadge() {}, setTitle() {},
    });
    await flush();
  } catch (err) {
    mountError = String(err && err.stack ? err.stack : err);
  }
  const tabs = allByClass(host, 'kit-tab');
  const rows = allByClass(host, 'kit-row');
  console.log(JSON.stringify({
    target: 'router',
    mountError,
    tabCount: tabs.length,
    activeTab: (tabs.find((t) => t.classList.contains('active')) || {}).textContent || null,
    l3Rows: rows.length,
  }));
  process.exit(0);
}

// --- phy target: mount phy.js against a payload, describe the rendered faceplate ---------------

const payload = JSON.parse(readFileSync(dataFile, 'utf8'));
globalThis.fetch = async (url) => {
  if (String(url).includes('/api/phy.json')) {
    return { ok: true, status: 200, json: async () => payload };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};

const host = makeEl('div');
let mountError = null;
let ctrl = null;
try {
  const mod = await import(`${staticDir}/widgets/phy.js`);
  ctrl = await mod.mount(host, {
    mode: 'window', id: 'ros:172.25.110.1', params: { address: '172.25.110.1', identity: 'test-gw' },
    setName() {}, setBadge() {}, setTitle() {},
  });
  ctrl.activate();
  await flush();
} catch (err) {
  mountError = String(err && err.stack ? err.stack : err);
}

const faceplate = findByClass(host, 'phy-faceplate');
const portNodes = faceplate ? allByClass(faceplate, 'phy-port') : [];
const cells = {};
for (const g of portNodes) {
  const id = g.getAttribute('data-port');
  const rect = (g.children || []).find((c) => c.classList.contains('phy-cell'));
  cells[id] = rect ? [...['phy-lit', 'phy-amber', 'phy-off', 'phy-disabled']].find((k) => rect.classList.contains(k)) || null : null;
}
const cageOccupied = faceplate ? allByClass(faceplate, 'phy-cage-occupied').length : 0;
// 076: the device-state box is now .kit-message, the status line .kit-status, facts .kit-facts.
const stateEl = findByClass(host, 'kit-message');
const statusEl = findByClass(host, 'kit-status');
const fwFlag = findByClass(host, 'phy-fw-behind');
const summaryFacts = allByClass(host, 'kit-facts').map((f) => (f.children[1] || {}).textContent).filter(Boolean);

// --- US2 interaction: click a port → inspector; row click → selection; 056 context descriptors --
const selectedDataPort = () => {
  const g = portNodes.find((n) => n.classList.contains('phy-selected'));
  return g ? g.getAttribute('data-port') : null;
};
let clickedHead = null;
let clickedFieldCount = 0;
let inspectorOpen = false;
let selectedAfterPortClick = null;
let portContextSubject = null;
let rowContextSubject = null;
let selectedAfterRowClick = null;
const rows = allByClass(host, 'kit-row');
if (!mountError && faceplate) {
  const clickable = portNodes.find((n) => n.__obiContext);
  if (clickable) {
    portContextSubject = clickable.__obiContext.contextFor?.().subject || null;
    clickable._fire('click');
    // 076: the inspector is now a kit panel (title in .kit-panel-title, the facts dl in
    // .kit-panel-body); before 075 it was an in-flow .phy-inspector div.
    const panel = findByClass(host, 'kit-panel');
    inspectorOpen = Boolean(panel);
    if (panel) {
      clickedHead = (findByClass(panel, 'kit-panel-title') || {}).textContent || null;
      const dl = (findByClass(panel, 'kit-panel-body')?.children || [])[0];
      clickedFieldCount = dl ? (dl.children || []).filter((c) => c.tagName === 'DT').length : 0;
    }
    selectedAfterPortClick = selectedDataPort();
  }
  if (rows.length) {
    const rc = rows[0].__obiContext;
    if (rc && rc.contextFor) {
      rowContextSubject = rc.contextFor({ row: rc.row, columns: rc.columns }).subject;
    }
    rows[rows.length - 1]._fire('click');
    selectedAfterRowClick = selectedDataPort();
  }
}

// 076: the inspector is now dismissible — clicking the panel's ✕ closes it AND deselects the port.
let dismissedClosesPanel = null;
let dismissedDeselects = null;
if (!mountError && faceplate && findByClass(host, 'kit-panel')) {
  const closeBtn = findByClass(host, 'kit-panel-close');
  if (closeBtn) closeBtn._fire('click', { target: closeBtn });
  dismissedClosesPanel = findByClass(host, 'kit-panel') === null;
  dismissedDeselects = selectedDataPort() === null;
}

// Table sections (US3/US4): each .phy-table-section carries a heading + a kit feed; report its
// visibility, row count, and whether it shows a degraded banner.
const sections = allByClass(host, 'phy-table-section').map((c) => {
  const heading = (c.children || []).find((x) => x.classList.contains('phy-section'));
  return {
    label: heading ? heading.textContent : null,
    hidden: c.hidden === true,
    rows: query(c, '.kit-row', true).length,
    degraded: Boolean(query(c, '.kit-state-degraded', false)),
  };
});
const poeBars = faceplate ? allByClass(faceplate, 'phy-poe-bar').length : 0;
const badgeCount = faceplate ? allByClass(faceplate, 'phy-badge').length : 0;
const badgedPorts = portNodes.filter((g) => g.classList.contains('phy-warn'))
  .map((g) => g.getAttribute('data-port'));

console.log(JSON.stringify({
  target: 'phy',
  mountError,
  faceplate: Boolean(faceplate),
  portCount: portNodes.length,
  cells,
  cageOccupied,
  poeBars,
  badgeCount,
  badgedPorts,
  sections,
  state: stateEl && !stateEl.hidden ? stateEl.textContent : null,
  statusClass: statusEl ? statusEl.className : null,
  statusText: statusEl ? statusEl.textContent : null,
  fwBehind: Boolean(fwFlag),
  summaryFacts,
  rowCount: rows.length,
  clickedHead,
  clickedFieldCount,
  inspectorOpen,
  selectedAfterPortClick,
  portContextSubject,
  rowContextSubject,
  selectedAfterRowClick,
  dismissedClosesPanel,
  dismissedDeselects,
}));
