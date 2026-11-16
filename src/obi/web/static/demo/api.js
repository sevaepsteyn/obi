// Static public demo (feature 136): the in-browser API. It is only ever copied into demo bundles
// and is never served live (the server's allowlist excludes demo/).
//
// demo/boot.js routes every root-absolute /api/*, /snapshots/* and /voice.json request here.
// Each route is answered as core/demo/routes.py catalogues it:
//   * rendered and snapshot responses come from data/api/<route>/<key>.json, whose key scheme
//     matches the Python exporter byte for byte (pinned by test_demo_routekey);
//   * logs and audit are filtered here from shipped slices with the live server's semantics
//     (pinned by test_demo_api_parity);
//   * live-device routes answer with the live error shape and the demo message.

export const KEY_PARAMS = {
  dash: [],
  inventory: [],
  health: ['device'],
  metrics: ['target', 'metric', 'window'],
  configs: ['device', 'view', 'at', 'from', 'to', 'limit'],
  router: ['device'],
};

export const UNAVAILABLE_MESSAGE = 'not available in the demo — this reads the device live';

// encodeURIComponent leaves !'()* unescaped; Python's quote(safe='-_.~') does not.
function encode(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
}

export function canonicalQuery(route, params) {
  const keys = KEY_PARAMS[route] ?? [];
  const pairs = [];
  for (const [k, v] of Object.entries(params)) {
    if (keys.includes(k) && v) pairs.push([k, String(v)]);
  }
  pairs.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  return pairs.map(([k, v]) => `${encode(k)}=${encode(v)}`).join('&');
}

async function sha1hex(text) {
  const digest = await globalThis.crypto.subtle.digest('SHA-1', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function routeKey(route, params) {
  const canonical = canonicalQuery(route, params);
  if (!canonical) return 'index';
  return (await sha1hex(canonical)).slice(0, 16);
}

export async function dataPath(route, params) {
  return `data/api/${route}/${await routeKey(route, params)}.json`;
}

// --- the bundle loader (configured by boot.js; by the node harness in tests) -------------------

let loadJson = async () => null;
let shippedSnapshots = null;
const cache = new Map();

export function configure({ load, manifest = null }) {
  loadJson = load;
  shippedSnapshots = Array.isArray(manifest?.snapshots) ? manifest.snapshots : null;
  cache.clear();
}

async function bundleJson(rel) {
  if (!cache.has(rel)) cache.set(rel, loadJson(rel));
  return cache.get(rel);
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

// --- shared filter vocabulary (logstore / auditstore semantics) ---------------------------------

export const SEVERITY_ORDER = ['debug', 'info', 'warning', 'error', 'critical'];
const CONTROLLER_COLLECTOR_TAGS = ['unifi', 'uisp'];
const RELATIVE_SECONDS = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 };
const ALLOWED_WINDOW = /^(\d+)\s*([smhdw])$/;

// FTS5 unicode61: lower-cased, diacritics removed, tokens are runs of letters/digits.
export function tokens(text) {
  return String(text ?? '').normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
    .split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

// A quoted FTS5 phrase matches when its tokens appear consecutively in one column.
export function phraseMatch(phrase, columns) {
  const want = tokens(phrase);
  if (!want.length) return true;
  return columns.some((col) => {
    const have = tokens(col);
    for (let i = 0; i + want.length <= have.length; i += 1) {
      if (want.every((t, j) => have[i + j] === t)) return true;
    }
    return false;
  });
}

function like(haystack, needle) { // SQLite LIKE '%needle%': ASCII case-insensitive substring
  return String(haystack ?? '').toLowerCase().includes(String(needle).toLowerCase());
}

export function relativeSince(raw, nowMs) {
  const m = ALLOWED_WINDOW.exec(String(raw ?? '').trim().toLowerCase());
  if (!m) return null;
  return nowMs - Number(m[1]) * RELATIVE_SECONDS[m[2]] * 1000;
}

function whenMs(raw, nowMs) {
  if (!raw) return null;
  const rel = relativeSince(raw, nowMs);
  if (rel !== null) return rel;
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? null : parsed;
}

// --- /api/logs.json over the shipped slice ------------------------------------------------------

export function filterLogs(entries, params, nowMs, { defaultLimit = 200 } = {}) {
  const source = params.source;
  const sinceMs = params.after !== undefined ? null : relativeSince(params.window || '24h', nowMs);
  const floor = params.severity ? SEVERITY_ORDER.indexOf(params.severity.toLowerCase()) : -1;
  const limit = params.limit ? Number(params.limit) : defaultLimit;
  const matched = entries.filter((e) => {
    if (sinceMs !== null && Date.parse(e.timestamp) < sinceMs) return false;
    if (params.device && !like(e.host, params.device)) return false;
    if (floor >= 0 && SEVERITY_ORDER.indexOf(e.severity ?? '') < floor) return false;
    if (source) {
      const tags = (Array.isArray(e.collectors) ? e.collectors : String(e.collectors ?? '').split(','))
        .map((t) => t.trim());
      if (source.toLowerCase() === 'syslog') {
        if (tags.some((t) => CONTROLLER_COLLECTOR_TAGS.includes(t))) return false;
      } else if (!like(tags.join(','), source)) return false;
    }
    if (params.q && !phraseMatch(params.q, [e.message, e.host, e.topics])) return false;
    return true;
  });
  return matched.slice(Math.max(0, matched.length - limit)); // newest N, ascending
}

async function logsAnswer(params) {
  const slice = await bundleJson('data/logs-slice.json');
  if (!slice) return json({ store: 'absent', mode: 'initial', entries: [], cursor: null,
    hint: 'no logs in this demo' });
  if (params.after !== undefined) {
    return json({ store: 'ok', mode: 'tail', entries: [], cursor: slice.cursor, hint: null });
  }
  return json({
    store: 'ok', mode: 'initial', cursor: slice.cursor,
    entries: filterLogs(slice.entries, params, Date.now()),
    hint: `demo: searching the newest ${slice.entries.length.toLocaleString()} of `
      + `${Number(slice.of).toLocaleString()} log lines`,
  });
}

// --- /api/audit.json over the shipped slice ------------------------------------------------------

export function filterAudit(slice, params, nowMs) {
  const sinceMs = whenMs(params.since, nowMs);
  const untilMs = whenMs(params.until, nowMs);
  const kinds = (params.kind || '').split(',').filter(Boolean);
  const sources = (params.source || '').split(',').filter(Boolean);
  const limit = params.limit ? Number(params.limit) : 100;
  const matched = slice.events.filter((e) => {
    const t = Date.parse(e.ts);
    if (sinceMs !== null && t < sinceMs) return false;
    if (untilMs !== null && t > untilMs) return false;
    if (kinds.length && !kinds.includes(e.kind)) return false;
    if (sources.length && !sources.includes(e.source)) return false;
    if (params.session && e.session_id !== params.session) return false;
    if (params.target && e.target !== params.target) return false;
    if (params.outcome && e.outcome !== params.outcome) return false;
    if (params.text) {
      const detail = slice.details[String(e.id)];
      const payload = detail ? JSON.stringify(detail.payload ?? '') : '';
      if (!phraseMatch(params.text, [e.summary, payload])) return false;
    }
    return true;
  });
  return { events: matched.slice(0, limit), total: matched.length }; // newest-first, as shipped
}

async function auditAnswer(params) {
  const slice = await bundleJson('data/audit-slice.json');
  const store = slice?.store ?? { present: false, enabled: false };
  if (!slice || !store.present) {
    return json({ events: [], total: 0, store, hint: 'no audit events in this demo' });
  }
  const view = params.view || 'list';
  if (view === 'detail') {
    const event = slice.details[String(params.id)];
    return event ? json({ event, store }) : json({ error: `no audit event with id ${params.id}` }, 404);
  }
  if (view === 'trace') {
    return json({ session: params.session, events: slice.traces[params.session] ?? [], store });
  }
  if (view === 'stats') {
    return json(slice.stats[params.since || '24h'] ?? slice.stats['24h'] ?? { store });
  }
  const { events, total } = filterAudit(slice, params, Date.now());
  return json({ events, total, store, hint: null });
}

// --- everything else ------------------------------------------------------------------------------

function unavailable(extra = {}) {
  return {
    store: 'absent', hint: UNAVAILABLE_MESSAGE, device: null,
    read: { outcome: 'unreachable', detail: UNAVAILABLE_MESSAGE, read_at: new Date().toISOString() },
    interfaces: [], addresses: [], truncated: false, ...extra,
  };
}

const MISSING = {
  health: (p) => ({ store: 'ok', device: p.device ?? null, readings: [], series: [], breaches: [],
    note: 'no health samples in this demo' }),
  metrics: (p) => ({ store: 'ok', mode: 'series', target: p.target, metric: p.metric, series: [],
    hint: null }),
  configs: (p) => ({ store: 'ok', mode: 'history', identity: p.device ?? null, found: false,
    entries: [], detail: 'not in the demo dataset' }),
  router: () => unavailable({ read: { outcome: 'unknown_device', detail: UNAVAILABLE_MESSAGE,
    read_at: new Date().toISOString() } }),
};

async function rendered(route, params) {
  let payload = await bundleJson(await dataPath(route, params));
  if (!payload) payload = MISSING[route] ? MISSING[route](params) : { store: 'absent', hint: null };
  if (route === 'metrics' && params.interface && Array.isArray(payload.series)) {
    payload = { ...payload, series: payload.series.filter((s) => s.ifname === params.interface) };
  }
  return json(payload);
}

const RENDERED_PATHS = {
  '/api/dash.json': 'dash', '/api/inventory.json': 'inventory', '/api/health.json': 'health',
  '/api/metrics.json': 'metrics', '/api/configs.json': 'configs', '/api/router.json': 'router',
};

// The demo's answer for one intercepted request, as a Response (or null: not a demo route).
export async function resolve(url, init = {}) {
  const u = new URL(url, 'demo:/'); // a parse-only base; never fetched
  const path = u.pathname;
  const params = Object.fromEntries(u.searchParams);
  const method = String(init.method || 'GET').toUpperCase();

  if (path.startsWith('/snapshots/')) {
    const name = path.slice('/snapshots/'.length);
    if (shippedSnapshots && !shippedSnapshots.includes(name)) {
      return json({ error: 'not part of this demo' }, 404);
    }
    const payload = await bundleJson(`data${path}`);
    return payload ? json(payload) : json({ error: 'not found' }, 404);
  }
  if (RENDERED_PATHS[path]) return rendered(RENDERED_PATHS[path], params);
  switch (path) {
    case '/api/logs.json': return logsAnswer(params);
    case '/api/audit.json': return auditAnswer(params);
    case '/api/watch.json': return json(await bundleJson('data/api/watch/index.json') ?? { store: 'absent', findings: [] });
    case '/api/radio.json': return json(await bundleJson('data/api/radio/index.json') ?? { store: 'absent', entries: [] });
    case '/api/phy.json': return json(unavailable());
    case '/api/tool':
      return method === 'POST'
        ? json({ ok: true, tool: 'demo', text: `error: ${UNAVAILABLE_MESSAGE}` })
        : json({ ok: false, error: 'method not allowed' }, 405);
    case '/api/radio-clip.wav': return new Response('', { status: 404 });
    case '/voice.json': return json({ enabled: true, demo: true, max_seconds: 30 });
    case '/api/turn':
      return new Response(`${JSON.stringify({ event: 'error', reason: 'demo',
        message: "This is a recorded preview — live chat isn't part of the demo." })}\n`,
      { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } });
    case '/healthz': return new Response('ok', { status: 200 });
    default: return null;
  }
}
