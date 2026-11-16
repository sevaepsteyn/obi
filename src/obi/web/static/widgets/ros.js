// ROS board (feature 040, renamed + widened by 047) — the RouterOS fleet from the status
// snapshot (obi status data). A row click drills down into that router's live detail window.

import { createBoard, formatMemPercent, formatUptime, labeledFields } from '../boards.js';
import { severityColor } from '../kit.js';

// 063: mem% now lives in boards.js (formatMemPercent) — one home for the ROS + UISP boards.

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: { kind: 'snapshot', url: '/snapshots/status.json' },
    columns: [
      { key: 'identity', label: 'device', width: '18ch' },
      { key: 'address', label: 'address', width: '14ch' },
      { key: 'reach', label: 'state', width: '11ch', color: severityColor },
      { key: 'model', label: 'model', width: '15ch' },
      { key: 'version', label: 'version', width: '10ch' },
      { key: 'uptime', label: 'uptime', width: '9ch', align: 'right' },
      { key: 'cpu', label: 'cpu%', width: '6ch', align: 'right' },
      { key: 'mem', label: 'mem%', width: '6ch', align: 'right' },
    ],
    rows: (data) => data.devices.map((device) => ({
      identity: device.identity ?? device.address,
      address: device.address,
      reach: device.reachability ?? device.discovery_state,
      model: device.model ?? '',
      version: device.version ?? '',
      uptime: formatUptime(device.uptime_seconds),
      cpu: device.cpu_load_percent ?? '',
      mem: formatMemPercent(device.free_memory_bytes, device.total_memory_bytes),
    })),
    counts: (data) => `${data.counts.reachable}/${data.counts.total} reachable`,
    // 056: right-click "ask obi about this" → the router named, its visible fields attached.
    contextFor: ({ row, columns }) => ({
      subject: `router ${row.identity}`,
      elementText: labeledFields(row, columns),
    }),
    // 047: click a router → open its live detail window (a no-op on the full-screen page,
    // which has no workspace to open windows into).
    onRowClick: (row) => ctx.openRouter?.(row.address, row.identity),
  });
}
