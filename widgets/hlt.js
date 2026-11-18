// HEALTH board (feature 096, id `hlt`) — fleet hardware health from /api/health.json (local metrics
// store; thresholds shared with the watch pass, so this board and the Slack alerts can
// never disagree). Rows arrive pre-sorted worst-first from the server; a row click drills
// into that router's live detail window (HEALTH tab) for RouterOS devices.

import { createBoard, labeledFields } from '../boards.js';
import { severityColor } from '../kit.js';

const dash = '—'; // absence stays absence — never a zero

function fmt(value, suffix) {
  return value === null || value === undefined ? dash : `${value}${suffix}`;
}

function fmtAgeSeconds(seconds) {
  if (seconds === null || seconds === undefined) return dash;
  if (seconds < 90) return `${seconds}s`;
  if (seconds < 5400) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: { kind: 'api', url: '/api/health.json' },
    columns: [
      { key: 'device', label: 'device', width: '18ch' },
      { key: 'kind', label: 'kind', width: '8ch' },
      { key: 'worst', label: 'state', width: '7ch', color: severityColor },
      { key: 'temp', label: 'temp', width: '8ch', align: 'right' },
      { key: 'cpu', label: 'cpu%', width: '6ch', align: 'right' },
      { key: 'mem', label: 'mem%', width: '6ch', align: 'right' },
      { key: 'rx', label: 'rx dBm', width: '8ch', align: 'right' },
      { key: 'disk', label: 'disk%', width: '6ch', align: 'right' },
      {
        key: 'breaches',
        label: 'breaches',
        width: '24ch',
        color: (value) => (value === dash ? '' : 'kit-c-down'),
        title: '_breach_detail',
      },
      { key: 'age', label: 'age', width: '6ch', align: 'right' },
    ],
    rows: (data) => data.devices.map((device) => ({
      device: device.device,
      kind: device.kind,
      worst: device.worst,
      temp: fmt(device.summary.temperature_c, '°C'),
      cpu: fmt(device.summary.cpu_percent, ''),
      mem: fmt(device.summary.memory_percent, ''),
      rx: fmt(device.summary.rx_power_min_dbm, ''),
      disk: fmt(device.summary.disk_max_percent, ''),
      breaches: device.breaches.length
        ? device.breaches.map((b) => b.label ?? b.metric).join(', ')
        : dash,
      age: fmtAgeSeconds(device.age_seconds) + (device.stale ? ' stale' : ''),
      _breach_detail: device.breaches
        .map((b) => `${b.label ?? b.metric} = ${b.value} ${b.unit} (limit ${b.limit})`)
        .join('\n'),
      _address: device.address,
      _kind: device.kind,
    })),
    counts: (data) => {
      const breaching = data.devices.filter((d) => d.breaches.length).length;
      const stale = data.devices.filter((d) => d.stale).length;
      return `${data.devices.length} devices · ${breaching} breaching · ${stale} stale`;
    },
    age: (data) => data.generated_at ?? null,
    contextFor: ({ row, columns }) => ({
      subject: `device health ${row.device}`,
      elementText: labeledFields(row, columns),
    }),
    // Drill into the router window (its HEALTH tab has the trend); collector VMs have no
    // router window — their row click is a no-op, not an error.
    onRowClick: (row) => {
      if (row._kind === 'routeros' && row._address) ctx.openRouter?.(row._address, row.device);
    },
  });
}
