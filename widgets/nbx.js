// NBX board (feature 040) — NetBox↔roster presence drift from the netbox-drift snapshot (017).

import { createBoard, labeledFields } from '../boards.js';

// Drift classes are their own vocabulary — mapped locally onto the kit's token classes.
function driftColor(value) {
  switch (value) {
    case 'matched':
      return 'kit-c-ok';
    case 'undocumented':
      return 'kit-c-warn';
    case 'retire_candidate':
      return 'kit-c-down';
    default:
      return 'kit-c-muted'; // outside_scope / un_correlatable
  }
}

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: { kind: 'snapshot', url: '/snapshots/netbox-drift.json' },
    columns: [
      { key: 'klass', label: 'drift', width: '17ch', color: driftColor },
      { key: 'name', label: 'device', width: '22ch' },
      { key: 'site', label: 'site', width: '14ch' },
      { key: 'model', label: 'model', width: '16ch' },
      { key: 'seen', label: 'last seen', width: '11ch', align: 'right' },
    ],
    rows: (data) => data.drift.map((entry) => ({
      klass: entry.drift_class,
      name: entry.name,
      site: entry.site ?? '',
      model: entry.model ?? '',
      seen: entry.last_seen ? entry.last_seen.slice(0, 10) : '',
    })),
    counts: (data) =>
      `${data.counts.matched} matched · ${data.counts.undocumented} undocumented · ` +
      `${data.counts.retire_candidate} retire?`,
    contextFor: ({ row, columns }) => ({
      subject: `drift: ${row.name}`,
      elementText: labeledFields(row, columns),
    }),
  });
}
