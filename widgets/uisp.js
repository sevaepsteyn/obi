// UISP board (feature 043) — the UISP device fleet from the uisp snapshot (006 data).
// Link RF health moved to the LINKS board; this is the devices-themselves view.

import { createBoard, formatMemPercent, formatUptime, labeledFields } from '../boards.js';
import { severityColor } from '../kit.js';

// 063: mem% now lives in boards.js (formatMemPercent) — one home for the ROS + UISP boards.

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: { kind: 'snapshot', url: '/snapshots/uisp.json' },
    columns: [
      { key: 'identity', label: 'device', width: '26ch' },
      { key: 'reach', label: 'state', width: '11ch', color: severityColor },
      { key: 'model', label: 'model', width: '14ch' },
      { key: 'uptime', label: 'uptime', width: '9ch', align: 'right' },
      { key: 'cpu', label: 'cpu %', width: '6ch', align: 'right' },
      { key: 'mem', label: 'mem %', width: '6ch', align: 'right' },
    ],
    rows: (data) => data.devices.map((device) => ({
      identity: device.identity ?? device.address,
      reach: device.reachability,
      model: device.model ?? '',
      uptime: formatUptime(device.uptime_seconds),
      cpu: device.cpu_load_percent ?? '',
      mem: formatMemPercent(device.free_memory_bytes, device.total_memory_bytes),
    })),
    counts: (data) => `${data.counts.reachable ?? 0}/${data.counts.total} devices`,
    contextFor: ({ row, columns }) => ({
      subject: `device ${row.identity}`,
      elementText: labeledFields(row, columns),
    }),
  });
}
