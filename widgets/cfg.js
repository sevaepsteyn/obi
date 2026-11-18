// CFG widget (feature 041, split by 051) — the fleet config-backup COVERAGE board.
//
// Clicking a device row opens that device's config-history window (widgets/conf.js) via
// ctx.openConfig; the per-device history / diff / full-config viewer lives there now, so this
// widget is coverage-only — no device dropdown. The coverage read is a local-git read,
// intent-audited server-side (the 026 rule); the table inherits sort/resize/copy/CSV from the kit.

import { labeledFields } from '../boards.js';
import { createFeed, createPoller, fetchJson } from '../kit.js';

const POLL_MS = 5 * 60 * 1000;

// Coverage states are their own vocabulary — mapped locally onto the kit's token classes.
// 085: 'stale' = CAPTURE health (obi has not successfully read the device within the server's
// stale_after window) — a red-grade problem, not "config unchanged for a while" (that is
// healthy and shows only in the informational last-change column).
function coverageColor(state) {
  switch (state) {
    case 'fresh': return 'kit-c-ok';
    case 'stale': return 'kit-c-down';
    case 'no backup': return 'kit-c-down';
    case 'pending': return 'kit-c-warn'; // no capture record yet (fills on the next ingest tick)
    default: return 'kit-c-muted'; // orphaned
  }
}

export async function mount(host, ctx) {
  host.classList.add('cfg');
  const content = document.createElement('div');
  content.className = 'cfg-content';
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.append(content);

  let feed = null;

  function clearContent() {
    feed?.destroy();
    feed = null;
    content.replaceChildren();
  }

  function showMessage(text) {
    clearContent();
    const box = document.createElement('div');
    box.className = 'kit-message'; // 076: shared muted message box (cfg-message retired)
    box.textContent = text;
    content.appendChild(box);
  }

  function renderCoverage(payload) {
    clearContent();
    feed = createFeed(content, {
      columns: [
        { key: 'identity', label: 'device', width: '22ch' },
        { key: 'state', label: 'state', width: '11ch', color: coverageColor },
        { key: 'versions', label: 'vers', width: '5ch', align: 'right' },
        { key: 'captured', label: 'captured', width: '17ch' },
        { key: 'changed', label: 'last change', width: '17ch' },
        { key: 'note', label: 'note' },
      ],
      tail: false,
      persist: 'cfg',
      // 051: a row click opens that device's config-history window (a no-op on the full-screen
      // page, which has no workspace to open windows into).
      onRowClick: (row) => ctx.openConfig?.(row.identity),
      // 056: right-click a coverage row → ask obi about that device's backup state.
      contextFor: ({ row, columns }) => ({
        subject: `config ${row.identity}`,
        elementText: labeledFields(row, columns),
      }),
    });
    feed.replace(payload.rows.map((row) => ({
      identity: row.identity,
      state: !row.has_backup ? 'no backup'
        : !row.in_roster ? 'orphaned'
        : row.stale ? 'stale'
        : !row.last_captured_at ? 'pending'
        : 'fresh',
      versions: row.versions,
      captured: row.last_captured_at ? row.last_captured_at.slice(0, 16).replace('T', ' ') : '',
      changed: row.last_changed_at ? row.last_changed_at.slice(0, 16).replace('T', ' ') : '',
      note: row.note ?? '',
    })));
    const totals = payload.totals;
    ctx.setBadge(`${totals.backed_up}/${totals.devices} backed up · ${totals.stale} capture-stale`);
  }

  async function refresh() {
    let payload = null;
    try {
      payload = await fetchJson('/api/configs.json');
    } catch (err) {
      showMessage(`refresh failing (${err.message}) — retrying`);
      return;
    }
    if (payload.store === 'absent') {
      showMessage(payload.hint);
      ctx.setBadge(null);
      return;
    }
    renderCoverage(payload);
  }

  const poller = createPoller(refresh, POLL_MS);

  return {
    unmount() {
      poller.stop();
      feed?.destroy();
    },
  };
}
