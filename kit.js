// obi widget kit (feature 038) — the shared vocabulary of DATA widgets.
//
// One column-feed primitive with two write modes (append = live tails, replace = the
// 039 batch's board tables), a filter row, a non-overlapping poller, and
// the one severity/health -> theme-token mapping. Nothing in this file may be specific to
// any single widget (tested) — that is what makes the 039 batch a stamping exercise.
// Contract: specs/038-web-data-widgets/contracts/widget-kit.md. Styling: theme.css tokens.

// --- severity/health -> semantic token class (FR-008) -------------------------------------------

export function severityColor(value) {
  switch ((value || '').toLowerCase()) {
    case 'critical':
    case 'error':
    case 'down':
    case 'disconnected':
    case 'unreachable':
      return 'kit-c-down';
    case 'warning':
    case 'degraded':
    case 'timed_out':
    case 'auth_failed':
      return 'kit-c-warn';
    case 'info':
    case 'debug':
    case 'healthy':
    case 'active':
    case 'reachable':
    case 'up':
      return 'kit-c-ok';
    default:
      return 'kit-c-muted'; // unknown/null: neutral, never dropped
  }
}

// --- data fetch primitive (feature 068) ---------------------------------------------------------
// The one fetch→check→parse shape the data widgets repeated by hand. Read-only GET, no-store by
// default (a live poll; pass `cache: 'no-cache'` for cached export artifacts). Throws on a non-2xx,
// preferring the server's `{error}` message when present and degrading to `HTTP <status>` otherwise
// — so a caller's try/catch → degrade-to-last-good keeps working unchanged. An empty-store shape in
// the body stays the caller's to branch on (a state, not an error). Returns the parsed JSON body.
export async function fetchJson(url, { cache = 'no-store' } = {}) {
  const response = await fetch(url, { cache });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body && body.error) detail = body.error;
    } catch {
      // a non-JSON error body (e.g. a proxy 502 page): keep the HTTP status
    }
    throw new Error(detail);
  }
  return response.json();
}

// --- copy & export primitives (feature 049) -----------------------------------------------------
// The one shared vocabulary for "get text out of obi": a copy control every text pane AND the table
// feed reuse, plus the pure serializers the feed's copy/CSV are built from. Contract:
// specs/049-web-copy-export/contracts/kit-copy-export.md. Nothing here is widget-specific.

// A one-click copy control. `getText` is read at CLICK time (always the live pane/table content),
// written to the clipboard with a graceful, non-throwing fallback where the Clipboard API is
// unavailable (insecure origin / permission denied). The server is https/localhost — a secure
// context — so `navigator.clipboard` is the primary path; the textarea path covers the edges.
export function createCopyButton(getText, { label = 'copy', title } = {}) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'kit-copy';
  button.textContent = label;
  if (title) button.title = title;

  let restoreTimer = 0;
  function flash(text, ok) {
    button.textContent = text;
    button.classList.toggle('kit-copied', ok);
    button.classList.toggle('kit-copy-fail', !ok);
    clearTimeout(restoreTimer);
    restoreTimer = setTimeout(() => {
      button.textContent = label;
      button.classList.remove('kit-copied', 'kit-copy-fail');
    }, 1200);
  }

  button.addEventListener('click', async () => {
    const text = String(getText() ?? '');
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        flash('copied', true);
        return;
      } catch {
        /* denied or unavailable: fall through to the legacy path */
      }
    }
    const ok = legacyCopy(text);
    flash(ok ? 'copied' : '⌘/Ctrl-C', ok);
  });
  return button;
}

// Hidden-textarea + execCommand('copy') fallback, run inside the click gesture; returns whether the
// copy succeeded so the caller can label accurately without copying twice.
function legacyCopy(text) {
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

// Serialize a feed's columns + rows to a matrix (header labels + one string cell per column per
// row), using EXACTLY renderRow's cell projection so an export always matches what is on screen.
export function rowsToMatrix(columns, rows) {
  const header = columns.map((column) => String(column.label ?? column.key ?? ''));
  const body = rows.map((row) =>
    columns.map((column) => {
      const value = row[column.key];
      return value == null ? '' : String(value);
    }));
  return [header, ...body];
}

// Tab-separated (clipboard): feed cell values never contain tabs, so no escaping is required and a
// spreadsheet accepts the pasted block directly.
export function toTSV(matrix) {
  return matrix.map((cells) => cells.join('\t')).join('\n');
}

// RFC 4180 CSV (file): quote a field iff it holds a comma, quote, CR or LF; double embedded quotes;
// terminate rows with CRLF so the file re-imports faithfully.
export function toCSV(matrix) {
  const encodeCell = (cell) => (/[",\r\n]/.test(cell) ? `"${cell.replace(/"/g, '""')}"` : cell);
  return matrix.map((cells) => cells.map(encodeCell).join(',')).join('\r\n');
}

// Filename timestamp: local wall-clock at click time, second precision, human readable yet
// filesystem-safe (no colons or spaces) — e.g. 2026-08-19_14-32-05.
export function timestampSlug(date = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `_${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`;
}

// Client-side file download (no server route): a Blob + object URL + a synthetic anchor click.
export function downloadCsv(filename, text) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

// --- row context annotation (feature 056) -------------------------------------------------------
// The right-click "ask obi about this" pipeline resolves the clicked element by walking up to the
// nearest ancestor carrying this annotation. It is a NON-ENUMERABLE property so it never leaks into
// an export, a filter, or JSON. The kit stays widget-agnostic (composes NO context text): it only
// carries the row/columns/rows + the widget's describe hook; the shell calls the hook and composes.
// Contract: specs/056-web-chat-ux/contracts/ask-obi-menu.md.

export const CONTEXT_PROP = '__obiContext';

export function annotateContext(element, descriptor) {
  Object.defineProperty(element, CONTEXT_PROP, {
    value: descriptor,
    enumerable: false,
    configurable: true,
    writable: true,
  });
}

// --- the column feed ------------------------------------------------------------------------------

export function createFeed(
  host,
  { columns, cap = 500, tail = true, persist = null, onRowClick = null, exportName = null,
    contextFor = null },
) {
  // The column template lives ONCE, as the --kit-cols custom property on the feed root;
  // theme.css applies it to the head and every row, so their tracks can never diverge (046).
  const defaultTracks = columns.map((column) =>
    column.width ? `${column.width}` : 'minmax(0, 1fr)');

  // --- resizable columns (046 US3): px tracks + a fr absorber, persisted per widget kind --------

  const MIN_COL_PX = 24; // a drag can narrow a column to ~3 characters, never to zero
  const TRACK_RE = /^(\d+px|minmax\(0, 1fr\))$/;
  const sig = columns.map((column) => column.key).join('|');
  const storeKey = persist ? `obi-web-cols:${persist}` : null;

  function loadTracks() {
    if (!storeKey) return null;
    try {
      const raw = JSON.parse(localStorage.getItem(storeKey) ?? 'null');
      // A sig mismatch means the column set changed since the widths were saved: discard,
      // never render a half-shifted layout.
      if (raw?.sig === sig && Array.isArray(raw.cols) && raw.cols.length === columns.length
          && raw.cols.every((track) => TRACK_RE.test(track))) return raw.cols;
    } catch {
      /* storage unavailable or corrupt record: fall through to the defaults */
    }
    return null;
  }

  let tracks = loadTracks(); // null = spec defaults; array = operator-resized tracks

  function saveTracks() {
    if (!storeKey) return;
    try {
      if (tracks) localStorage.setItem(storeKey, JSON.stringify({ sig, cols: tracks }));
      else localStorage.removeItem(storeKey);
    } catch {
      /* storage unavailable: the resize still holds for this session */
    }
  }

  const root = document.createElement('div');
  root.className = 'kit-feed';

  function applyTracks() {
    root.style.setProperty('--kit-cols', (tracks ?? defaultTracks).join(' '));
  }
  applyTracks();

  root.innerHTML = `
    <div class="kit-head"></div>
    <div class="kit-body"></div>
    <div class="kit-state" hidden></div>
    <div class="kit-tools"></div>
    <button type="button" class="kit-live" hidden>▼ live</button>`;
  const head = root.querySelector('.kit-head');
  const body = root.querySelector('.kit-body');
  const stateEl = root.querySelector('.kit-state');
  const tools = root.querySelector('.kit-tools');
  const liveChip = root.querySelector('.kit-live');

  // Copy (visible rows → TSV, header + current sort/filter) and CSV (→ an RFC-4180 file download).
  // Both read displayRows() at click time, so they always reflect what's on screen (049). The
  // cluster overlays the top-right and lives outside the head cells, so it can't cross-fire with a
  // column sort or a resize drag.
  const exportBase = exportName || persist || 'table';
  tools.append(
    createCopyButton(() => toTSV(rowsToMatrix(columns, displayRows())),
      { title: 'copy table as tab-separated text' }),
  );
  const csvButton = document.createElement('button');
  csvButton.type = 'button';
  csvButton.className = 'kit-csv';
  csvButton.textContent = 'csv';
  csvButton.title = 'download this table as CSV';
  csvButton.addEventListener('click', () =>
    downloadCsv(`obi-${exportBase}-${timestampSlug()}.csv`, toCSV(rowsToMatrix(columns, displayRows()))));
  tools.append(csvButton);
  function makeHandle(index) {
    // The handle is its OWN element: a drag (or its trailing click) can never reach the
    // sort target, and a plain click on the label can never resize.
    const handle = document.createElement('span');
    handle.className = 'kit-resize';
    handle.addEventListener('click', (event) => event.stopPropagation());
    handle.addEventListener('dblclick', (event) => {
      event.stopPropagation();
      tracks = null; // reset this table to its spec-default widths
      applyTracks();
      saveTracks();
    });
    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      event.stopPropagation();
      handle.setPointerCapture(event.pointerId);
      const widths = headCells.map((entry) => entry.cell.getBoundingClientRect().width);
      const startX = event.clientX;
      const startWidth = widths[index];
      const onMove = (moveEvent) => {
        const width = Math.max(MIN_COL_PX, Math.round(startWidth + moveEvent.clientX - startX));
        // Dragged + untouched columns go px; the LAST track stays the fr absorber so the
        // grid always fits its container.
        tracks = widths.map((w, position) =>
          position === columns.length - 1
            ? 'minmax(0, 1fr)'
            : `${position === index ? width : Math.round(w)}px`);
        applyTracks();
      };
      const onUp = () => {
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onUp);
        saveTracks();
      };
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
    });
    return handle;
  }

  const headCells = [];
  columns.forEach((column, index) => {
    const cell = document.createElement('span');
    const label = document.createElement('span');
    label.className = 'kit-label';
    label.textContent = column.label;
    cell.appendChild(label);
    if (index < columns.length - 1) cell.appendChild(makeHandle(index));
    if (column.align === 'right') cell.classList.add('kit-right');
    cell.addEventListener('click', () => cycleSort(column));
    head.appendChild(cell);
    headCells.push({ cell, label });
  });
  host.appendChild(root);

  let rows = []; // the model, in arrival order (= natural order); the DOM is a projection

  // --- sorting (046 US2): one sort column, asc -> desc -> natural; session-only ------------------

  let sortKey = null; // null = natural (arrival) order
  let sortDir = 'asc';

  function compareCells(a, b) {
    // Assumes non-empty operands. Numeric when both are numbers; otherwise natural-aware
    // text ("eth2" < "eth10", ISO timestamps chronological).
    const as = String(a).trim();
    const bs = String(b).trim();
    const an = Number(as);
    const bn = Number(bs);
    if (as !== '' && bs !== '' && Number.isFinite(an) && Number.isFinite(bn)) return an - bn;
    return as.localeCompare(bs, undefined, { numeric: true, sensitivity: 'base' });
  }

  function displayRows() {
    const column = columns.find((candidate) => candidate.key === sortKey);
    if (!column) return rows;
    const field = column.sort ?? column.key; // per-column raw sort-key opt-in
    const sign = sortDir === 'desc' ? -1 : 1;
    return [...rows].sort((ra, rb) => {
      // Stable (Array.prototype.sort); empty values group last in BOTH directions.
      const a = ra[field];
      const b = rb[field];
      const aEmpty = a == null || a === '';
      const bEmpty = b == null || b === '';
      if (aEmpty || bEmpty) return aEmpty && bEmpty ? 0 : aEmpty ? 1 : -1;
      return sign * compareCells(a, b);
    });
  }

  function updateSortHeader() {
    columns.forEach((column, index) => {
      const { cell, label } = headCells[index];
      const active = column.key === sortKey;
      label.textContent = active
        ? `${column.label} ${sortDir === 'asc' ? '▲' : '▼'}`
        : column.label;
      if (active) cell.setAttribute('aria-sort', sortDir === 'asc' ? 'ascending' : 'descending');
      else cell.removeAttribute('aria-sort');
    });
  }

  function cycleSort(column) {
    if (sortKey !== column.key) {
      sortKey = column.key;
      sortDir = 'asc';
    } else if (sortDir === 'asc') {
      sortDir = 'desc';
    } else {
      sortKey = null; // third click: back to natural order
    }
    // A sorted view has no tail: follow (and its chip) suspend while a sort is active and
    // resume when it clears with the view at the bottom.
    if (tail) {
      if (sortKey) setFollow(false);
      liveChip.hidden = follow || !tail || sortKey !== null;
    }
    updateSortHeader();
    rerender();
    if (tail && !sortKey && nearBottom()) setFollow(true);
    if (follow) body.scrollTop = body.scrollHeight;
  }

  let follow = tail; // board mode (tail:false): top-anchored, no follow machinery
  let followListeners = [];
  const nearBottom = () =>
    body.scrollHeight - body.scrollTop - body.clientHeight < 24;

  function setFollow(value) {
    if (follow === value) return;
    follow = value;
    liveChip.hidden = follow || !tail || sortKey !== null;
    followListeners.forEach((cb) => cb(follow));
  }

  if (tail) {
    body.addEventListener('scroll', () => {
      if (sortKey !== null) return; // follow machinery is suspended while sorted (046 US2)
      // Scrolling up pauses the tail WITHOUT moving the view; returning to the bottom
      // (or the chip) resumes it.
      if (follow && !nearBottom()) setFollow(false);
      else if (!follow && nearBottom()) setFollow(true);
    });
  }
  liveChip.addEventListener('click', () => {
    body.scrollTop = body.scrollHeight;
    setFollow(true);
  });

  function renderRow(row) {
    const el = document.createElement('div');
    el.className = 'kit-row';
    for (const column of columns) {
      const cell = document.createElement('span');
      const value = row[column.key];
      // textContent only: log/message content is data, never markup (kit contract).
      cell.textContent = value == null ? '' : String(value);
      if (column.align === 'right') cell.classList.add('kit-right');
      if (column.color) {
        // A color fn may return '' for "no styling" (088: the RADIO who/mark columns);
        // classList.add('') throws and would kill the whole row render. The row rides
        // along as a second argument (088: confidence coloring needs a sibling field);
        // single-argument color fns simply ignore it.
        const colorClass = column.color(value, row);
        if (colorClass) cell.classList.add(colorClass);
      }
      if (column.title && row[column.title] != null) cell.title = String(row[column.title]);
      el.appendChild(cell);
    }
    if (onRowClick) {
      // Opt-in row drill-down: a board hands the full row object (incl. any non-column
      // metadata it stashed) to its handler — the watcher opens an in-board detail panel,
      // ROS opens a per-router window. Body rows are separate from the header sort/resize
      // targets, so a row click can never cross-fire with a sort or a column drag.
      el.classList.add('kit-row-clickable');
      el.addEventListener('click', () => onRowClick(row));
    }
    if (contextFor) {
      // 056: annotate the row so a right-click resolves it. The kit composes nothing — it hands
      // the widget's describe hook the row + columns + a live view of the model (for neighbors).
      annotateContext(el, { row, columns, getRows: () => rows, contextFor });
    }
    return el;
  }

  function trimModel() {
    // Eviction is ALWAYS by arrival order, whatever order is displayed.
    if (rows.length > cap) rows.splice(0, rows.length - cap);
  }

  function rerender() {
    body.replaceChildren(...displayRows().map(renderRow));
  }

  function clearState() {
    stateEl.hidden = true;
    stateEl.textContent = '';
    stateEl.className = 'kit-state';
  }

  return {
    get follow() {
      return follow;
    },
    setFollow,
    onFollowChange(cb) {
      followListeners.push(cb);
    },
    append(newRows) {
      if (!newRows.length) return;
      clearState();
      if (sortKey !== null) {
        // Sorted view: new arrivals land in sorted position; no tail to follow.
        rows.push(...newRows);
        trimModel();
        rerender();
        return;
      }
      const pinned = follow;
      for (const row of newRows) {
        rows.push(row);
        body.appendChild(renderRow(row)); // natural order: append-only fast path
      }
      trimModel();
      while (body.children.length > cap) body.firstChild.remove();
      if (pinned) body.scrollTop = body.scrollHeight; // paused view is never yanked
    },
    replace(newRows) {
      clearState();
      rows = [...newRows];
      trimModel();
      rerender();
      if (follow) body.scrollTop = body.scrollHeight;
    },
    showState(kind, message) {
      // 'loading' | 'empty' | 'error' | 'degraded' — explicit, never a blank pane. Shown
      // as a banner over the feed; existing rows stay visible (degraded keeps history).
      stateEl.hidden = false;
      stateEl.textContent = message;
      stateEl.className = `kit-state kit-state-${kind}`;
    },
    clear() {
      rows = [];
      body.replaceChildren();
      clearState();
    },
    destroy() {
      followListeners = [];
      root.remove();
    },
  };
}

// --- the filter row ---------------------------------------------------------------------------------

export function createFilterRow(host, fields, onChange) {
  const row = document.createElement('div');
  row.className = 'kit-filters';
  const values = {};
  const controls = {}; // key -> {wrap, el, kind}: handles for dynamic option/visibility updates
  let debounceTimer = 0;

  function emit() {
    onChange({ ...values });
  }

  function fillSelect(select, options, selected) {
    select.replaceChildren();
    for (const [value, label] of options) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      if (value === selected) option.selected = true;
      select.appendChild(option);
    }
  }

  for (const field of fields) {
    values[field.key] = field.value ?? '';
    const wrap = document.createElement('label');
    wrap.className = 'kit-filter';
    const caption = document.createElement('span');
    caption.textContent = field.label;
    wrap.appendChild(caption);
    if (field.kind === 'select') {
      const select = document.createElement('select');
      fillSelect(select, field.options, values[field.key]);
      select.addEventListener('change', () => {
        values[field.key] = select.value;
        emit(); // selects fire immediately
      });
      wrap.appendChild(select);
      controls[field.key] = { wrap, el: select, kind: 'select' };
    } else {
      const input = document.createElement('input');
      input.type = 'text';
      input.autocomplete = 'off';
      input.spellcheck = false;
      if (field.placeholder) input.placeholder = field.placeholder;
      input.addEventListener('input', () => {
        values[field.key] = input.value.trim();
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(emit, 300); // text debounces
      });
      wrap.appendChild(input);
      controls[field.key] = { wrap, el: input, kind: 'text' };
    }
    row.appendChild(wrap);
  }
  host.appendChild(row);
  return {
    get values() {
      return { ...values };
    },
    // Replace a select's options in place — silent (never emits). The current value is kept
    // when still offered; pass `value` to move the selection; otherwise it falls to the first.
    setOptions(key, options, value) {
      const control = controls[key];
      if (!control || control.kind !== 'select') return;
      let next = value !== undefined ? value : values[key];
      if (!options.some(([v]) => v === next)) next = options.length ? options[0][0] : '';
      values[key] = next;
      fillSelect(control.el, options, next);
    },
    setHidden(key, hidden) {
      const control = controls[key];
      if (control) control.wrap.hidden = Boolean(hidden);
    },
    destroy() {
      clearTimeout(debounceTimer);
      row.remove();
    },
  };
}

// --- the tab strip (feature 075) ----------------------------------------------------------------
// A thin generalization of conf.js's proven makeTab/syncTabs closure: a row of buttons, `.active`
// toggling, and an onActivate callback the caller uses to show/hide its panes. The active tab
// persists per WIDGET KIND under `obi-web-tab:<persist>` — kit.js is the sanctioned owner of this
// key family (as it is of obi-web-cols:*), so all windows of a kind share the last-selected tab
// (the 046 twin-window rule). Restore default is the first tab (missing/invalid stored value), which
// also covers pre-feature layouts. `tabs` is [{id, label}]. Contract: specs/075-router-phy.

export function createTabs(host, { tabs, persist = null, onActivate = null } = {}) {
  const strip = document.createElement('div');
  strip.className = 'kit-tabs';
  const buttons = new Map();
  const storeKey = persist ? `obi-web-tab:${persist}` : null;
  let active = null;

  function saveActive(id) {
    if (!storeKey) return;
    try {
      localStorage.setItem(storeKey, id);
    } catch {
      /* storage unavailable (private mode): the choice still holds for this session */
    }
  }

  function loadActive() {
    if (!storeKey) return null;
    try {
      return localStorage.getItem(storeKey);
    } catch {
      return null;
    }
  }

  function activate(id, { persist: doPersist = true } = {}) {
    if (!buttons.has(id) || id === active) {
      if (id === active) return; // a re-click of the active tab is a no-op (no double render)
      return;
    }
    active = id;
    for (const [tid, button] of buttons) button.classList.toggle('active', tid === id);
    if (doPersist) saveActive(id);
    onActivate?.(id);
  }

  for (const tab of tabs) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'kit-tab';
    button.textContent = tab.label;
    button.addEventListener('click', () => activate(tab.id));
    strip.appendChild(button);
    buttons.set(tab.id, button);
  }
  host.appendChild(strip);

  // Restore the persisted tab (silently — no double-persist), defaulting to the first tab. The
  // initial onActivate fires so the caller mounts the starting pane.
  const stored = loadActive();
  const initial = stored && buttons.has(stored) ? stored : tabs[0]?.id;
  if (initial != null) activate(initial, { persist: false });

  return {
    element: strip,
    get active() {
      return active;
    },
    activate,
  };
}

// --- the panel primitive (feature 076) ----------------------------------------------------------
// The ONE widget panel language: a floating (default) or modal quick-reference surface with a head
// that rhymes with `.win-title` (a panel reads as a small window), a scoped Escape, a close ✕, and
// an optional pop-out to a real window. Before 076 this chrome was hand-rolled three divergent ways
// (boards.createDetailPanel, ospf/viewer2d absolute overlays, the un-dismissable phy inspector);
// createPanel is their single generalization. Contract: specs/076-widget-language/contracts/kit-panel.md.

// The "is the user typing?" guard, shared with shell.js (D2 — one definition). Used to let Escape
// pass through to a focused text field instead of closing a panel / the command line.
export function isTypingTarget(el) {
  if (!el) return false;
  return el.matches?.('input, textarea, select') || el.isContentEditable === true;
}

/**
 * A widget panel: title-bar chrome (title, optional pop-out, close ✕) + a scrolling body, floating
 * within `host` by default or modal (dimmed backdrop) when `{ modal: true }`. The body is always
 * DOM the caller built with textContent — the panel never interpolates data into markup.
 *
 * @param {Element} host  the widget region the panel anchors inside (gets position:relative if unset)
 * @param {object|null} ctx  the widget's shell ctx; used for ctx.mode (pop-out visibility) and
 *   ctx.openSnapshot (the snapshot pop-out fallback). null ⇒ no pop-out button (harness/tests).
 * @param {{modal?: boolean, onClose?: () => void}} [opts]  onClose fires on any open→closed
 *   transition (✕, Esc, backdrop, or a programmatic close()) — a widget uses it to reconcile its
 *   own selection state (e.g. deselect the inspected port). It must not itself call close().
 * @returns {{ open: (o: {title: string, body: Element, popOut?: {open: () => void}}) => void,
 *   close: () => void, isOpen: () => boolean, destroy: () => void }}
 *   open() replaces content and shows (re-open while open swaps in place, position kept); close()
 *   hides + clears the body + drops the keydown listener; destroy() (mandatory in unmount()) tears
 *   down all DOM/listeners and makes a later open() throw. Exactly one document keydown listener
 *   exists while open, zero when closed/destroyed. The pop-out button renders only in a real
 *   workspace window that has a pop-out path (ctx.mode === 'window' and a declared popOut or
 *   ctx.openSnapshot); its click runs `popOut.open()` else `ctx.openSnapshot(title, body)`, then closes.
 */
export function createPanel(host, ctx, { modal = false, onClose = null } = {}) {
  if (!host.style.position) host.style.position = 'relative'; // anchor the absolute panel/backdrop

  const panel = document.createElement('div');
  panel.className = 'kit-panel';
  const headEl = document.createElement('div');
  headEl.className = 'kit-panel-head';
  const titleEl = document.createElement('span');
  titleEl.className = 'kit-panel-title';
  const popoutBtn = document.createElement('button');
  popoutBtn.type = 'button';
  popoutBtn.className = 'kit-panel-popout';
  popoutBtn.title = 'open as window';
  popoutBtn.textContent = '⧉';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'kit-panel-close';
  closeBtn.setAttribute('aria-label', 'close');
  closeBtn.textContent = '✕';
  headEl.append(titleEl, popoutBtn, closeBtn);
  const bodyEl = document.createElement('div');
  bodyEl.className = 'kit-panel-body';
  panel.append(headEl, bodyEl);

  // Modal wraps the panel in a dimmed, click-to-dismiss backdrop (the createDetailPanel semantics);
  // floating mounts the bare panel, bottom-right anchored, host content still interactive.
  let root = panel;
  let backdrop = null;
  if (modal) {
    backdrop = document.createElement('div');
    backdrop.className = 'kit-panel-backdrop';
    backdrop.appendChild(panel);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) close(); // the dim, never the panel itself
    });
    root = backdrop;
  }

  let open_ = false;
  let destroyed = false;
  let popOutThunk = null; // this open()'s popOut.open, if any

  function onKey(event) {
    if (event.key !== 'Escape' || isTypingTarget(event.target)) return;
    // Scoped to the focused window; fullscreen pages have no `.win` ancestor and are always focused.
    if (!(host.closest('.win')?.classList.contains('focused') ?? true)) return;
    close();
  }

  function close() {
    if (!open_) return;
    open_ = false;
    root.remove();
    bodyEl.replaceChildren();
    popOutThunk = null;
    document.removeEventListener('keydown', onKey);
    onClose?.(); // let the widget reconcile its own selection state (must not re-enter close())
  }

  closeBtn.addEventListener('click', close);
  popoutBtn.addEventListener('click', () => {
    const title = titleEl.textContent;
    const body = bodyEl.firstElementChild; // capture before close() clears the body
    if (popOutThunk) popOutThunk();
    else ctx?.openSnapshot?.(title, body); // absent until US2; a no-op when unavailable
    close();
  });

  function open({ title, body, popOut } = {}) {
    if (destroyed) throw new Error('createPanel: open() after destroy()');
    titleEl.textContent = title ?? '';
    if (body) bodyEl.replaceChildren(body);
    else bodyEl.replaceChildren();
    bodyEl.scrollTop = 0;
    popOutThunk = popOut?.open ?? null;
    // The button shows only in a real window WITH a pop-out path — so no dead button while
    // openSnapshot doesn't exist yet (US1) and none on the fullscreen page (D6/FR-010).
    popoutBtn.hidden = !(ctx?.mode === 'window'
      && (popOutThunk || typeof ctx?.openSnapshot === 'function'));
    if (!open_) {
      host.appendChild(root);
      document.addEventListener('keydown', onKey);
      open_ = true;
    }
  }

  return {
    open,
    close,
    isOpen: () => open_,
    destroy() {
      close();
      destroyed = true;
      root.remove();
    },
  };
}

/**
 * A single floating hover tooltip (feature 076, D7) — the one bespoke rich-hover surface, kit-owned.
 * A `.kit-tooltip` node on document.body, pointer-events:none, positioned at the pointer +12/+12 and
 * nudged back inside the viewport. For rich hovers on SVG/canvas (phy's faceplate); table cells keep
 * their native `title=` via the feed's column mechanism.
 * @returns {{ show: (text: string, event: MouseEvent) => void, move: (event: MouseEvent) => void,
 *   hide: () => void, destroy: () => void }}  destroy() (call from unmount()) removes the node.
 */
export function createTooltip() {
  const el = document.createElement('div');
  el.className = 'kit-tooltip';
  el.hidden = true;
  document.body.appendChild(el);
  function place(event) {
    const pad = 12;
    const rect = el.getBoundingClientRect();
    let x = event.clientX + pad;
    let y = event.clientY + pad;
    if (x + rect.width > window.innerWidth) x = event.clientX - pad - rect.width;
    if (y + rect.height > window.innerHeight) y = event.clientY - pad - rect.height;
    el.style.left = `${Math.max(0, x)}px`;
    el.style.top = `${Math.max(0, y)}px`;
  }
  return {
    show(text, event) { el.textContent = text; el.hidden = false; place(event); },
    move(event) { if (!el.hidden) place(event); },
    hide() { el.hidden = true; },
    destroy() { el.remove(); },
  };
}

/**
 * A one-line status readout (feature 076) — the phy/router `.ok/.warn/.down/.muted` line, kit-owned
 * once. Appends one `.kit-status` div to `host`.
 * @returns {{ set: (text: string|null, tone?: 'ok'|'warn'|'down'|'muted') => void, element: Element }}
 *   set(text, tone) shows the text in the tone color; set(null) hides it.
 */
export function createStatusLine(host) {
  const el = document.createElement('div');
  el.className = 'kit-status';
  el.hidden = true;
  host.appendChild(el);
  return {
    element: el,
    set(text, tone) {
      if (text == null || text === '') {
        el.hidden = true;
        el.textContent = '';
        el.className = 'kit-status';
        return;
      }
      el.hidden = false;
      el.textContent = text;
      el.className = tone ? `kit-status ${tone}` : 'kit-status';
    },
  };
}

/**
 * A ⟳ refresh button (feature 076) — the phy/router byte-duplicate, kit-owned. Disables itself while
 * the promise from `onRefresh()` is pending, re-enables on settle (so a slow read can't stack).
 * @returns {HTMLButtonElement} a `.kit-btn.kit-refresh` button.
 */
export function createRefreshButton(onRefresh) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'kit-btn kit-refresh';
  button.textContent = '⟳ refresh';
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      await onRefresh();
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

// Deep-clone a node into an INERT snapshot (feature 076): every interactive control is disabled,
// anchors lose their href, and ids are stripped (no duplicate-id leaks). cloneNode(true) inherently
// drops JS listeners and the annotateContext expando, so a snapshot's right-click degrades to the
// shell's generic text seed — text selection/scroll/copy still work. Used by ctx.openSnapshot to
// mint a snapshot window that can never masquerade as live, interactive data.
export function inertClone(node) {
  const clone = node.cloneNode(true);
  for (const control of clone.querySelectorAll?.('button, select, input, textarea') ?? []) {
    control.disabled = true;
  }
  for (const anchor of clone.querySelectorAll?.('a[href]') ?? []) {
    anchor.removeAttribute('href');
  }
  for (const withId of clone.querySelectorAll?.('[id]') ?? []) {
    withId.removeAttribute('id');
  }
  clone.removeAttribute?.('id'); // the root node itself, if it carried one
  return clone;
}

// --- the poller --------------------------------------------------------------------------------------

export function createPoller(fn, ms) {
  // Immediate first run, then every `ms` — with an in-flight guard so a slow response
  // skips ticks instead of stacking requests. stop() is mandatory in unmount().
  let running = false;
  let stopped = false;

  async function tick() {
    if (running || stopped) return;
    running = true;
    try {
      await fn();
    } finally {
      running = false;
    }
  }

  const timer = setInterval(tick, ms);
  tick();
  return {
    poke: tick, // callers may request an off-cycle run (e.g. right after a filter change)
    stop() {
      stopped = true;
      clearInterval(timer);
    },
  };
}
