// Headless smoke harness for the OSPF map widget (054).
//
// Mounts the REAL widgets/ospf.js in node with a stub DOM/canvas and a fixture snapshot, then
// prints one JSON line summarizing what actually drew. Exists because a runtime-only JS error
// (a reference to an undefined constant) once aborted draw() after the first edge stroke — the
// widget's own poll try/catch swallowed it, every asset-source assertion still matched, and the
// live map rendered as a single line. Syntax checks and string greps cannot catch that class of
// bug; actually executing draw() does.
//
// Usage: node ospf_headless.mjs <path-to-ospf.js> <path-to-snapshot.json>
// Output: {"mountError": null|string, "strokes": N, "curves": N, "nodeCircles": N, "texts": N}
import { readFileSync } from 'node:fs';

const modulePath = process.argv[2];
const snapshot = JSON.parse(readFileSync(process.argv[3], 'utf8'));

const calls = [];
const ctx2d = new Proxy({
  setTransform: () => {},
  clearRect: () => {},
  beginPath: () => {},
  moveTo: () => {},
  lineTo: () => {},
  quadraticCurveTo: () => calls.push(['quad']),
  stroke: () => calls.push(['stroke']),
  fill: () => {},
  arc: (x, y, r) => calls.push(['arc', r]),
  fillRect: () => {},
  fillText: () => calls.push(['fillText']),
  setLineDash: () => {},
  save: () => {},
  restore: () => {},
  translate: () => {},
  rotate: () => {},
  measureText: (t) => ({ width: String(t).length * 6.5 }),
}, {
  set(target, prop, value) { target[prop] = value; return true; },
  get(target, prop) { return prop in target ? target[prop] : () => {}; },
});

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {}, classList: { add: () => {}, toggle: () => {} },
    children: [], hidden: false, textContent: '', className: '', type: '', value: '',
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
    replaceChildren(...cs) { this.children = cs; },
    // 076: kit.createPanel (the ospf detail panel) builds chrome with setAttribute + title.
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, removeEventListener() {}, remove() {}, setPointerCapture() {},
  };
  if (tag === 'canvas') { el.getContext = () => ctx2d; el.width = 0; el.height = 0; }
  return el;
}

globalThis.document = {
  createElement: makeEl,
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => {},
};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.window = { devicePixelRatio: 1 };
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => snapshot });
globalThis.setInterval = () => 0; // no timers in the harness — one mount, one poll, one draw

const host = makeEl('div');
host.clientWidth = 800;
host.clientHeight = 560;

let mountError = null;
try {
  const mod = await import(modulePath);
  await mod.mount(host, {
    mode: 'window', id: 'ospf', params: null,
    setTitle() {}, setName() {}, setBadge() {},
    openOspf() {}, openOspfLint() {}, requestClose() {},
  });
} catch (err) {
  mountError = String(err && err.stack ? err.stack : err);
}

console.log(JSON.stringify({
  mountError,
  strokes: calls.filter((c) => c[0] === 'stroke').length,
  curves: calls.filter((c) => c[0] === 'quad').length,
  nodeCircles: calls.filter((c) => c[0] === 'arc' && c[1] === 7).length,
  texts: calls.filter((c) => c[0] === 'fillText').length,
}));
