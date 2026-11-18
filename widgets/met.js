// MET widget (feature 041) — the 021 metrics history as time-series charts.
//
// Coverage mode of /api/metrics.json populates the device picker (only devices the store
// actually has readings for); series mode is the CLI history payload verbatim — reset-safe
// rates computed by the store at query time, gap buckets carried through so the chart can
// break its lines honestly. The catalog's first render:'canvas' data widget.
//
// Display conventions live HERE, not in the store: traffic is shown in bits/s (the ops-graph
// convention — MRTG, router pages), memory in bytes; the store serves exact bytes/kB. Series
// are drawn busiest-first so a many-port router's active links are never hidden behind the
// chart's series cap, and the interface picker is a dropdown fed by the readings themselves.

import { createChart } from '../chart.js';
import { createFilterRow, createPoller, fetchJson } from '../kit.js';
import { formatAge } from '../boards.js'; // 076: dedup the badge age wording to boards.formatAge

const POLL_MS = 5 * 60 * 1000; // the collector cron's cadence
const METRICS = [
  ['ifHCInOctets', 'traffic in'],
  ['ifHCOutOctets', 'traffic out'],
  ['ifInErrors', 'errors in'],
  ['ifOutErrors', 'errors out'],
  ['ifInDiscards', 'discards in'],
  ['ifOutDiscards', 'discards out'],
  ['cpuLoad', 'cpu load'],
  ['memUsedKb', 'memory used'],
  ['cpuTemperature', 'cpu temp'],
];
// Device-scalar metrics have no interface dimension — the iface picker hides for them.
const SCALAR_METRICS = new Set(['cpuLoad', 'memUsedKb', 'cpuTemperature']);
// Presentation transforms applied to the store's exact values (factor on avg/min/max).
const DISPLAY = {
  ifHCInOctets: { factor: 8, unit: 'bits/s' },
  ifHCOutOctets: { factor: 8, unit: 'bits/s' },
  memUsedKb: { factor: 1024, unit: 'B' },
};
const ALL_IFACES = ['', 'all interfaces'];

export async function mount(host, ctx) {
  const filtersHost = document.createElement('div');
  const stateEl = document.createElement('div');
  stateEl.className = 'kit-message'; // 076: shared muted bordered box (was met-state)
  const chartHost = document.createElement('div');
  chartHost.className = 'met-chart';
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.append(filtersHost, stateEl, chartHost);

  function showState(message) {
    stateEl.textContent = message ?? '';
    stateEl.hidden = !message;
  }

  // Coverage first: the device picker offers what the store has (spec US1 AS-1). Say so while
  // it lands — the filter row is built from this response, so until it returns the window has
  // nothing in it at all, and a blank box reads as a broken widget rather than a working one.
  showState('loading devices…');
  let coverage = null;
  try {
    coverage = await fetchJson('/api/metrics.json');
  } catch (err) {
    showState(`server unreachable (${err.message}) — reopen the widget to retry`);
    return { unmount() {} };
  }
  if (coverage.store === 'absent') {
    showState(coverage.hint);
    return { unmount() {} };
  }
  const devices = coverage.devices.map((d) => d.device);
  if (!devices.length) {
    showState('the metrics store has no readings yet — wait for the collector cron');
    return { unmount() {} };
  }

  // Devices are in; the first series is the next (short) wait — same wording as a filter change.
  showState('querying…');
  let selection = { target: devices[0], metric: 'ifHCInOctets', interface: '', window: '24h' };
  const ifacesByDevice = new Map(); // device -> [ifname]: learned from unfiltered responses
  const chart = createChart(chartHost);
  let newestBucket = null;

  function updateBadge() {
    if (!newestBucket) {
      ctx.setBadge(null);
      return;
    }
    // 076: age wording dedup'd to boards.formatAge ('<1m'/'Nm'/'Nh'); met keeps its 'last ' prefix.
    ctx.setBadge(`last ${formatAge(newestBucket)}`);
  }

  function ifaceOptions(names) {
    return [ALL_IFACES, ...names.map((n) => [n, n])];
  }

  async function refresh() {
    const scalar = SCALAR_METRICS.has(selection.metric);
    const params = new URLSearchParams({
      target: selection.target,
      metric: selection.metric,
      window: selection.window,
    });
    if (selection.interface && !scalar) params.set('interface', selection.interface);
    let payload = null;
    try {
      payload = await fetchJson(`/api/metrics.json?${params}`);
    } catch (err) {
      showState(`refresh failing (${err.message}) — retrying`);
      return; // the last-good chart stays up
    }
    if (payload.store === 'absent') {
      showState(payload.hint);
      return;
    }

    // An unfiltered per-interface response names every interface the store has readings
    // for — that IS the iface dropdown's option list (kept per device for quick switching).
    if (!scalar && !selection.interface) {
      const names = payload.series.map((s) => s.ifname).filter(Boolean)
        .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
      ifacesByDevice.set(selection.target, names);
      filters.setOptions('interface', ifaceOptions(names), selection.interface);
    }

    const display = DISPLAY[selection.metric];
    const factor = display?.factor ?? 1;
    const scale = (v) => (v == null ? v : v * factor);
    const series = payload.series.map((s) => ({
      label: s.ifname ?? selection.target, // device-scalar series wear the device name
      buckets: factor === 1 ? s.buckets : s.buckets.map((b) => ({
        ...b, avg: scale(b.avg), min: scale(b.min), max: scale(b.max),
      })),
      activity: Math.max(0, ...s.buckets.map((b) => (b.n > 0 && b.avg != null ? b.avg : 0))),
    }));
    // Busiest first: the chart caps how many series it draws — the active links must be
    // the ones that survive the cap, never an arbitrary first-by-ifindex slice.
    series.sort((a, b) => b.activity - a.activity);
    if (!series.length) {
      showState('no readings for this selection in the window');
      chart.render([], {});
      newestBucket = null;
      updateBadge();
      return;
    }
    showState(null);
    chart.render(series, { unit: display?.unit ?? payload.series[0].unit });
    const filled = series[0].buckets.filter((b) => b.n > 0);
    newestBucket = filled.length ? filled[filled.length - 1].start : null;
    updateBadge();
  }

  const filters = createFilterRow(filtersHost, [
    { key: 'target', kind: 'select', label: 'device',
      options: devices.map((d) => [d, d]), value: selection.target },
    { key: 'metric', kind: 'select', label: 'metric', options: METRICS,
      value: selection.metric },
    { key: 'interface', kind: 'select', label: 'iface', options: [ALL_IFACES], value: '' },
    { key: 'window', kind: 'select', label: 'window',
      options: [['1h', '1h'], ['6h', '6h'], ['24h', '24h'], ['7d', '7d']], value: '24h' },
  ], (values) => {
    if (values.target !== selection.target) {
      // A new device has its own ports: reset the iface filter to its (cached) list.
      const known = ifacesByDevice.get(values.target) ?? [];
      filters.setOptions('interface', ifaceOptions(known), '');
      values.interface = '';
    }
    filters.setHidden('interface', SCALAR_METRICS.has(values.metric));
    selection = values;
    showState('querying…');
    poller.poke();
  });
  filters.setHidden('interface', SCALAR_METRICS.has(selection.metric));

  const poller = createPoller(refresh, POLL_MS); // after the filter row: refresh touches it

  const ageTimer = setInterval(updateBadge, 30 * 1000);

  return {
    unmount() {
      poller.stop();
      clearInterval(ageTimer);
      chart.destroy();
    },
  };
}
