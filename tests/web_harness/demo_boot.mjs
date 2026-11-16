// Boot-shim harness (feature 136, T019): load a bundle's demo/boot.js with a file-backed fetch,
// then exercise every URL template the workspace widgets use (research survey) and report what
// each one answered.
//
// Usage: node demo_boot.mjs <bundle-root>
// Output (last line): {"shellLoaded":bool,"demoNowYear":n,"answers":[{url,status,json,kind}...]}
import { readFile } from 'node:fs/promises';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto?.subtle) globalThis.crypto = webcrypto;
const root = process.argv[2];
const fetched = [];
globalThis.fetch = async (input) => {
  const url = typeof input === 'string' ? input : (input.href ?? input.url);
  fetched.push(url);
  if (!url.startsWith('file:')) return new Response('', { status: 599 });
  try {
    const body = await readFile(fileURLToPath(url));
    return new Response(body, { status: 200 });
  } catch {
    return new Response('', { status: 404 });
  }
};
await import(pathToFileURL(join(root, 'demo', 'boot.js')).href);

const URLS = [
  ['GET', '/snapshots/ospf.json'], ['GET', '/snapshots/status.json'], ['GET', '/voice.json'],
  ['GET', '/api/health.json'], ['GET', '/api/health.json?device=aspen-gw'],
  ['GET', '/api/watch.json?device=&severity=&window=24h'], ['GET', '/api/logs.json?window=24h'],
  ['GET', '/api/logs.json?window=24h&after=5'], ['GET', '/api/radio.json?window=24h&after=0'],
  ['GET', '/api/audit.json?view=list&limit=200'], ['GET', '/api/audit.json?view=stats&since=24h'],
  ['GET', '/api/metrics.json'], ['GET', '/api/metrics.json?target=aspen-gw&metric=cpuLoad&window=24h'],
  ['GET', '/api/configs.json'], ['GET', '/api/configs.json?device=aspen-gw'],
  ['GET', '/api/dash.json'], ['GET', '/api/inventory.json'],
  ['GET', '/api/router.json?device=172.20.0.1'], ['GET', '/api/phy.json?device=172.20.0.1'],
  ['POST', '/api/tool'], ['GET', '/healthz'], ['GET', '/links.json'],
];
const answers = [];
for (const [method, url] of URLS) {
  const response = await globalThis.fetch(url, { method });
  let parsed = null;
  try { parsed = JSON.parse(await response.text()); } catch { parsed = null; }
  answers.push({ url, status: response.status, json: parsed !== null,
                 store: parsed?.store ?? null, hint: parsed?.hint ?? null });
}
console.log(JSON.stringify({
  shellLoaded: globalThis.SHELL_LOADED === true,
  demoNowYear: new Date().getFullYear(),
  demo: globalThis.OBI_DEMO?.manifest?.format_version ?? null,
  rootRelativeLinks: fetched.some((u) => u.endsWith('/links.json') && u.startsWith('file:')),
  answers,
}));
