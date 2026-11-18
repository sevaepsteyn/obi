// LOGS widget (feature 038) — the fleet's device-log tail as a workspace citizen.
//
// A pure renderer over GET /api/logs.json (contracts/logs-api.md): the store, the filters,
// and the id-cursor tail all live server-side in the 013 core; this module wires the kit's
// feed + filter row + poller to that route. Timestamps arrive as the STORE's localized ISO
// strings and are displayed by slicing — the browser does zero timezone math (the
// thin-adapter rule, same as the map doing zero geodesy).
//
// Freshness is this widget's own concern (037 field lesson): the badge shows the newest
// entry's age (store freshness / ingest health); a failed poll shows the kit's degraded
// banner and tailing resumes from the held cursor on the next success — ids are monotonic,
// so recovery can neither duplicate nor skip rows.

import { createFeed, createFilterRow, createPoller, fetchJson, severityColor } from '../kit.js';
import { formatAge } from '../boards.js'; // 076: dedup the badge age wording to boards.formatAge

const TAIL_MS = 20 * 1000; // comfortably inside SC-001's 45 s bound; a quiet delta is ~100 B
const AGE_MS = 30 * 1000;
const DISPLAY_CAP = 500;

export async function mount(host, ctx) {
  const filtersHost = document.createElement('div');
  const feedHost = document.createElement('div');
  feedHost.style.flex = '1';
  feedHost.style.minHeight = '0';
  host.append(filtersHost, feedHost);

  let filters = { source: '', device: '', severity: '', q: '', window: '24h' };
  let cursor = null; // null => next fetch is an initial page (also after filter changes)
  let newestTs = null;
  let longWindow = false; // 7d rows show the date — a time alone is ambiguous across days

  const feed = createFeed(feedHost, {
    columns: [
      { key: 'time', label: 'time', width: '11ch', title: 'fullTs', sort: 'fullTs' },
      { key: 'host', label: 'device', width: '16ch' },
      { key: 'severity', label: 'sev', width: '9ch', color: severityColor },
      { key: 'message', label: 'message' },
    ],
    cap: DISPLAY_CAP,
    persist: 'logs',
    // 056: right-click a log line → ask obi about it. The context is that line plus up to 3
    // neighbors on each side (bounded — never the whole log), the clicked line marked with ».
    contextFor: ({ row, rows }) => {
      const index = rows.indexOf(row);
      const near = rows.slice(Math.max(0, index - 3), index + 4);
      const lines = near.map((entry) => {
        const mark = entry === row ? '» ' : '  ';
        return `${mark}${entry.fullTs || entry.time} ${entry.host} ${entry.severity} ${entry.message}`;
      });
      return { subject: `log line — ${row.host}`, elementText: lines.join('\n') };
    },
  });
  feed.showState('loading', 'loading the log store…');

  function toRow(entry) {
    const ts = entry.timestamp || '';
    return {
      time: longWindow ? `${ts.slice(5, 10)} ${ts.slice(11, 19)}` : ts.slice(11, 19),
      fullTs: ts,
      host: entry.host,
      severity: entry.severity ?? '—',
      message: entry.message,
    };
  }

  function updateBadge() {
    // 076: age wording dedup'd to boards.formatAge ('<1m'/'Nm'/'Nh'); logs keeps its 'last ' prefix.
    const age = newestTs ? `last ${formatAge(newestTs)}` : 'no entries';
    ctx.setBadge(feed.follow ? age : `${age} · tail paused`);
  }

  function queryString(extra) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value && !(key === 'window' && extra.after !== undefined)) params.set(key, value);
    }
    if (extra.after !== undefined) params.set('after', String(extra.after));
    return `/api/logs.json?${params.toString()}`;
  }

  async function poll() {
    let payload = null;
    try {
      payload = await fetchJson(queryString(cursor === null ? {} : { after: cursor }));
    } catch (err) {
      feed.showState('degraded', `server unreachable (${err.message}) — retrying`);
      return; // cursor unchanged: the next success resumes exactly where we left off
    }
    if (payload.store === 'absent') {
      feed.clear();
      feed.showState('empty', payload.hint || 'no log store yet');
      newestTs = null;
      updateBadge();
      return;
    }
    const entries = payload.entries;
    if (payload.mode === 'initial') {
      feed.replace(entries.map(toRow));
      if (!entries.length) feed.showState('empty', 'no entries match the current filters');
    } else if (entries.length) {
      feed.append(entries.map(toRow));
    }
    cursor = payload.cursor;
    if (entries.length) newestTs = entries[entries.length - 1].timestamp;
    updateBadge();
  }

  const poller = createPoller(poll, TAIL_MS);

  createFilterRow(filtersHost, [
    { key: 'source', kind: 'select', label: 'source',
      options: [['', 'all'], ['syslog', 'syslog'], ['unifi', 'unifi'], ['uisp', 'uisp']] },
    { key: 'device', kind: 'text', label: 'device', placeholder: 'substring' },
    { key: 'severity', kind: 'select', label: 'sev ≥',
      options: [['', 'all'], ['debug', 'debug'], ['info', 'info'], ['warning', 'warning'],
                ['error', 'error'], ['critical', 'critical']] },
    { key: 'q', kind: 'text', label: 'text', placeholder: 'substring' },
    { key: 'window', kind: 'select', label: 'window',
      options: [['15m', '15m'], ['1h', '1h'], ['6h', '6h'], ['24h', '24h'], ['7d', '7d']],
      value: '24h' },
  ], (values) => {
    filters = values;
    longWindow = values.window === '7d';
    cursor = null; // filter change ⇒ fresh initial page + re-based cursor
    feed.showState('loading', 'querying…');
    poller.poke();
  });

  feed.onFollowChange(updateBadge);
  const ageTimer = setInterval(updateBadge, AGE_MS);

  return {
    unmount() {
      poller.stop();
      clearInterval(ageTimer);
      feed.destroy();
    },
  };
}
