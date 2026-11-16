// Per-device config window (feature 051) — one device's config-backup history plus the 049 detail
// view (diff⇄full-config toggle, inline line filter, copy), moved out of the CFG coverage widget so
// each device opens in its own window and many coexist (the 047 router-window pattern, for configs).
//
// Opened from a CFG coverage row (ctx.openConfig) or `/conf <device>`; the device is ctx.params.device.
// Read-only: every read is a local 026 git read via /api/configs.json (history/diff/show modes),
// intent-audited server-side (never config content) — no device is contacted. Reuses the .cfg-*
// styling from 041/049, so it adds no CSS.

import { annotateContext, createCopyButton, createTabs, fetchJson as apiFetch } from '../kit.js';

export async function mount(host, ctx) {
  const device = ctx.params?.device ?? '';
  host.classList.add('cfg');
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';

  const content = document.createElement('div');
  content.className = 'cfg-content';
  host.append(content);
  ctx.setName?.(device);

  // Detail-pane state, persisted across version changes within this window (049):
  let detailMode = 'diff'; // 'diff' | 'show' (full config)
  let filterTerm = ''; // the full-config inline line filter

  function message(text) {
    return Object.assign(document.createElement('div'),
      { className: 'kit-message', textContent: text }); // 076: shared muted message box
  }

  function showMessage(text) {
    content.replaceChildren(message(text));
  }

  // 068: URL builder over the shared kit.fetchJson (the no-store fetch + error this widget inlined).
  const fetchJson = (query) => apiFetch(`/api/configs.json${query}`);

  function renderDiffLines(target, diffText) {
    const pre = document.createElement('pre');
    pre.className = 'cfg-diff';
    for (const line of diffText.split('\n')) {
      const span = document.createElement('span');
      span.textContent = line + '\n';
      if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'diff-add';
      else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'diff-del';
      else if (line.startsWith('@@')) span.className = 'diff-hunk';
      pre.appendChild(span);
    }
    target.replaceChildren(pre);
  }

  function renderHistory(payload) {
    content.replaceChildren();
    const list = document.createElement('div');
    list.className = 'cfg-history';

    // Detail pane: a diff⇄full-config toggle, an inline filter (full-config only), and copy.
    const detail = document.createElement('div');
    detail.className = 'cfg-detail';
    const toolbar = document.createElement('div');
    toolbar.className = 'cfg-toolbar';
    const detailHost = document.createElement('div');
    detailHost.className = 'cfg-detail-host';

    let activeCommit = null;
    let shownText = ''; // what's currently rendered — the copy source (diff text or filtered config)
    let fullConfig = ''; // last-fetched full config for the active version (unfiltered)
    let filterTimer = 0;

    const filterInput = document.createElement('input');
    filterInput.type = 'text';
    filterInput.className = 'cfg-filter';
    filterInput.placeholder = 'filter lines…';
    filterInput.autocomplete = 'off';
    filterInput.spellcheck = false;
    filterInput.value = filterTerm;
    const copyButton = createCopyButton(() => shownText, { title: 'copy the shown text' });
    copyButton.style.marginLeft = 'auto';
    // 076: the diff⇄full-config toggle is the shared kit tab strip (persisted per widget kind — all
    // conf windows share the last-selected view, the 046 twin-window rule). onActivate replaces the
    // old makeTab/syncTabs closure: sync detailMode, show the filter only in full-config, re-render.
    createTabs(toolbar, {
      tabs: [{ id: 'diff', label: 'diff' }, { id: 'show', label: 'full config' }],
      persist: 'conf-detail',
      onActivate: (id) => {
        detailMode = id;
        filterInput.hidden = id !== 'show';
        showDetail();
      },
    });
    toolbar.append(filterInput, copyButton);
    detail.append(toolbar, detailHost);
    content.append(list, detail);

    // 056: right-click the diff/config pane → ask obi about THIS device's config; the currently
    // shown text (diff or filtered config) rides along, bounded by the shell's transport cap.
    annotateContext(detail, {
      contextFor: () => ({
        subject: `config ${device}${activeCommit ? ` @ ${activeCommit}` : ''}`,
        elementText: `${detailMode === 'diff' ? 'diff' : 'config'} for ${device}:\n${shownText}`,
      }),
    });

    function renderConfig() {
      // Client-side line filter over the already-fetched config (049 R3): instant, no round-trip.
      const term = filterTerm.trim().toLowerCase();
      const lines = fullConfig.split('\n');
      const kept = term ? lines.filter((line) => line.toLowerCase().includes(term)) : lines;
      if (term && kept.length === 0) {
        shownText = '';
        detailHost.replaceChildren(message('no lines match'));
        return;
      }
      shownText = kept.join('\n');
      const pre = document.createElement('pre');
      pre.className = 'cfg-config';
      pre.textContent = shownText; // config content is data, never markup
      detailHost.replaceChildren(pre);
    }

    async function showDetail() {
      if (!activeCommit) return;
      try {
        if (detailMode === 'diff') {
          const diff = await fetchJson(
            `?device=${encodeURIComponent(device)}&view=diff&to=${encodeURIComponent(activeCommit)}`);
          if (!diff.found || !diff.changed) {
            shownText = '';
            detailHost.replaceChildren(message(diff.detail || 'no changes between these versions'));
          } else {
            shownText = diff.diff;
            renderDiffLines(detailHost, diff.diff);
          }
        } else {
          const show = await fetchJson(
            `?device=${encodeURIComponent(device)}&view=show&at=${encodeURIComponent(activeCommit)}`);
          if (!show.found) {
            fullConfig = '';
            shownText = '';
            detailHost.replaceChildren(message(show.detail || 'no stored config for this version'));
          } else {
            fullConfig = show.content || '';
            renderConfig();
          }
        }
      } catch (err) {
        shownText = '';
        detailHost.replaceChildren(message(`load failed: ${err.message}`));
      }
    }

    filterInput.addEventListener('input', () => {
      filterTerm = filterInput.value;
      clearTimeout(filterTimer);
      filterTimer = setTimeout(() => { if (detailMode === 'show') renderConfig(); }, 200);
    });

    let active = null;
    for (const entry of payload.entries) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'kit-btn cfg-entry'; // 076: shared button base + the full-width entry layout
      row.textContent =
        `${entry.captured_at.slice(0, 16).replace('T', ' ')}  ` +
        `+${entry.insertions}/-${entry.deletions}  ${entry.summary}`;
      row.addEventListener('click', () => {
        active?.classList.remove('active');
        active = row;
        row.classList.add('active');
        activeCommit = entry.commit;
        showDetail();
      });
      list.appendChild(row);
    }
    ctx.setBadge?.(`${payload.entries.length} version(s)${payload.truncated ? '+' : ''}`);
    // createTabs already restored the persisted view (diff/full-config) + the filter visibility.
    list.firstChild?.click(); // land on the newest version (in the persisted mode)
  }

  async function load() {
    if (!device) {
      showMessage('no device');
      return;
    }
    let payload = null;
    try {
      payload = await fetchJson(`?device=${encodeURIComponent(device)}`);
    } catch (err) {
      showMessage(`load failing (${err.message})`);
      return;
    }
    if (payload.store === 'absent') {
      showMessage(payload.hint);
      ctx.setBadge?.(null);
      return;
    }
    if (!payload.found) {
      showMessage(payload.detail || 'no history for this device');
      ctx.setBadge?.(null);
      return;
    }
    renderHistory(payload);
  }

  await load();

  return { unmount() {} };
}
