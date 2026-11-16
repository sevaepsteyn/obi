// DASH widget (feature 095) — the green/yellow/red status board.
//
// Renders the minute cycle's persisted board (/api/dash.json — a local-file read server-side;
// this widget triggers no device I/O) as one tile per signal, worst-of colored, with the
// non-green items surfaced first. Polls at 60 s (the AUDIT cadence precedent) and re-renders
// every 30 s so ages keep moving between polls. The honesty rule is client-side too: a board
// older than STALE_MS renders stale EVERYWHERE, whatever states it persisted — a dead cron
// must never leave a green wall (FR-009/SC-004). Fetch failure keeps the last-good board
// rendered with a degraded badge (the boards.js convention); `?w=dash` is the wall display.

import { createPoller, fetchJson } from '../kit.js';

const POLL_MS = 60 * 1000;
export const STALE_MS = 180 * 1000; // 3× the cycle cadence — matches the server-side tile rule

const STATE_RANK = { green: 0, stale: 1, yellow: 2, red: 3 };

// Pure view logic, exported for the headless harness (the 046 pattern: logic testable in node
// without a browser; the DOM below is only arrangement).

export function generatedMs(board) {
  // µs field preferred; the ISO twin is the fallback (it is also what the shots harness can
  // re-anchor — `_fresh` rewrites ISO strings only). Unparseable ⇒ 0 ⇒ maximally stale: honest.
  if (board.generated_at_us != null) return board.generated_at_us / 1000;
  const parsed = Date.parse(board.generated_at ?? '');
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function isStale(board, nowMs) {
  return nowMs - generatedMs(board) > STALE_MS;
}

export function sortItems(items) {
  // Non-green first (worst first), then stable by label — the attention ordering.
  return [...items].sort(
    (a, b) =>
      (STATE_RANK[b.state] ?? 0) - (STATE_RANK[a.state] ?? 0) || a.label.localeCompare(b.label),
  );
}

export function badgeText(board, nowMs, { degraded = false } = {}) {
  const stale = isStale(board, nowMs);
  const ageMin = Math.max(0, Math.round((nowMs - generatedMs(board)) / 60000));
  const age = ageMin < 1 ? '<1m' : ageMin < 120 ? `${ageMin}m` : `${Math.round(ageMin / 60)}h`;
  const state = stale ? `stale (${age} old)` : `${board.state} · ${age}`;
  return degraded ? `${state} · refresh failing` : state;
}

export function tileState(tile, stale) {
  if (stale) return 'stale';
  // 135: a whole signal in maintenance (mesh/lighting) reads as maintenance, not its color.
  return tile.maintenance ? 'maint' : tile.state;
}

// 135: maintenance items are deliberate — neither problems nor ok. Problems keep the
// worst-first order; maintenance items get a dim group of their own.
export function partitionItems(items) {
  const problems = [];
  const maint = [];
  const ok = [];
  for (const item of sortItems(items)) {
    if (item.maintenance) maint.push(item);
    else if (item.state === 'green') ok.push(item);
    else problems.push(item);
  }
  return { problems, maint, ok };
}

// The standing banner text — the browser twin of MaintenanceSummary.line() (Python).
export function maintenanceLine(summary) {
  if (!summary) return null;
  const parts = [];
  if (summary.sites?.length) parts.push(`sites ${summary.sites.join(', ')}`);
  if (summary.devices?.length) parts.push(`devices ${summary.devices.join(', ')}`);
  if (summary.uisp_sites?.length) parts.push(`UISP sites ${summary.uisp_sites.join(', ')}`);
  if (summary.signals?.length) parts.push(`signals ${summary.signals.join(', ')}`);
  if (summary.uisp_flagged) {
    const noun = summary.uisp_flagged === 1 ? 'radio' : 'radios';
    parts.push(`${summary.uisp_flagged} UISP-flagged ${noun}${summary.uisp_cached ? ' (from cache)' : ''}`);
  }
  let text = parts.join('; ') || 'nothing';
  if (summary.reason) text += ` — ${summary.reason}`;
  if (summary.unmatched?.length) text += ` [matches nothing: ${summary.unmatched.join(', ')}]`;
  return text;
}

export async function mount(host, ctx) {
  host.classList.add('dash');
  // The cfg.js host idiom: a bounded flex column, so .dash-content (flex:1, overflow-y:auto)
  // is THE scroll surface — one scrollbar for the whole board, never per-tile scrolling.
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';
  const content = document.createElement('div');
  content.className = 'dash-content';
  host.append(content);

  let board = null; // last-good board (kept through fetch failures)
  let hint = null; // absent-store hint
  let degraded = false;
  const expanded = new Set();

  function showMessage(text) {
    content.replaceChildren();
    const box = document.createElement('div');
    box.className = 'kit-message';
    box.textContent = text;
    content.appendChild(box);
  }

  function renderTile(tile, stale) {
    const state = tileState(tile, stale);
    const el = document.createElement('div');
    el.className = `dash-tile dash-${state}`;

    const head = document.createElement('div');
    head.className = 'dash-tile-head';
    const name = document.createElement('span');
    name.className = 'dash-tile-name';
    name.textContent = tile.signal;
    const word = document.createElement('span');
    word.className = `dash-tile-state dash-word-${state}`;
    word.textContent = state;
    const count = document.createElement('span');
    count.className = 'dash-tile-count';
    const { problems, maint, ok } = partitionItems(tile.items);
    count.textContent = `${ok.length}/${tile.items.length - maint.length}`;
    head.append(name, word, count);
    el.appendChild(head);

    const open = expanded.has(tile.signal);
    // Expanded: everything (maintenance rows dimmed, after the problems). Collapsed: the
    // first problems, plus one dim "maintenance (n)" line so planned work is never hidden.
    const items = [...problems, ...maint, ...ok];
    const shown = open ? items : problems.slice(0, 3);
    if (shown.length || maint.length) {
      const list = document.createElement('div');
      list.className = 'dash-items';
      for (const item of shown) {
        const row = document.createElement('div');
        row.className = item.maintenance ? 'dash-item dash-item-maint' : 'dash-item';
        const dot = document.createElement('span');
        // Stale board ⇒ item dots go muted too: old colors must not read as live judgment.
        const dotState = stale ? 'stale' : item.maintenance ? 'maint' : item.state;
        dot.className = `dash-dot dash-dot-${dotState}`;
        const label = document.createElement('span');
        label.className = 'dash-item-label';
        label.textContent = item.label;
        const detail = document.createElement('span');
        detail.className = 'dash-item-detail';
        detail.textContent = item.detail || '';
        row.append(dot, label, detail);
        list.appendChild(row);
      }
      if (!open && maint.length) {
        const group = document.createElement('div');
        group.className = 'dash-maint-group';
        const names = maint.slice(0, 4).map((i) => i.label).join(', ');
        const extra = maint.length > 4 ? ` +${maint.length - 4}` : '';
        group.textContent = `maintenance (${maint.length}): ${names}${extra}`;
        list.appendChild(group);
      }
      const hidden = problems.length - shown.length;
      if (!open && hidden > 0) {
        const more = document.createElement('div');
        more.className = 'dash-more';
        more.textContent = `+${hidden} more`;
        list.appendChild(more);
      }
      el.appendChild(list);
    }
    el.addEventListener('click', () => {
      if (expanded.has(tile.signal)) expanded.delete(tile.signal);
      else expanded.add(tile.signal);
      render();
    });
    return el;
  }

  function render() {
    if (!board) {
      showMessage(hint ?? 'no dashboard snapshot yet — run: obi dash run (cron)');
      ctx.setBadge(null);
      return;
    }
    const now = Date.now();
    const stale = isStale(board, now);
    content.replaceChildren();
    if (stale) {
      const warn = document.createElement('div');
      warn.className = 'dash-stale-banner';
      warn.textContent =
        'board is stale — the dash cron is not writing; check: obi web status / obi dash run';
      content.appendChild(warn);
    }
    const maintText = maintenanceLine(board.maintenance);
    if (maintText) {
      const maintBanner = document.createElement('div');
      maintBanner.className = 'dash-maint-banner';
      maintBanner.textContent = `MAINT: ${maintText}`;
      content.appendChild(maintBanner);
    }
    const grid = document.createElement('div');
    grid.className = 'dash-grid';
    for (const tile of board.tiles) grid.appendChild(renderTile(tile, stale));
    content.appendChild(grid);
    for (const note of board.notes ?? []) {
      const line = document.createElement('div');
      line.className = 'dash-note';
      line.textContent = `note: ${note}`;
      content.appendChild(line);
    }
    ctx.setBadge(badgeText(board, now, { degraded }));
  }

  async function refresh() {
    let payload = null;
    try {
      payload = await fetchJson('/api/dash.json');
    } catch {
      degraded = true; // keep the last-good board on screen; the badge says the feed is failing
      render();
      return;
    }
    degraded = false;
    if (payload.store === 'absent') {
      board = null;
      hint = payload.hint;
      render();
      return;
    }
    board = payload.board;
    hint = null;
    render();
  }

  const poller = createPoller(refresh, POLL_MS);
  const ageTimer = setInterval(render, 30 * 1000); // ages/staleness keep moving between polls

  return {
    unmount() {
      poller.stop();
      clearInterval(ageTimer);
    },
  };
}
