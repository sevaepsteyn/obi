// Headless smoke harness for the LINK detail window (widgets/link.js, feature 058).
//
// Mounts the REAL widget in node with a stub DOM/canvas + fixture links.json/terrain.json, then
// prints one JSON line describing what actually rendered. Same reason as the viewer2d/ospf
// harnesses: a runtime-only error in mount()/render()/drawProfile() passes `node --check` and
// every asset-source grep but ships a broken window — only executing it catches that.
//
// Usage: node link_headless.mjs <path-to-link.js> <fixture-dir> <link-key>
// Output: {"mountError":null|str,"name":str|null,"rows":N,"health":str|null,"message":str|null,"profile":bool}
import { readFileSync } from 'node:fs';

const modulePath = process.argv[2];
const fixtureDir = process.argv[3];
const key = process.argv[4];
const links = readFileSync(`${fixtureDir}/links.json`, 'utf8');
const terrain = readFileSync(`${fixtureDir}/terrain.json`, 'utf8');

const ctx2d = new Proxy({}, { get: () => () => {} });

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    style: {}, classList: { add() {}, remove() {}, toggle() {} },
    children: [], className: '', textContent: '', clientWidth: 300,
    append(...cs) { this.children.push(...cs); },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...cs) { this.children = cs; },
    addEventListener() {}, removeEventListener() {}, remove() {},
  };
  if (tag === 'canvas') { el.getContext = () => ctx2d; el.width = 0; el.height = 0; }
  return el;
}

globalThis.document = { createElement: makeEl };
globalThis.window = { devicePixelRatio: 1 };
globalThis.requestAnimationFrame = (fn) => { fn(); return 0; };
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.fetch = async (url) => {
  const u = String(url);
  if (u.endsWith('links.json')) return { ok: true, status: 200, json: async () => JSON.parse(links) };
  if (u.endsWith('terrain.json')) return { ok: true, status: 200, json: async () => JSON.parse(terrain) };
  return { ok: false, status: 404, json: async () => ({}) };
};

const host = makeEl('div');
let name = null;
let health = null;

let mountError = null;
try {
  const mod = await import(modulePath);
  await mod.mount(host, {
    mode: 'window', id: 'link', params: { key },
    setName(v) { name = v; }, setBadge() {}, setTitle() {},
  });
} catch (err) {
  mountError = String(err && err.stack ? err.stack : err);
}

// The widget appends a .linkw-body div to the host; inspect its rendered children.
const body = host.children.find((c) => c.className === 'linkw-body');
const kids = body ? body.children : [];
const rows = kids.filter((c) => c.className === 'kit-facts').length; // 076: fact rows now .kit-facts
const healthEl = kids.find((c) => c.className === 'health');
health = healthEl ? healthEl.textContent : null;
const messageEl = kids.find((c) => c.className === 'kit-message'); // 076: message box now .kit-message
const profile = kids.some((c) => c.className === 'profile');

console.log(JSON.stringify({
  mountError,
  name,
  rows,
  health,
  message: messageEl ? messageEl.textContent : null,
  profile,
}));
