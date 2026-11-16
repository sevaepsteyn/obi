// Headless smoke harness for the 096 health surfaces: the HLT fleet board (widgets/hlt.js)
// and the router-window HEALTH tab (widgets/rhealth.js). Mounts the REAL modules in node
// against the shared stub DOM and prints one JSON line of what actually rendered — a
// runtime-only mount error passes every grep but ships a broken window; only executing
// catches it. Synthetic payloads only (the route contract tests pin the server side).
//
// Usage:
//   node health_headless.mjs hlt     <static-dir> <payload.json>
//   node health_headless.mjs rhealth <static-dir> <payload.json>
import { readFileSync } from 'node:fs';

const target = process.argv[2];
const staticDir = process.argv[3];
const dataFile = process.argv[4];

// --- the shared stub DOM (verbatim from phy_headless.mjs) --------------------------------------

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

function findByClass(root, cls) {
  return query(root, `.${cls}`, false);
}
function allByClass(root, cls) {
  return query(root, `.${cls}`, true);
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
let intervalsStarted = 0;
let intervalsCleared = 0;
globalThis.setInterval = () => { intervalsStarted += 1; return intervalsStarted; };
globalThis.clearInterval = (id) => { if (id) intervalsCleared += 1; };
globalThis.requestAnimationFrame = (fn) => { fn(); return 0; };

const flush = () => new Promise((r) => setTimeout(r, 10));

const payload = JSON.parse(readFileSync(dataFile, 'utf8'));
globalThis.fetch = async (url) => {
  if (String(url).includes('/api/health.json')) {
    return { ok: true, status: 200, json: async () => payload };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};

function rowTexts(root) {
  return allByClass(root, 'kit-row').map((row) =>
    (row.children || []).map((c) => c.textContent).filter(Boolean).join(' | '));
}

if (target === 'hlt') {
  const host = makeEl('div');
  let mountError = null;
  const opened = [];
  try {
    const mod = await import(`${staticDir}/widgets/hlt.js`);
    await mod.mount(host, {
      mode: 'window', id: 'hlt',
      setName() {}, setBadge() {}, setTitle() {},
      openRouter: (address, identity) => opened.push({ address, identity }),
    });
    await flush();
  } catch (err) {
    mountError = String(err && err.stack ? err.stack : err);
  }
  const rows = allByClass(host, 'kit-row');
  if (rows[0]) rows[0]._fire('click'); // worst-first ⇒ the breaching router drills down
  const statusEl = findByClass(host, 'kit-state') || findByClass(host, 'kit-message');
  console.log(JSON.stringify({
    target: 'hlt',
    mountError,
    rowCount: rows.length,
    rows: rowTexts(host),
    opened,
    emptyState: statusEl ? statusEl.textContent : null,
  }));
  process.exit(0);
}

if (target === 'rhealth') {
  const host = makeEl('div');
  let mountError = null;
  let ctrl = null;
  try {
    const mod = await import(`${staticDir}/widgets/rhealth.js`);
    ctrl = await mod.mount(host, {
      mode: 'window', id: 'ros:172.25.110.1', params: { address: '172.25.110.1', identity: 'chestnut-gw' },
      setName() {}, setBadge() {}, setTitle() {},
    });
    const startedBeforeActivate = intervalsStarted;
    ctrl.activate();
    await flush();
    const startedAfterActivate = intervalsStarted;
    ctrl.deactivate();
    console.log(JSON.stringify({
      target: 'rhealth',
      mountError,
      rows: rowTexts(host),
      status: (findByClass(host, 'kit-status') || {}).textContent || null,
      polling: {
        beforeActivate: startedBeforeActivate,
        afterActivate: startedAfterActivate,
        cleared: intervalsCleared,
      },
    }));
  } catch (err) {
    mountError = String(err && err.stack ? err.stack : err);
    console.log(JSON.stringify({ target: 'rhealth', mountError }));
  }
  process.exit(0);
}

console.error(`unknown target ${target}`);
process.exit(2);
