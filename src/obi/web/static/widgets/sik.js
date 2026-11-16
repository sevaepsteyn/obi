// SIK board (feature 040) — Siklu EtherHaul radios from the siklu snapshot (020 data).

import { createBoard, formatUptime, labeledFields } from '../boards.js';
import { severityColor } from '../kit.js';

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: { kind: 'snapshot', url: '/snapshots/siklu.json' },
    columns: [
      { key: 'identity', label: 'radio', width: '24ch' },
      { key: 'reach', label: 'state', width: '12ch', color: severityColor },
      { key: 'rssi', label: 'rssi', width: '6ch', align: 'right' },
      { key: 'cinr', label: 'cinr', width: '6ch', align: 'right' },
      { key: 'tx', label: 'tx dbm', width: '7ch', align: 'right' },
      { key: 'uptime', label: 'uptime', width: '9ch', align: 'right' },
    ],
    rows: (data) => data.radios.map((radio) => ({
      identity: radio.identity ?? radio.address,
      reach: radio.reachability,
      rssi: radio.rf?.rssi_dbm ?? '',
      cinr: radio.rf?.cinr_db ?? '',
      tx: radio.rf?.tx_power_dbm ?? '',
      uptime: formatUptime(radio.uptime_seconds),
    })),
    // Link pairing lives on the LINKS board (043) — this badge is radios-only.
    counts: (data) => `${data.counts.reachable}/${data.counts.total} radios`,
    contextFor: ({ row, columns }) => ({
      subject: `radio ${row.identity}`,
      elementText: labeledFields(row, columns),
    }),
  });
}
