// Headless logic harness for kit.createPanel (feature 076) — runs the REAL kit.js primitive in node
// against a small DOM stub and asserts its behavior contract (open/close/destroy listener discipline,
// the exact Esc-guard truth table, modal backdrop dismissal, open-while-open replacement, and the
// pop-out branch selection). Same rationale as phy_headless/ospf_headless: executing the code path is
// the only thing that catches a listener leak or a mis-wired guard. Prints one JSON summary line and
// exits non-zero if any check failed.
//
// Usage: node kit_panel_headless.mjs [static-dir]   (defaults to src/obi/web/static beside the repo)
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const staticDir = process.argv[2] ?? resolve(HERE, '../../src/obi/web/static');

// --- a small DOM stub (append/remove/closest/classList/matches + a keydown listener registry) ----

const keydownListeners = new Set();

function makeEl(tag) {
  const classes = new Set();
  const listeners = {};
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {},
    _attrs: {},
    children: [],
    parentElement: null,
    textContent: '',
    title: '',
    type: '',
    hidden: false,
    disabled: false,
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
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k] ?? null; },
    removeAttribute(k) { delete this._attrs[k]; },
    // Deep clone (for kit.inertClone): fresh element, copied class/text/attrs/disabled + children.
    cloneNode(deep) {
      const copy = makeEl(this.tagName.toLowerCase());
      copy.className = this.className;
      copy.textContent = this.textContent;
      copy.disabled = this.disabled;
      Object.assign(copy._attrs, this._attrs);
      if (deep) this.children.forEach((child) => copy.appendChild(child.cloneNode(true)));
      return copy;
    },
    // querySelectorAll: DESCENDANTS matching any comma-part — `tag`, `[attr]`, or `tag[attr]`.
    querySelectorAll(selector) {
      const parts = String(selector).split(',').map((s) => s.trim());
      const match = (node, sel) => {
        const m = sel.match(/^([a-zA-Z]*)(?:\[([a-zA-Z-]+)\])?$/);
        if (!m) return false;
        if (m[1] && node.tagName !== m[1].toUpperCase()) return false;
        if (m[2] && node.getAttribute(m[2]) == null) return false;
        return true;
      };
      const out = [];
      const walk = (node) => node.children.forEach((child) => {
        if (parts.some((sel) => match(child, sel))) out.push(child);
        walk(child);
      });
      walk(this);
      return out;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; },
    getBoundingClientRect() { return { width: 100, height: 40, left: 0, top: 0 }; },
    _adopt(c) { if (c && typeof c === 'object') { c.parentElement = this; } return c; },
    appendChild(c) { this._adopt(c); this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => { this._adopt(c); this.children.push(c); }); },
    replaceChildren(...cs) {
      this.children.forEach((c) => { if (c && c.parentElement === this) c.parentElement = null; });
      this.children = [];
      cs.forEach((c) => { this._adopt(c); this.children.push(c); });
    },
    remove() {
      const p = this.parentElement;
      if (p) { p.children = p.children.filter((x) => x !== this); this.parentElement = null; }
    },
    get firstElementChild() { return this.children[0] ?? null; },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    _fire(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev ?? {})); },
    // matches: supports the isTypingTarget selector + simple tag/class selectors.
    matches(sel) {
      return String(sel).split(',').map((s) => s.trim()).some((s) => {
        if (s.startsWith('.')) return classes.has(s.slice(1));
        return this.tagName === s.toUpperCase();
      });
    },
    // closest: walk self + ancestors for a class selector ('.win').
    closest(sel) {
      const want = sel.startsWith('.') ? sel.slice(1) : null;
      let node = this;
      while (node) {
        if (want ? [...node._classes()].includes(want) : node.tagName === sel.toUpperCase()) {
          return node;
        }
        node = node.parentElement;
      }
      return null;
    },
    _classes() { return classes; },
    isContentEditable: false,
  };
  return el;
}

globalThis.document = {
  createElement: (t) => makeEl(t),
  addEventListener(type, fn) { if (type === 'keydown') keydownListeners.add(fn); },
  removeEventListener(type, fn) { if (type === 'keydown') keydownListeners.delete(fn); },
};
globalThis.document.body = makeEl('body');
globalThis.window = { innerWidth: 1200, innerHeight: 800 };
if (!globalThis.crypto?.randomUUID) {
  globalThis.crypto = { randomUUID: () => `test-${Math.random().toString(16).slice(2)}` };
}

function fireKeydown(ev) { [...keydownListeners].forEach((fn) => fn(ev)); }
function findByClass(root, cls) {
  if (root.classList?.contains(cls)) return root;
  for (const child of root.children || []) {
    const hit = findByClass(child, cls);
    if (hit) return hit;
  }
  return null;
}

// --- test scaffold -------------------------------------------------------------------------------

const { createPanel, isTypingTarget, inertClone, createTooltip, createStatusLine, createRefreshButton }
  = await import(`${staticDir}/kit.js`);
const { snapshotWidgetDef, SNAP_ID_PREFIX } = await import(`${staticDir}/registry.js`);

const failures = [];
let checks = 0;
function ok(cond, label) {
  checks += 1;
  if (!cond) failures.push(label);
}

// A host inside an optional `.win` (focused or not); null win = a fullscreen page (no `.win`).
function makeHost({ win = 'focused' } = {}) {
  const host = makeEl('div');
  if (win) {
    const winEl = makeEl('section');
    winEl.className = win === 'focused' ? 'win focused' : 'win';
    winEl.appendChild(host);
  }
  return host;
}
const typingTarget = () => { const el = makeEl('input'); return el; };

// 1) open/close/destroy listener discipline + isOpen ---------------------------------------------
{
  const host = makeHost();
  const panel = createPanel(host, { mode: 'window' });
  ok(keydownListeners.size === 0 && !panel.isOpen(), 'closed: no keydown listener, not open');
  panel.open({ title: 'a', body: makeEl('div') });
  ok(keydownListeners.size === 1, 'open: exactly one keydown listener');
  ok(panel.isOpen() && host.children.length === 1, 'open: mounted + isOpen');
  panel.open({ title: 'b', body: makeEl('div') }); // re-open while open
  ok(keydownListeners.size === 1, 'open-while-open: still exactly one listener');
  ok(findByClass(host, 'kit-panel-title').textContent === 'b', 'open-while-open: title replaced');
  panel.close();
  ok(keydownListeners.size === 0 && !panel.isOpen(), 'close: listener removed, not open');
  panel.open({ title: 'c', body: makeEl('div') });
  panel.destroy();
  ok(keydownListeners.size === 0, 'destroy: listener removed');
  let threw = false;
  try { panel.open({ title: 'd', body: makeEl('div') }); } catch { threw = true; }
  ok(threw, 'destroy: subsequent open() throws');
}

// 1b) onClose fires on any open→closed transition (✕/Esc/programmatic), once per close ------------
{
  let closes = 0;
  const host = makeHost();
  const panel = createPanel(host, { mode: 'window' }, { onClose: () => { closes += 1; } });
  panel.open({ title: 'a', body: makeEl('div') });
  ok(closes === 0, 'onClose: not called while open');
  panel.close();
  ok(closes === 1, 'onClose: called once on close');
  panel.close(); // idempotent — no double fire when already closed
  ok(closes === 1, 'onClose: not re-fired when already closed');
}

// 2) isTypingTarget predicate --------------------------------------------------------------------
{
  ok(isTypingTarget(typingTarget()) === true, 'isTypingTarget: input is a typing target');
  const editable = makeEl('div'); editable.isContentEditable = true;
  ok(isTypingTarget(editable) === true, 'isTypingTarget: contenteditable');
  ok(isTypingTarget(makeEl('div')) === false, 'isTypingTarget: a div is not');
  ok(isTypingTarget(null) === false, 'isTypingTarget: null is not');
}

// 3) Esc-guard truth table -----------------------------------------------------------------------
function escCase(label, { win, target, key = 'Escape' }, shouldClose) {
  const host = makeHost({ win });
  const panel = createPanel(host, { mode: 'window' });
  panel.open({ title: 't', body: makeEl('div') });
  fireKeydown({ key, target });
  ok(panel.isOpen() === !shouldClose, label);
  panel.destroy();
}
escCase('esc: focused win + plain target → closes', { win: 'focused', target: makeEl('div') }, true);
escCase('esc: typing target → ignored', { win: 'focused', target: typingTarget() }, false);
escCase('esc: unfocused win → ignored', { win: 'unfocused', target: makeEl('div') }, false);
escCase('esc: fullscreen (no .win) → closes', { win: null, target: makeEl('div') }, true);
escCase('esc: non-Escape key → ignored', { win: 'focused', target: makeEl('div'), key: 'a' }, false);

// 4) modal backdrop-click dismissal --------------------------------------------------------------
{
  const host = makeHost();
  const panel = createPanel(host, { mode: 'window' }, { modal: true });
  panel.open({ title: 'm', body: makeEl('div') });
  const backdrop = findByClass(host, 'kit-panel-backdrop');
  const panelEl = findByClass(backdrop, 'kit-panel');
  ok(backdrop != null && panelEl != null, 'modal: backdrop wraps the panel');
  backdrop._fire('click', { target: panelEl }); // click on the panel, not the dim
  ok(panel.isOpen(), 'modal: click on panel does NOT dismiss');
  backdrop._fire('click', { target: backdrop }); // click on the dim
  ok(!panel.isOpen(), 'modal: click on backdrop dismisses');
  panel.destroy();
}

// 5) pop-out branch selection + button visibility ------------------------------------------------
function popCase(label, { ctx, popOut }, expect) {
  const host = makeHost();
  const panel = createPanel(host, ctx);
  const body = makeEl('div');
  panel.open({ title: 'pop', body, ...(popOut ? { popOut } : {}) });
  const btn = findByClass(host, 'kit-panel-popout');
  ok(btn.hidden === expect.hidden, `${label}: button hidden=${expect.hidden}`);
  if (!expect.hidden) {
    btn._fire('click', { target: btn });
    ok(expect.check(), `${label}: correct pop-out branch + closed`);
    ok(!panel.isOpen(), `${label}: panel closed after pop-out`);
  }
  panel.destroy();
}
{
  let thunkCalls = 0;
  let snapArgs = null;
  const ctxWin = { mode: 'window', openSnapshot: (t, b) => { snapArgs = [t, b]; } };
  // popOut declared → thunk runs, openSnapshot untouched
  popCase('popout thunk', { ctx: ctxWin, popOut: { open: () => { thunkCalls += 1; } } },
    { hidden: false, check: () => thunkCalls === 1 && snapArgs === null });
}
{
  let snapArgs = null;
  const ctxWin = { mode: 'window', openSnapshot: (t, b) => { snapArgs = [t, b]; } };
  // no popOut but openSnapshot present → snapshot fallback with (title, bodyNode)
  popCase('popout snapshot', { ctx: ctxWin },
    { hidden: false, check: () => snapArgs != null && snapArgs[0] === 'pop' && snapArgs[1] != null });
}
// window but no pop-out path (US1 intermediate) → button hidden
popCase('popout none (window, no snapshot)', { ctx: { mode: 'window' } }, { hidden: true });
// fullscreen → hidden; null ctx → hidden
popCase('popout none (fullscreen)', { ctx: { mode: 'fullscreen', openSnapshot: () => {} } },
  { hidden: true });
popCase('popout none (null ctx)', { ctx: null }, { hidden: true });

// 6) inertClone: controls disabled, hrefs stripped, ids stripped (kit.inertClone, US2) -----------
{
  const source = makeEl('div');
  source.setAttribute('id', 'root-id');
  const button = makeEl('button'); source.appendChild(button);
  const input = makeEl('input'); source.appendChild(input);
  const select = makeEl('select'); source.appendChild(select);
  const anchor = makeEl('a'); anchor.setAttribute('href', 'https://example/x'); source.appendChild(anchor);
  const inner = makeEl('span'); inner.setAttribute('id', 'inner-id'); source.appendChild(inner);

  const clone = inertClone(source);
  ok(clone !== source, 'inertClone: returns a distinct node');
  const [cButton, cInput, cSelect, cAnchor, cInner] = clone.children;
  ok(cButton.disabled === true && cInput.disabled === true && cSelect.disabled === true,
    'inertClone: button/input/select disabled');
  ok(cAnchor.getAttribute('href') === null, 'inertClone: anchor href stripped');
  ok(cInner.getAttribute('id') === null && clone.getAttribute('id') === null,
    'inertClone: ids stripped (root + descendants)');
  // The source is untouched — the clone is a separate, inert copy.
  ok(button.disabled === false && anchor.getAttribute('href') !== null && source.getAttribute('id') === 'root-id',
    'inertClone: the source node is left intact');
}

// 7) snapshotWidgetDef: ephemeral, snap: id, module, params, clamped sizes (US2) -----------------
{
  const body = makeEl('div');
  const def = snapshotWidgetDef('ether5 · sfp+', body, 1_700_000_000_000, [500, 400]);
  ok(def.id.startsWith(SNAP_ID_PREFIX), 'snapshotWidgetDef: snap: id');
  ok(def.ephemeral === true, 'snapshotWidgetDef: ephemeral flag');
  ok(def.mnemonic === 'SNAP', 'snapshotWidgetDef: SNAP fallback mnemonic when no source given');
  const inherited = snapshotWidgetDef('core-gw-1', body, 0, null, 'OSPF');
  ok(inherited.mnemonic === 'OSPF', 'snapshotWidgetDef: inherits the source window mnemonic');
  ok(def.module === './widgets/snap.js', 'snapshotWidgetDef: module');
  ok(def.title === 'ether5 · sfp+', 'snapshotWidgetDef: title = subject');
  ok(def.params.node === body && def.params.takenAt === 1_700_000_000_000,
    'snapshotWidgetDef: params carry the node + takenAt');
  ok(Array.isArray(def.defaultSize) && def.defaultSize[0] >= 320 && def.defaultSize[0] <= 720,
    'snapshotWidgetDef: defaultSize clamped');
  const huge = snapshotWidgetDef('x', body, 0, [5000, 5000]);
  ok(huge.defaultSize[0] <= 720 && huge.defaultSize[1] <= 640, 'snapshotWidgetDef: oversize clamped');
  const tiny = snapshotWidgetDef('x', body, 0, null);
  ok(tiny.defaultSize[0] === 460 && tiny.defaultSize[1] === 420, 'snapshotWidgetDef: no-size fallback');
}

// 8) createTooltip: show/move/hide/destroy (US3) -------------------------------------------------
{
  const before = globalThis.document.body.children.length;
  const tip = createTooltip();
  const node = globalThis.document.body.children[before];
  ok(node && node.classList.contains('kit-tooltip') && node.hidden === true,
    'createTooltip: a hidden .kit-tooltip is appended to body');
  tip.show('hello', { clientX: 10, clientY: 10 });
  ok(node.hidden === false && node.textContent === 'hello', 'createTooltip: show sets text + reveals');
  tip.hide();
  ok(node.hidden === true, 'createTooltip: hide conceals');
  tip.destroy();
  ok(globalThis.document.body.children.includes(node) === false, 'createTooltip: destroy removes node');
}

// 9) createStatusLine: tone classes + hide (US3) -------------------------------------------------
{
  const host = makeEl('div');
  const status = createStatusLine(host);
  ok(status.element.hidden === true, 'createStatusLine: starts hidden');
  status.set('reachable', 'ok');
  ok(!status.element.hidden && status.element.classList.contains('ok')
    && status.element.textContent === 'reachable', 'createStatusLine: set(ok) shows + tones');
  status.set('timeout', 'warn');
  ok(status.element.classList.contains('warn') && !status.element.classList.contains('ok'),
    'createStatusLine: tone swaps, old tone dropped');
  status.set(null);
  ok(status.element.hidden === true, 'createStatusLine: set(null) hides');
}

// 10) createRefreshButton: busy while pending, re-enabled on settle (US3) ------------------------
{
  let resolveRefresh;
  const button = createRefreshButton(() => new Promise((r) => { resolveRefresh = r; }));
  ok(button.className.includes('kit-btn') && button.textContent === '⟳ refresh',
    'createRefreshButton: a kit-btn labeled ⟳ refresh');
  ok(button.disabled === false, 'createRefreshButton: enabled at rest');
  button._fire('click');
  ok(button.disabled === true, 'createRefreshButton: disabled while the refresh promise is pending');
  resolveRefresh();
  await new Promise((r) => setTimeout(r, 0)); // let the finally{} run
  ok(button.disabled === false, 'createRefreshButton: re-enabled once the refresh settles');
}

console.log(JSON.stringify({ target: 'kit_panel', checks, failures }));
process.exit(failures.length ? 1 : 0);
