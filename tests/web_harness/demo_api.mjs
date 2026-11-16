// Browser filter-engine harness (feature 136, T017): run demo/api.js filterLogs/filterAudit over
// the shipped slices for a list of queries, so the pytest wrapper can compare with the Python
// handlers on the same rows (research R9).
//
// Usage: node demo_api.mjs <api.js> <bundle-root> <queries.json> <demo-now-iso>
// Output (last line): JSON list of results, in input order.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto?.subtle) globalThis.crypto = webcrypto;
const [apiPath, root, queriesPath, nowIso] = process.argv.slice(2);
const api = await import(apiPath);
const nowMs = Date.parse(nowIso);
const logs = JSON.parse(readFileSync(join(root, 'data', 'logs-slice.json'), 'utf8'));
const audit = JSON.parse(readFileSync(join(root, 'data', 'audit-slice.json'), 'utf8'));
const out = [];
for (const { route, params } of JSON.parse(readFileSync(queriesPath, 'utf8'))) {
  if (route === 'logs') {
    out.push(api.filterLogs(logs.entries, params, nowMs).map((e) => [e.timestamp, e.host, e.message]));
  } else {
    const { events, total } = api.filterAudit(audit, params, nowMs);
    out.push({ ids: events.map((e) => e.id), total });
  }
}
console.log(JSON.stringify(out));
