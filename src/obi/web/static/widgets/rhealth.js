// Router-window HEALTH tab (feature 096) — one device's hardware-health readings with a
// 24 h trend, from /api/health.json?device= (local metrics store only; thresholds shared
// with the watch pass). Lazy-mounted by router.js; polls only while its tab is active
// (the phy.js discipline).

import { createFeed, createRefreshButton, createStatusLine, fetchJson } from '../kit.js';

const POLL_MS = 60_000; // the store advances on the 5-min crons; 1 min keeps age honest
const SPARK = '▁▂▃▄▅▆▇█';

function sparkline(buckets) {
  const values = buckets.map((b) => (b.n ? b.avg : null));
  const present = values.filter((v) => v !== null);
  if (present.length < 2) return '';
  const min = Math.min(...present);
  const max = Math.max(...present);
  const span = max - min || 1;
  return values
    .map((v) => (v === null ? ' ' : SPARK[Math.round(((v - min) / span) * (SPARK.length - 1))]))
    .join('');
}

export async function mount(host, ctx) {
  const identity = ctx.params?.identity ?? '';

  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';

  const toolbar = document.createElement('div');
  toolbar.className = 'kit-toolbar';
  const status = createStatusLine(toolbar);
  const feedHost = document.createElement('div');
  feedHost.style.flex = '1';
  feedHost.style.minHeight = '0';
  host.append(toolbar, feedHost);

  const feed = createFeed(feedHost, {
    columns: [
      { key: 'reading', label: 'reading', width: '22ch' },
      { key: 'instance', label: 'instance', width: '16ch' },
      {
        key: 'value',
        label: 'value',
        width: '12ch',
        align: 'right',
        color: (value, row) => (row._breached ? 'kit-c-down' : ''),
        title: '_limit',
      },
      { key: 'trend', label: 'trend (24h)', width: '24ch' },
    ],
    persist: 'rhealth',
    exportName: 'health',
  });

  let lastRead = 0;

  async function refresh() {
    if (!identity) {
      feed.showState('empty', 'device identity unknown — open this window from a board row');
      return;
    }
    let body = null;
    try {
      body = await fetchJson(`/api/health.json?device=${encodeURIComponent(identity)}`);
    } catch (err) {
      status.set(`refresh failing (${err.message}) — retrying`, 'down');
      return; // last-good rows stay
    }
    lastRead = Date.now();
    if (body.store === 'absent' || !body.readings?.length) {
      feed.clear();
      feed.showState(
        'empty',
        body.hint ?? body.note ?? 'no health samples for this device yet — run: obi metrics ingest',
      );
      status.set('no data', 'muted');
      return;
    }
    const breached = new Map(
      (body.breaches ?? []).map((b) => [`${b.metric}|${b.instance}`, b]),
    );
    const trends = new Map(
      (body.series ?? []).map((s) => [`${s.metric}|${s.ifname ?? ''}`, sparkline(s.buckets)]),
    );
    feed.replace(
      body.readings.map((r) => {
        const breach = breached.get(`${r.metric}|${r.instance}`);
        return {
          reading: r.metric,
          instance: r.label ?? r.instance ?? '—',
          value: `${r.value} ${r.unit}`.trim(),
          trend: trends.get(`${r.metric}|${r.label ?? ''}`)
            ?? trends.get(`${r.metric}|`) ?? '',
          _breached: Boolean(breach),
          _limit: breach ? `breaching: limit ${breach.limit}` : '',
        };
      }),
    );
    const breaches = body.breaches?.length ?? 0;
    const tone = breaches ? 'down' : body.stale ? 'warn' : 'ok';
    const staleText = body.stale ? ' · STALE' : '';
    status.set(
      `${body.readings.length} readings · ${breaches} breaching${staleText}`,
      tone,
    );
  }

  toolbar.append(createRefreshButton(refresh));

  // Poll only while this tab is active (the phy.js D8 discipline) — kit's createPoller is
  // one-shot-stoppable, so the tab rolls its own restartable interval.
  let timer = null;
  let inFlight = false;
  async function tick() {
    if (inFlight) return;
    inFlight = true;
    try {
      await refresh();
    } finally {
      inFlight = false;
    }
  }
  function stopPolling() {
    if (timer !== null) clearInterval(timer);
    timer = null;
  }

  return {
    activate() {
      if (timer === null) timer = setInterval(tick, POLL_MS);
      if (Date.now() - lastRead > POLL_MS) void tick();
    },
    deactivate() {
      stopPolling();
    },
    unmount() {
      stopPolling();
      feed.destroy();
    },
  };
}
