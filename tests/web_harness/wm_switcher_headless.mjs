// Headless logic harness for the window switcher (feature 077) — runs the REAL wm.js in node
// against a DOM stub and asserts contracts/window-switcher.md §1–§2: what `list()` enumerates, in
// what order, and what `activate()` actually does to a window.
//
// Why execute it rather than grep it: this feature has exactly two traps, and both are invisible to
// a source read.
//   1. `focus()` reorders z-index but never un-hides a minimized window (it sits at display:none),
//      so a focus-only `activate()` is a SILENT no-op — no throw, no visible change (C2.1/FR-017).
//   2. `serialize()` filters ephemeral windows for layout persistence, so sourcing the list there
//      silently omits chat/snapshot windows the operator is looking at (C1.2/FR-019).
// Both are asserted here against observable end state, not against which function got called.
//
// Same shape as kit_panel_headless.mjs / phy_headless.mjs: real module from the static dir, a small
// DOM stub, one JSON summary line, non-zero exit if any check failed.
//
// Usage: node wm_switcher_headless.mjs [static-dir]   (defaults to src/obi/web/static beside the repo)
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const staticDir = process.argv[2] ?? resolve(HERE, '../../src/obi/web/static');

// --- a small DOM stub -------------------------------------------------------------------------
//
// Nesting-aware, unlike phy_headless's flat parseHTML: wm.js's window template queries DESCENDANT
// selectors ('.name .base'), so the stub has to build a real tree and resolve a selector chain.

function parseHTML(html) {
  const roots = [];
  const stack = [];
  const re = /<\/?(\w+)([^>]*?)(\/?)>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    const [raw, tag, attrs, selfClose] = m;
    if (raw.startsWith('</')) {
      stack.pop();
      continue;
    }
    const el = makeEl(tag);
    const cls = /class="([^"]*)"/.exec(attrs);
    if (cls) el.className = cls[1];
    const type = /type="([^"]*)"/.exec(attrs);
    if (type) el.type = type[1];
    const title = /title="([^"]*)"/.exec(attrs);
    if (title) el.title = title[1];
    if (/\bhidden\b/.test(attrs)) el.hidden = true;
    const parent = stack[stack.length - 1];
    if (parent) parent.appendChild(el);
    else roots.push(el);
    if (!selfClose) stack.push(el);
  }
  return roots;
}

// One selector part: `.cls`, `tag`, or `tag.cls`.
function matchPart(node, part) {
  const dot = part.indexOf('.');
  if (dot === -1) return node.tagName === part.toUpperCase();
  const tag = part.slice(0, dot);
  const cls = part.slice(dot + 1);
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  return node.classList.contains(cls);
}

// Descendant chain ('.name .base'): every part must match an ancestor-to-self path, loosely — the
// same "some ancestor matched the earlier part" rule a real querySelector uses.
function query(root, selector, all) {
  const chain = String(selector).trim().split(/\s+/);
  const results = [];
  const walk = (node, depth) => {
    for (const child of node.children) {
      const hit = matchPart(child, chain[depth]);
      if (hit && depth === chain.length - 1) results.push(child);
      else if (hit) walk(child, depth + 1);
      walk(child, depth); // a non-match (or a match) may still contain a match at this depth
    }
  };
  walk(root, 0);
  return all ? results : results[0] ?? null;
}

function makeEl(tag) {
  const classes = new Set();
  const listeners = {};
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {},
    dataset: {},
    _attrs: {},
    children: [],
    parentElement: null,
    textContent: '',
    title: '',
    type: '',
    hidden: false,
    disabled: false,
    // wm.js reads these off the workspace root for clamping/snap zones.
    clientWidth: 1200,
    clientHeight: 800,
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
    set innerHTML(html) {
      this.children = [];
      parseHTML(html).forEach((child) => this.appendChild(child));
    },
    get innerHTML() { return ''; },
    appendChild(c) { c.parentElement = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => { if (c && typeof c === 'object') this.appendChild(c); }); },
    remove() {
      const p = this.parentElement;
      if (p) { p.children = p.children.filter((x) => x !== this); this.parentElement = null; }
    },
    querySelector(sel) { return query(this, sel, false); },
    querySelectorAll(sel) { return query(this, sel, true); },
    getBoundingClientRect() { return { width: 100, height: 40, left: 0, top: 0 }; },
    setPointerCapture() {},
    releasePointerCapture() {},
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    _fire(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev ?? {})); },
  };
  return el;
}

globalThis.document = {
  createElement: (t) => makeEl(t),
  addEventListener() {},
  removeEventListener() {},
};
globalThis.document.body = makeEl('body');
globalThis.window = { innerWidth: 1200, innerHeight: 800, addEventListener() {} };

// --- checks ------------------------------------------------------------------------------------

const checks = [];
const failures = [];
function ok(condition, label) {
  checks.push(label);
  if (!condition) failures.push(label);
}

const { createWindowManager } = await import(resolve(staticDir, 'wm.js'));

let mutations = 0;
const workspace = makeEl('div');
const dock = makeEl('div');
const wm = createWindowManager(workspace, dock, { onMutate: () => { mutations += 1; } });

// A def shaped like registry.js's: the fields wm.create() actually reads.
function def(id, mnemonic, title, extra = {}) {
  return { id, mnemonic, title, defaultSize: [400, 300], minSize: [200, 120], ...extra };
}

// 1) list(): shape + completeness ---------------------------------------------------------------
{
  const map = wm.create(def('map', 'MAP', 'link map'));
  const logs = wm.create(def('logs', 'LOGS', 'syslog'));

  const entries = wm.list();
  ok(Array.isArray(entries), 'list: returns an array');
  ok(entries.length === 2, 'list: one entry per open window');

  const entry = entries.find((e) => e.id === 'map');
  ok(entry?.mnemonic === 'MAP' && entry?.title === 'link map',
    'list: entry carries mnemonic + title');
  ok(entry?.minimized === false, 'list: a normal window is not flagged minimized');
  ok(Object.keys(entry).sort().join(',') === 'id,minimized,mnemonic,title',
    'list: entry is the {id, mnemonic, title, minimized} projection — no extra fields');
  // C1.5: plain data — no live window objects, no elements, no methods reachable from an entry.
  ok(Object.values(entry).every((v) => typeof v !== 'function' && typeof v !== 'object'),
    'list: entry is plain data (C1.5) — no live objects or methods leak to the renderer');

  map.close();
  logs.close();
  ok(wm.list().length === 0, 'list: returns [] when nothing is open (C1.6)');
}

// 2) list(): ORDER is z-order, top-most first, and tracks focus ---------------------------------
{
  const a = wm.create(def('a', 'A', 'first'));
  const b = wm.create(def('b', 'B', 'second'));
  const c = wm.create(def('c', 'C', 'third'));

  ok(wm.list().map((e) => e.id).join(',') === 'c,b,a',
    'list: top-most first — newest created is first (R2)');

  wm.focus(a);
  ok(wm.list().map((e) => e.id).join(',') === 'a,c,b',
    'list: order tracks focus — the just-focused window leads');

  // C1.7: reading the list must not perturb anything.
  const before = mutations;
  wm.list();
  wm.list();
  ok(mutations === before, 'list: side-effect free — no mutation notification (C1.7)');
  ok(wm.list().map((e) => e.id).join(',') === 'a,c,b', 'list: repeated reads are stable');

  a.close(); b.close(); c.close();
}

// 3) list(): THE FR-019 TRAP — ephemeral windows are listed, though serialize() drops them -------
{
  const map = wm.create(def('map', 'MAP', 'link map'));
  const talk = wm.create(def('talk:abc', 'CHAT', 'ask obi', { ephemeral: true }));
  const snap = wm.create(def('snapshot:xyz', 'MAP', 'link map (snapshot)', { ephemeral: true }));

  const listed = wm.list().map((e) => e.id).sort();
  const persisted = wm.serialize().map((w) => w.widget).sort();

  ok(listed.join(',') === 'map,snapshot:xyz,talk:abc',
    'list: ephemeral windows ARE listed — the switcher answers "what is open" (C1.2/FR-019)');
  ok(persisted.join(',') === 'map',
    'serialize: still filters ephemeral windows — correct FOR PERSISTENCE, and unchanged');
  ok(listed.length > persisted.length,
    'list != serialize — proving the switcher does not source from the persistence view');

  map.close(); talk.close(); snap.close();
}

// 4) list(): titles are read LIVE (a renamed window shows its current name) ----------------------
{
  // A rename goes through the SHELL's ctx.setName, not wm's raw win.setName: the shell updates the
  // title element AND syncs win.def.title (the 056 fix, added so wm reads the real title when it
  // builds a dock chip). Reproduced faithfully here — calling win.setName alone would only repaint
  // the DOM and would not be the path any widget actually takes.
  const shellSetName = (win, name) => { win.setName(name); if (name) win.def.title = name; };

  const ros = wm.create(def('ros:10.20.0.1', 'ROS', '10.20.0.1'));
  ok(wm.list()[0].title === '10.20.0.1', 'list: title starts as the def title');

  // 047: a router window renames itself once its live read resolves the identity.
  shellSetName(ros, 'core-gw-1');
  ok(wm.list()[0].title === 'core-gw-1',
    'list: title is read live — a rename is reflected without re-opening (C1.4/FR-014)');

  // list() and the dock chip read the SAME field, so a menu entry and a chip can never disagree.
  ros.minimize();
  const chipTitle = dock.children[0]?.querySelector('.ti')?.textContent;
  ok(chipTitle === 'core-gw-1' && wm.list()[0].title === chipTitle,
    'list: the menu entry and the dock chip agree — one title source, not two');
  ros.close();
}

// 5) activate(): focuses a buried window without touching geometry or state ----------------------
{
  const a = wm.create(def('a', 'A', 'first'));
  const b = wm.create(def('b', 'B', 'second'));
  wm.create(def('c', 'C', 'third'));

  a.toggleMaximize();
  const geometry = { x: a.x, y: a.y, w: a.w, h: a.h, state: a.state };
  wm.focus(b); // bury the maximized window

  wm.activate('a');
  ok(wm.list()[0].id === 'a', 'activate: raises the window to the front (C2.2/FR-015)');
  ok(a.el.classList.contains('focused'), 'activate: the raised window is marked focused');
  ok(a.state === geometry.state && a.x === geometry.x && a.y === geometry.y
    && a.w === geometry.w && a.h === geometry.h,
    'activate: a maximized window stays maximized — geometry/state untouched (C2.2/FR-016)');

  wm.list().forEach((e) => wm.get(e.id)?.close());
}

// 6) activate(): an unknown id is a silent no-op -------------------------------------------------
{
  const map = wm.create(def('map', 'MAP', 'link map'));
  const before = mutations;
  let threw = false;
  try {
    wm.activate('ros:nope'); // the operator selected an entry whose window just closed
    wm.activate(undefined);
    wm.activate(null);
  } catch (err) {
    threw = true;
  }
  ok(!threw, 'activate: an unknown id does not throw (C2.3/FR-023)');
  ok(mutations === before, 'activate: an unknown id mutates nothing');
  ok(wm.list().length === 1, 'activate: an unknown id leaves the workspace untouched');
  map.close();
}

// 7) THE FR-017 TRAP: activate() on a minimized window RESTORES, it does not merely focus ---------
{
  const map = wm.create(def('map', 'MAP', 'link map'));
  wm.create(def('logs', 'LOGS', 'syslog')); // something to be buried under

  map.minimize();
  ok(wm.list().find((e) => e.id === 'map')?.minimized === true,
    'list: a minimized window is still listed, flagged minimized (C1.3/FR-018)');
  ok(map.el.style.display === 'none', 'minimize: the window is hidden (the premise of the trap)');
  ok(dock.children.length === 1, 'minimize: a dock chip appears (unchanged 037 behavior)');

  wm.activate('map');

  // The assertions are on OBSERVABLE STATE, not on which function ran: a focus-only activate()
  // would leave display:none and the chip in place, and would pass any "did we call focus?" check.
  ok(map.el.style.display !== 'none',
    'activate: a minimized window is RESTORED, not merely focused (C2.1/FR-017)');
  ok(map.state === 'normal', 'activate: state returns to its pre-minimize value');
  ok(dock.children.length === 0, 'activate: the dock chip is removed on restore');
  ok(map.el.classList.contains('focused'), 'activate: the restored window takes focus');
  ok(wm.list()[0].id === 'map' && wm.list()[0].minimized === false,
    'activate: the restored window leads the list and is no longer flagged');

  wm.list().forEach((e) => wm.get(e.id)?.close());
}

// 8) restore returns to the PRIOR state, not a hardcoded 'normal' (US3 scenario 3) ---------------
{
  const max = wm.create(def('max', 'MAX', 'maximized one'));
  max.toggleMaximize();
  ok(max.state === 'max', 'setup: window is maximized');
  max.minimize();
  wm.activate('max');
  ok(max.state === 'max',
    'activate: minimized-while-maximized comes back MAXIMIZED (US3 scenario 3)');
  max.close();

  // A snapped window is constructed directly: snapTo() is internal to wm (a drag gesture arms it)
  // and is not on the win handle, so there is no public way to snap one here. Setting the state wm
  // itself would set is faithful for the restore path, which reads stateBeforeMin/snapZone — and it
  // beats a `snapped.snapTo?.(...)` that silently no-ops and asserts 'normal' === 'normal'.
  const snapped = wm.create(def('snap', 'SNAP', 'snapped one'));
  snapped.state = 'snapped';
  snapped.snapZone = 'left';
  snapped.minimize();
  ok(snapped.state === 'min', 'setup: the snapped window minimized');
  wm.activate('snap');
  ok(snapped.state === 'snapped' && snapped.snapZone === 'left',
    'activate: minimized-while-snapped comes back SNAPPED, not normal (US3 scenario 3)');
  snapped.close();
}

// 9) menu-restore and dock-chip-restore reach the SAME state (C2.5/US3 scenario 4) ---------------
{
  const viaMenu = wm.create(def('a', 'A', 'via menu'));
  viaMenu.toggleMaximize();
  viaMenu.minimize();
  wm.activate('a');
  const menuResult = { state: viaMenu.state, display: viaMenu.el.style.display, chips: dock.children.length };
  viaMenu.close();

  const viaChip = wm.create(def('b', 'B', 'via chip'));
  viaChip.toggleMaximize();
  viaChip.minimize();
  dock.children[0]._fire('click'); // the operator clicks the chip instead of the menu entry
  const chipResult = { state: viaChip.state, display: viaChip.el.style.display, chips: dock.children.length };
  viaChip.close();

  ok(JSON.stringify(menuResult) === JSON.stringify(chipResult),
    'activate: restoring from the menu is indistinguishable from clicking the dock chip (C2.5)');
}

// 10) OPENING a window notifies — a read-only window survives a reload -------------------------
//
// The regression this guards: `serialize()` always reported the open windows correctly, but
// `create()` did not call mutated(), so the shell's debounced save never ran on open. A window
// opened and then only READ (DASH, HEALTH — boards you look at, never drag) was in no saved
// layout: it opens on top, so clicking it lands in the focus() branch that restacks WITHOUT
// notifying. Reload, and every other window came back while that one was gone.
//
// Asserted against the end state the operator actually gets — what a reload would rebuild —
// by modelling the shell's persistence exactly: the layout is (re)written only when onMutate
// fires, so a missing notification shows up as a stale layout, not as a call that didn't happen.
{
  let persisted = null; // what localStorage would hold; only onMutate rewrites it
  const space = makeEl('div');
  const bar = makeEl('div');
  const shell = createWindowManager(space, bar, {
    onMutate: () => { persisted = shell.serialize().map((w) => w.widget); },
  });

  const map = shell.create(def('map', 'MAP', 'link map'));
  ok(persisted?.join(',') === 'map', 'open: the first window is persisted as it opens');

  const dash = shell.create(def('dash', 'DASH', 'status dashboard'));
  ok(persisted?.includes('dash'),
    'open: a newly opened window reaches the saved layout without any further gesture');

  // The operator reads the board: clicks it (it is already top-most) and expands a tile. Both go
  // through focus(); neither moves it. This is the path that used to persist nothing.
  dash.focus();
  dash.el._fire('pointerdown', { target: { closest: () => null } });
  ok(persisted?.join(',') === 'map,dash',
    'open: reading a top-most window does not drop it from the saved layout');

  // And the saved rect is the one a reload would rebuild it at.
  const entry = shell.serialize().find((w) => w.widget === 'dash');
  ok(entry && Number.isFinite(entry.x) && Number.isFinite(entry.y)
    && entry.w === 400 && entry.h === 300 && entry.state === 'normal',
    'open: the persisted entry carries a restorable rect + state');

  map.close();
  dash.close();
  ok(persisted?.length === 0, 'close: still notifies — a closed window leaves the saved layout');
}

console.log(JSON.stringify({ target: 'wm_switcher', checks: checks.length, failures }));
process.exit(failures.length ? 1 : 0);
