// Key-scheme parity harness (feature 136, T007): compute demo/api.js routeKey for a list of
// queries so the pytest wrapper can compare them with core/demo/routes.py route_key.
//
// Usage: node demo_routekey.mjs <path-to-demo/api.js> <queries.json>
// Output (last line): JSON list of keys, in input order.
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto?.subtle) globalThis.crypto = webcrypto;
const api = await import(process.argv[2]);
const queries = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const keys = [];
for (const { route, params } of queries) keys.push(await api.routeKey(route, params));
console.log(JSON.stringify(keys));
