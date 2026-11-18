// obi board driver (feature 040) — the stamping mold for per-tool board widgets.
//
// One driver over the 038 kit's board mode: fetch a data source (a cron-written snapshot
// wrapper file, or a local-data /api route), map it to rows, replace the feed, wear the
// data's age + counts on the window badge, and stay honest about every degraded state
// (missing snapshot, absent store, failed poll — last-good rows always kept). A widget
// module contributes ONLY its spec: source, columns, row mapping, counts, filters
// (contracts/snapshots-and-routes.md). No tool-specific strings live here (tested).

import { createFeed, createFilterRow, createPoller } from './kit.js';

const DEFAULT_POLL_MS = 5 * 60 * 1000; // the export/snapshot cadence every surface shares
const AGE_MS = 30 * 1000;

export function formatAge(isoTs) {
  const then = Date.parse(isoTs);
  if (Number.isNaN(then)) return 'age ?';
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 1) return '<1m';
  if (minutes < 120) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

export function formatUptime(seconds) {
  if (seconds == null) return '';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

// 088: sub-minute durations (radio transmissions run 0.5s .. ~30s) — formatUptime floors
// at whole minutes and is the wrong tool. "3.4s" under ten seconds, "12s" under a minute,
// "1m05s" beyond.
export function formatDuration(ms) {
  if (ms == null) return '';
  if (ms < 9950) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.round((ms % 60000) / 1000);
  return `${minutes}m${String(seconds).padStart(2, '0')}s`;
}

// 063: memory used %, from free + total bytes. Two boards carried this identical formula under
// different local names; one home now. '' on missing/degenerate input (a board cell, so an empty
// string reads as "no data", never NaN%).
export function formatMemPercent(free, total) {
  if (free == null || total == null || total <= 0) return '';
  return `${Math.round((1 - free / total) * 100)}%`;
}

// 056: the visible projection of a row as `label: value` lines — exactly what is on screen, `_`-
// prefixed metadata excluded. The shared building block every board/table describe hook reuses so
// a right-click context is the row the operator sees, nothing hidden (constitution VI consent).
export function labeledFields(row, columns) {
  return (columns ?? [])
    .filter((column) => !String(column.key).startsWith('_'))
    .map((column) => `${column.label}: ${row[column.key] ?? ''}`)
    .join('\n');
}

// The default board describe: subject = the row's first (identity) column, element text = the
// labeled visible fields. A widget overrides `spec.contextFor` only to name its element kind (e.g.
// "router core-gw-1") — the projection stays honest either way.
function defaultContextFor(columns) {
  const identity = (columns ?? []).find((column) => !String(column.key).startsWith('_'));
  return ({ row }) => ({
    subject: String(identity ? row[identity.key] ?? '' : '').trim() || 'row',
    elementText: labeledFields(row, columns),
  });
}

export function createBoard(host, ctx, spec) {
  const filtersHost = document.createElement('div');
  const feedHost = document.createElement('div');
  feedHost.style.flex = '1';
  feedHost.style.minHeight = '0';
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.append(filtersHost, feedHost);

  const feed = createFeed(feedHost, {
    columns: spec.columns,
    cap: spec.cap ?? 500,
    tail: false,
    persist: ctx.id ?? null, // column widths persist per widget kind (046 US3)
    onRowClick: spec.onRowClick ?? null, // opt-in row detail (the spec renders the view)
    // 056: every board row is a right-click "ask obi" entry point. The widget names its element
    // kind via spec.contextFor; absent that, the generic identity-column default (FR-007).
    contextFor: spec.contextFor ?? defaultContextFor(spec.columns),
  });
  feed.showState('loading', 'loading…');

  let filterValues = {};
  let cachedPayload = null; // last-good data — re-filterable without refetching
  let ageIso = null;
  let countsText = null;

  function updateBadge() {
    const parts = [];
    if (countsText) parts.push(countsText);
    if (ageIso) parts.push(formatAge(ageIso));
    ctx.setBadge(parts.length ? parts.join(' · ') : null);
  }

  function render() {
    if (cachedPayload === null) return;
    let rows = spec.rows(cachedPayload);
    const text = (filterValues.text || '').toLowerCase();
    if (text) {
      // The generic client-side narrowing for snapshot boards: substring across cells. Keys
      // starting with `_` are row metadata (e.g. a stashed detail object, a tooltip string),
      // never displayed and never filtered against.
      rows = rows.filter((row) =>
        Object.entries(row)
          .filter(([key]) => !key.startsWith('_'))
          .some(([, value]) => String(value ?? '').toLowerCase().includes(text)),
      );
    }
    feed.replace(rows);
    if (!rows.length) feed.showState('empty', 'nothing matches');
    countsText = spec.counts ? spec.counts(cachedPayload) : null;
    ageIso = spec.age ? spec.age(cachedPayload) : ageIso;
    updateBadge();
  }

  // 043: the multi-snapshot kind. Fetch every url each poll; the cached payload becomes a
  // map key → {data, generated_at} | null (404 ⇒ null: that source is absent; a FAILED fetch
  // keeps that key's last-good — per-source honesty, FR-004). The badge age is the OLDEST
  // present source: two merged feeds must never look fresher than the staler one (FR-010).
  async function refreshSet() {
    const previous = cachedPayload ?? {};
    const merged = {};
    await Promise.all(
      Object.entries(spec.source.urls).map(async ([key, url]) => {
        try {
          const response = await fetch(url, { cache: 'no-store' });
          if (response.status === 404) {
            merged[key] = null;
            return;
          }
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const body = await response.json();
          merged[key] = { data: body.data, generated_at: body.generated_at ?? null };
        } catch {
          merged[key] = previous[key] ?? null; // failed poll: last-good rows stay
        }
      }),
    );
    if (Object.values(merged).every((entry) => entry === null)) {
      cachedPayload = null;
      feed.clear();
      feed.showState('empty', spec.absent ?? 'no snapshot yet — run: obi web snapshot (cron)');
      ctx.setBadge(null);
      return;
    }
    const stamps = Object.values(merged)
      .filter((entry) => entry?.generated_at)
      .map((entry) => entry.generated_at);
    ageIso = stamps.length ? stamps.sort()[0] : null; // ISO-8601 sorts chronologically
    cachedPayload = merged;
    render();
  }

  async function refresh() {
    if (spec.source.kind === 'snapshot-set') return refreshSet();
    const url = spec.query ? spec.query(filterValues) : spec.source.url;
    let body = null;
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (response.status === 404 && spec.source.kind === 'snapshot') {
        feed.clear();
        feed.showState('empty', spec.absent ?? 'no snapshot yet — run: obi web snapshot (cron)');
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      body = await response.json();
    } catch (err) {
      feed.showState('degraded', `refresh failing (${err.message}) — retrying`);
      return; // last-good rows stay on the board
    }
    if (spec.source.kind === 'snapshot') {
      ageIso = body.generated_at ?? null;
      cachedPayload = body.data;
    } else {
      if (body.store === 'absent') {
        feed.clear();
        feed.showState('empty', body.hint ?? 'no data yet');
        cachedPayload = null;
        ctx.setBadge(null);
        return;
      }
      cachedPayload = body;
      ageIso = spec.age ? spec.age(body) : null;
    }
    render();
  }

  const poller = createPoller(refresh, spec.pollMs ?? DEFAULT_POLL_MS);
  const ageTimer = setInterval(updateBadge, AGE_MS);

  const fields = [...(spec.filters ?? [])];
  if (spec.source.kind === 'snapshot' || spec.source.kind === 'snapshot-set') {
    fields.push({ key: 'text', kind: 'text', label: 'filter', placeholder: 'substring' });
  }
  if (fields.length) {
    createFilterRow(filtersHost, fields, (values) => {
      filterValues = values;
      if (spec.query) poller.poke(); // query-side filters refetch…
      else render(); // …client-side ones re-render the cached payload
    });
  }

  return {
    unmount() {
      poller.stop();
      clearInterval(ageTimer);
      feed.destroy();
    },
  };
}

// 076: createDetailPanel was retired here — its one caller (a modal report) now uses the shared
// panel language, kit.createPanel(host, ctx, { modal: true }). See specs/076-widget-language.
