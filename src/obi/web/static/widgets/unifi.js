// UNIFI board (feature 040) — UniFi devices from the unifi snapshot (011 data).

import { createBoard, formatUptime, labeledFields } from '../boards.js';
import { severityColor } from '../kit.js';

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: { kind: 'snapshot', url: '/snapshots/unifi.json' },
    columns: [
      { key: 'identity', label: 'device', width: '24ch' },
      { key: 'address', label: 'address', width: '15ch' },
      { key: 'reach', label: 'state', width: '12ch', color: severityColor },
      { key: 'version', label: 'version', width: '12ch' },
      { key: 'uptime', label: 'uptime', width: '9ch', align: 'right' },
    ],
    rows: (data) => data.devices.map((device) => ({
      identity: device.identity ?? device.address,
      address: device.address ?? '',
      reach: device.reachability,
      version: device.version ?? '',
      uptime: formatUptime(device.uptime_seconds),
    })),
    counts: (data) => `${data.counts.reachable}/${data.counts.total} reachable`,
    contextFor: ({ row, columns }) => ({
      subject: `device ${row.identity}`,
      elementText: labeledFields(row, columns),
    }),
  });
}
