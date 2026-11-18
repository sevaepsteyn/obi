// LINKS board (feature 043) — the backhaul view: every device-to-device wireless connection,
// UISP wireless links (034 snapshot) and Siklu paired links (020 pairing, in the siklu
// snapshot) merged into one table. Devices live on the UISP/SIK boards; connections live here.

import { linkKey } from '../bands.js';
import { createBoard, labeledFields } from '../boards.js';
import { severityColor } from '../kit.js';

function uispRows(data) {
  return (data.links ?? []).map((link) => {
    // 058: the endpoint-pair key opens this link's detail window (converges with a MAP click).
    const ends = (link.ends ?? []).map((end) => end?.name).filter(Boolean);
    return {
      vendor: 'uisp',
      link: link.label,
      state: link.state,
      signal: link.signal_dbm ?? '',
      ghz: link.frequency_mhz != null ? (link.frequency_mhz / 1000).toFixed(1) : '',
      km: link.distance_m != null ? (link.distance_m / 1000).toFixed(2) : '',
      _key: ends.length === 2 ? linkKey(ends[0], ends[1]) : null,
    };
  });
}

function sikluRows(data) {
  return (data.links ?? []).map((link) => {
    const ends = [link.end_a, link.end_b].filter(Boolean);
    const rssi = ends.map((end) => end.rf?.rssi_dbm).filter((v) => v != null);
    const mhz = ends.map((end) => end.rf?.tx_frequency_mhz).find((v) => v != null);
    return {
      vendor: 'siklu',
      // Full end identities when both are known — the canonical short name otherwise.
      link: ends.length === 2 ? ends.map((end) => end.identity).join(' ↔ ') : link.name,
      state: link.state,
      signal: rssi.length ? Math.min(...rssi) : '', // worst end — a link is its weakest side
      ghz: mhz != null ? (mhz / 1000).toFixed(1) : '',
      km: '', // the source has no distance concept — blank is honest
      _key: ends.length === 2 ? linkKey(ends[0].identity, ends[1].identity) : null,
    };
  });
}

export async function mount(host, ctx) {
  return createBoard(host, ctx, {
    source: {
      kind: 'snapshot-set',
      urls: { uisp: '/snapshots/uisp-links.json', siklu: '/snapshots/siklu.json' },
    },
    // 058: a row click opens that link's detail window (the same window a MAP link tap opens).
    // Rows whose endpoints didn't resolve carry no key and simply don't open.
    onRowClick: (row) => { if (row._key) ctx.openLink?.(row._key, row.link); },
    columns: [
      { key: 'vendor', label: 'vendor', width: '6ch' },
      { key: 'link', label: 'link' },
      { key: 'state', label: 'state', width: '11ch', color: severityColor },
      { key: 'signal', label: 'dbm', width: '6ch', align: 'right' },
      { key: 'ghz', label: 'ghz', width: '6ch', align: 'right' },
      { key: 'km', label: 'km', width: '7ch', align: 'right' },
    ],
    rows: (payload) => [
      ...(payload.uisp ? uispRows(payload.uisp.data) : []),
      ...(payload.siklu ? sikluRows(payload.siklu.data) : []),
    ],
    counts: (payload) => {
      let total = 0;
      let up = 0;
      let down = 0;
      const gaps = [];
      if (payload.uisp) {
        const counts = payload.uisp.data.counts ?? {};
        total += counts.total ?? 0;
        up += counts.active ?? 0;
        down += counts.down ?? 0;
      } else {
        gaps.push('uisp: no snapshot');
      }
      if (payload.siklu) {
        const links = payload.siklu.data.links ?? [];
        total += links.length;
        up += links.filter((link) => link.state === 'up').length;
        down += links.filter((link) => link.state === 'down').length;
      } else {
        gaps.push('siklu: no snapshot');
      }
      return [`${up} up · ${down} down / ${total}`, ...gaps].join(' · ');
    },
    // 056: the identity column here is the LINK, not the leading `vendor` cell — name it directly.
    contextFor: ({ row, columns }) => ({
      subject: `link ${row.link}`,
      elementText: labeledFields(row, columns),
    }),
  });
}
