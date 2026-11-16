// Headless smoke harness for the 2D MAP widget (viewer2d.js, feature 055).
//
// Mounts the REAL viewer2d.js in node with a stub DOM/canvas + fixture links.json/terrain.json,
// then prints one JSON line summarizing what actually drew. Exists for the same reason as the
// OSPF harness (054): a runtime-only JS error in draw() passes `node --check` and every
// asset-source grep, then ships a broken map — executing draw() is the only thing that catches
// it (the LABEL_FULL_PX lesson, spec FR-015).
//
// Over the OSPF harness this adds: an `Image` stub whose `src` setter records the URL and fires
// `onload` synchronously (so the terrain underlay actually paints), stroke calls that carry the
// current strokeStyle (so the utilization fill is distinguishable from the health stroke), and a
// `fetch` stub serving the fixture files. A single clean draw is forced after mount (via the
// captured ResizeObserver callback) so the counts are deterministic, not draw-count-dependent.
//
// Usage: node viewer2d_headless.mjs <path-to-viewer2d.js> <fixture-dir> [terrain]
// Output: {"mountError":null|str,"curves":N,"strokes":N,"fillStrokes":N,"drawImages":N,"texts":N}
import { readFileSync } from 'node:fs';

const modulePath = process.argv[2];
const fixtureDir = process.argv[3];
const terrainPref = process.argv[4] === 'terrain';

const USAGE_FILL = 'rgba(230, 237, 243, 0.5)';
const links = readFileSync(`${fixtureDir}/links.json`, 'utf8');
const terrain = readFileSync(`${fixtureDir}/terrain.json`, 'utf8');

const calls = [];
const gc = { strokeStyle: '', lineWidth: 0, fillStyle: '', font: '', textAlign: '', globalAlpha: 1 };
let pathStart = null; // last moveTo — pairs each quad with its chord start (curve-side checks)
const ctx2d = new Proxy({
  setTransform() {}, clearRect() {}, beginPath() {}, closePath() {},
  moveTo: (x, y) => { pathStart = [x, y]; },
  lineTo: () => calls.push(['lineTo']),
  quadraticCurveTo: (cx, cy, x, y) =>
    calls.push(['quad', pathStart ? pathStart[0] : 0, pathStart ? pathStart[1] : 0, cx, cy, x, y]),
  stroke: () => calls.push(['stroke', gc.strokeStyle, gc.lineWidth]),
  fill: () => calls.push(['fill']),
  arc: (x, y, r) => calls.push(['arc', r]),
  fillRect() {},
  fillText: (t) => calls.push(['fillText', String(t)]),
  drawImage: () => calls.push(['drawImage']),
  setLineDash() {}, save() {}, restore() {}, translate() {}, rotate() {},
  measureText: (t) => ({ width: String(t).length * 6.5 }),
}, {
  set(target, prop, value) { gc[prop] = value; target[prop] = value; return true; },
  get(target, prop) {
    if (prop in target) return target[prop];
    return prop in gc ? gc[prop] : () => {};
  },
});

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {}, classList: { add: () => {}, remove: () => {}, toggle: () => {} },
    children: [], hidden: false, textContent: '', className: '', type: '', value: '',
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
    replaceChildren(...cs) { this.children = cs; },
    // 076: kit.createPanel (the in-canvas link detail) builds chrome with setAttribute.
    setAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, removeEventListener() {}, remove() {}, setPointerCapture() {},
  };
  if (tag === 'canvas') { el.getContext = () => ctx2d; el.width = 0; el.height = 0; }
  return el;
}

let resizeCb = null;
globalThis.document = { createElement: makeEl, addEventListener() {}, removeEventListener() {} };
// Terrain defaults ON, so an absent key now reads as on. To exercise the OFF path the stub must
// return the explicit opt-out sentinel ('off'); the terrainPref flag picks on vs off.
globalThis.localStorage = {
  getItem: (k) => (k === 'obi.viewer2d.terrain' ? (terrainPref ? 'on' : 'off') : null),
  setItem: () => {}, removeItem: () => {},
};
globalThis.window = { devicePixelRatio: 1 };
globalThis.requestAnimationFrame = () => 0;
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.ResizeObserver = class {
  constructor(cb) { resizeCb = cb; }
  observe() {} disconnect() {}
};
// The shade underlay loads via an Image (not fetch): src-setter records the URL, fires onload.
globalThis.Image = class {
  constructor() { this.onload = null; this.onerror = null; this._src = ''; }
  set src(url) { this._src = url; if (this.onload) this.onload(); }
  get src() { return this._src; }
};
globalThis.fetch = async (url) => {
  const u = String(url);
  if (u.endsWith('links.json')) return { ok: true, status: 200, json: async () => JSON.parse(links) };
  if (u.endsWith('terrain.json')) return { ok: true, status: 200, json: async () => JSON.parse(terrain) };
  return { ok: false, status: 404, json: async () => ({}) };
};

const host = makeEl('div');
host.clientWidth = 900;
host.clientHeight = 600;

let mountError = null;
try {
  const mod = await import(modulePath);
  await mod.start2D(host, { ageTarget: { set textContent(_v) { /* badge sink */ } } });
  // One clean, fully-loaded draw so the tallies don't depend on how many times draw() ran.
  calls.length = 0;
  if (resizeCb) resizeCb();
} catch (err) {
  mountError = String(err && err.stack ? err.stack : err);
}

console.log(JSON.stringify({
  mountError,
  curves: calls.filter((c) => c[0] === 'quad').length,
  // full segments [ax, ay, cx, cy, bx, by] so the pytest side can assert which SIDE of the
  // chord each parallel member bows to (the reversed-orientation overlap regression).
  curveSegs: calls.filter((c) => c[0] === 'quad').map((c) => c.slice(1)),
  strokes: calls.filter((c) => c[0] === 'stroke').length,
  fillStrokes: calls.filter((c) => c[0] === 'stroke' && c[1] === USAGE_FILL).length,
  drawImages: calls.filter((c) => c[0] === 'drawImage').length,
  texts: calls.filter((c) => c[0] === 'fillText').length,
}));
