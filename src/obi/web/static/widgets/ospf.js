// OSPF map widget (feature 052, polished by 053) — the routing topology as a pannable/zoomable
// 2D graph, plus (053) the OSPF lint window (this same module mounted with params.view='lint',
// so the lint list is its own openable/closable window and no new served file was needed).
//
// Thin renderer (constitution III / research R3): every number here was precomputed by
// `obi web snapshot` into /snapshots/ospf.json — per-direction costs, the shortest-path tree
// from every root, ghost classes (including a one-sided ghost's intended peer), SPOF flags,
// shared-fate groups, external routes, the default-route statement, and the lint findings. This
// module does ZERO routing logic: it draws the graph, hit-tests it, re-roots the SPF tree by
// LOOKING UP spf[root] (never computing it), and opens a router's drill-down window on click.
// Node positions seed from the snapshot and are drag-adjustable, persisted per browser under
// obi-ospf-positions-v1 (the 4th sanctioned localStorage owner). No geodesy — the seeds arrive
// as plain x/y (test_no_widget_does_geodesy).
//
// 053 rendering notes: two routers can be joined by SEVERAL links (a formed adjacency plus one
// or two configured backups). Every router-pair connection — formed or ghost — is drawn as a
// quadratic curve with a per-connection perpendicular offset so parallel links never overlap,
// and each side is labeled individually with its interface name and outbound cost.

import { createCopyButton, createPanel } from '../kit.js';
import { formatAge } from '../boards.js'; // 076: the shared age formatter (was a local copy)

const POLL_MS = 5 * 60 * 1000; // the snapshot cadence every board shares
const POSITIONS_KEY = 'obi-ospf-positions-v1'; // dragged node positions (this widget's only store)
const FOCUS_EVENT = 'obi:ospf-focus'; // lint window → map focus (same-page windows, plain event)

// Cost-tier styling — the deliberate scheme (spec FR-012), labeled by VALUE only (053: the
// commentary lives in the operator's head; the map shows the numbers). Named constants,
// contract-tested.
const TIER_STYLE = {
  1: { color: '#22c55e', width: 3.6, label: '1' },
  100: { color: '#3987e5', width: 3.0, label: '100' },
  200: { color: '#eab308', width: 2.3, label: '200' },
  300: { color: '#f59e0b', width: 2.0, label: '300' },
  1000: { color: '#ef4444', width: 1.6, label: '1000' },
};
const TIER_NEUTRAL = { color: '#9ca3af', width: 2.0, label: 'other' };

// Ghost-edge styles (research R10): every ghost is GREY — a ghost is dormant debt, not live
// traffic, so no class may borrow a cost-tier color (broken was red and read as a cost-1000
// link on the canvas). One-sided = mid grey drawn to its intended peer when the snapshot
// resolved one; broken = the loudest ghost (a failed redundancy): brighter grey, thicker,
// long dashes; unpaired = faint stub off its owner (no peer exists yet). Named constants,
// contract-tested.
const GHOST_STYLE = {
  'one-sided': { color: '#9ca3af', dash: [5, 5], width: 1.6 },
  broken: { color: '#d1d5db', dash: [7, 4], width: 2.6 },
  unpaired: { color: '#9ca3af', dash: [2, 5], width: 1.3 },
};

const NODE_RADIUS = 7;
const TEXT = '#c9d5e1';
const MUTED = '#8a97a6';
// Node fill scheme (mirrors draw(): SPF root, enrichment-ok, stale/absent) — named so the legend
// (073) and the node renderer describe the same colors.
const NODE_ROOT = '#3987e5';
const NODE_OK = '#c9d5e1';
const NODE_STALE = '#6b7280';
const RING_SPOF = '#f97316'; // articulation ring + SPOF-link marker
const ASBR_DOT = '#a855f7'; // external-route origin badge
const PSEUDO_FILL = '#64748b'; // broadcast-segment square
// 073 internet cloud: a display-only node the default-originating ASBRs link to. The active
// link (this root's real egress) is solid/primary; the standbys are its own dashed grey.
const CLOUD_STYLE = { fill: '#20304a', stroke: '#8fb8de', text: '#c9d5e1', radius: 16 };
const DEFAULT_LINK_ACTIVE = { color: '#22c55e', width: 3.2 };
const DEFAULT_LINK_STANDBY = { color: '#9ca3af', width: 1.6, dash: [6, 5] };
// 073 backup emphasis: links OFF the selected root's shortest-path tree are dashed and slightly
// thinner — NOT dimmed by opacity. Canvas globalAlpha composites the stroke with the dark map
// background, shifting the apparent colour away from the full-opacity legend swatch; keeping full
// colour and signalling "backup" by dash+weight keeps the legend and the screen in agreement.
const OFFTREE_DASH = [6, 5];
const UI_KEY = '__ui__'; // reserved position-store key: panel show/hide state (073, FR-018)
const CLOUD_KEY = '__cloud__'; // reserved position-store key: dragged internet-cloud position (073)
const PARALLEL_GAP = 26; // screen px between parallel links of one router pair
// 054 label level-of-detail: a connection shorter than this on screen shows only its per-side
// costs; zoom in (or select it) and the interface names appear.
const LABEL_FULL_PX = 190;

function tierStyle(tier) {
  return TIER_STYLE[tier] ?? TIER_NEUTRAL;
}

function loadPositions() {
  try {
    const raw = JSON.parse(localStorage.getItem(POSITIONS_KEY) ?? '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function savePositions(positions) {
  try {
    localStorage.setItem(POSITIONS_KEY, JSON.stringify(positions));
  } catch {
    /* storage unavailable — positions apply for this session only */
  }
}

// 076: formatAge now imported from boards.js (see top). Callers keep their `.replace('data ', '')`
// — a harmless no-op against boards.formatAge's prefix-less '<1m'/'5m'/'2h' output.

async function fetchSnapshot() {
  const response = await fetch('/snapshots/ospf.json', { cache: 'no-store' });
  if (response.status === 404) return { missing: true };
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  const data = body.data;
  data.generated_at = body.generated_at; // carry the envelope stamp for the age readout
  return { data };
}

function makeEl(tag, className) {
  const node = document.createElement(tag);
  node.className = className;
  return node;
}

// 076: lint severity ('warn'/'info') → the canonical token colour class the category span wears.
// NOT severityColor() — that maps 'info'→kit-c-ok; the lint list wants muted for info.
function lintSeverityToken(severity) {
  return severity === 'warn' ? 'kit-c-warn' : 'kit-c-muted';
}

// A quadratic curve point: P0→control→P2 at t.
function qPoint(ax, ay, cx, cy, bx, by, t) {
  const u = 1 - t;
  return [u * u * ax + 2 * u * t * cx + t * t * bx,
    u * u * ay + 2 * u * t * cy + t * t * by];
}

export async function mount(host, ctx) {
  if (ctx.params?.view === 'lint') return mountLint(host, ctx);
  return mountMap(host, ctx);
}

// --- the OSPF lint window (053) — same snapshot, its own window, focus-on-select --------------

async function mountLint(host, ctx) {
  host.classList.add('ospf-lint-window');
  const list = makeEl('div', 'ospf-lint-list');
  const status = makeEl('div', 'kit-message'); // 076: shared muted-message box (was ospf-status)
  status.hidden = true;
  host.append(list, status);

  function render(topology) {
    list.replaceChildren();
    for (const finding of topology.lint) {
      const row = makeEl('button', 'ospf-lint-row');
      row.type = 'button';
      // 076: the category colour is a canonical token on the span (was a `.sev-*` row modifier).
      const cat = makeEl('span', `ospf-lint-cat ${lintSeverityToken(finding.severity)}`);
      cat.textContent = finding.category;
      const msg = makeEl('span', 'ospf-lint-msg');
      const node = topology.nodes.find((n) => n.router_id === finding.router_id);
      msg.textContent = `${node?.identity ?? finding.router_id}: ${finding.message}`;
      row.append(cat, msg);
      // Selecting a finding focuses the culprit on the map window (SC-006); the map listens for
      // this same-page event. No map open ⇒ the event is simply unheard.
      row.addEventListener('click', () => {
        document.dispatchEvent(new CustomEvent(FOCUS_EVENT, { detail: { id: finding.id } }));
      });
      list.appendChild(row);
    }
    const warn = topology.lint.filter((f) => f.severity === 'warn').length;
    const info = topology.lint.length - warn;
    const age = formatAge(topology.generated_at);
    ctx.setBadge?.([`${warn} warn`, `${info} info`, age.replace('data ', '')].filter(Boolean).join(' · '));
  }

  async function pollOnce() {
    try {
      const got = await fetchSnapshot();
      if (got.missing) { status.textContent = 'no OSPF snapshot yet'; status.hidden = false; return; }
      status.hidden = true;
      render(got.data);
    } catch (err) {
      status.textContent = `OSPF lint failed to load (${err.message})`;
      status.hidden = false;
    }
  }

  await pollOnce();
  const pollTimer = setInterval(pollOnce, POLL_MS);
  return {
    unmount() {
      clearInterval(pollTimer);
    },
  };
}

// --- the OSPF map ------------------------------------------------------------------------------

async function mountMap(host, ctx) {
  host.style.position = 'relative';
  host.style.overflow = 'hidden';

  const canvas = document.createElement('canvas');
  canvas.className = 'ospf-canvas';
  canvas.style.touchAction = 'none';
  host.appendChild(canvas);
  const c = canvas.getContext('2d');

  const legend = makeEl('div', 'ospf-legend');
  // 076: .ospf-toolbar kept for its canvas-overlay position:absolute/background; its buttons now
  // wear the shared .kit-btn (below). The status overlay becomes the shared .kit-message box.
  const toolbar = makeEl('div', 'ospf-toolbar');
  const defaultPanel = makeEl('div', 'ospf-default');
  // 076: the MAP status stays its own canvas-spanning overlay (.ospf-status) — its pointer-events:none
  // is a LOAD-BEARING 054 guard against dead-clicks (a real 053 field-pass bug), which the shared
  // .kit-message box does not carry. Only the lint-window status (in-flow) became .kit-message.
  const status = makeEl('div', 'ospf-status');
  status.hidden = true;
  host.append(legend, toolbar, defaultPanel, status);

  // 076: the node/edge/ghost detail is now the shared floating panel (kit.createPanel) — closing it
  // (✕ / Esc / empty-canvas click) deselects and redraws, exactly what closeDetail() used to do.
  const detail = createPanel(host, ctx, { onClose: () => { selected = null; draw(); } });

  // toolbar: SPF-root selector (053: re-rooting moved here — a node CLICK now opens the router's
  // drill-down window), the lint-window button, and a fit-view button.
  const rootLabel = makeEl('span', 'ospf-toolbar-label');
  rootLabel.textContent = 'SPF root';
  const rootSelect = makeEl('select', 'ospf-root');
  rootSelect.addEventListener('change', () => {
    if (topology?.spf?.[rootSelect.value]) currentRoot = rootSelect.value; // lookup, never computed
    renderDefault(); // 073: the default panel is per-root — re-render for the new vantage
    draw();
  });
  const lintButton = makeEl('button', 'kit-btn'); // 076: shared plain button (was ospf-lint-button)
  lintButton.type = 'button';
  lintButton.textContent = 'lint';
  lintButton.addEventListener('click', () => ctx.openOspfLint?.());
  // 073: the default-route panel is toggled by a `0.0.0.0/0` button; the legend by a `legend`
  // button placed immediately to the LEFT of `fit`. Both persist their state (saveUi).
  const defaultButton = makeEl('button', 'kit-btn'); // 076: shared plain button (was ospf-default-button)
  defaultButton.type = 'button';
  defaultButton.textContent = '0.0.0.0/0';
  defaultButton.title = 'toggle the default-route panel';
  defaultButton.addEventListener('click', () => {
    defaultVisible = !defaultVisible; renderDefault(); saveUi();
  });
  const legendButton = makeEl('button', 'kit-btn'); // 076: shared plain button (was ospf-legend-button)
  legendButton.type = 'button';
  legendButton.textContent = 'legend';
  legendButton.title = 'toggle the legend';
  legendButton.addEventListener('click', () => {
    legendVisible = !legendVisible; applyLegendVisibility(); saveUi();
  });
  const fitButton = makeEl('button', 'kit-btn'); // 076: shared plain button (was ospf-fit-button)
  fitButton.type = 'button';
  fitButton.textContent = 'fit';
  fitButton.addEventListener('click', () => { fitViewport(); draw(); });
  // 086: copy the EFFECTIVE arrangement (drags where present, else current seeds) as the
  // blessed-layout document — the operator saves it to [web] ospf_layout_path and every
  // browser boots with it (contract ospf-layout.md §5). Kit copy primitive (049).
  const exportButton = createCopyButton(() => layoutDocument(), {
    label: 'export layout',
    title: 'copy the current arrangement as the blessed-layout JSON ([web] ospf_layout_path)',
  });
  toolbar.append(
    rootLabel, rootSelect, lintButton, defaultButton, legendButton, fitButton, exportButton,
  );

  // --- state ---------------------------------------------------------------------------------
  let topology = null; // the snapshot `data`
  let positions = loadPositions(); // {router_id: {x, y}} dragged overrides
  let currentRoot = null; // the SPF root (a router_id)
  let legendVisible = false; // 073: legend starts hidden (FR-013), reclaiming canvas space
  let defaultVisible = false; // 073: default-route panel starts hidden (bottom-right), opened on demand
  let selected = null; // {kind: 'node'|'edge'|'ghost', id}
  let focusId = null; // a lint-focused element id (pulsed highlight)
  let refreshFailing = false;
  const view = { scale: 1, cx: 0, cy: 0 };
  const nodeById = new Map();
  let connections = []; // render list: formed edges + peer-resolved ghosts, with parallel offsets

  function worldOf(node) {
    const stored = positions[node.router_id];
    if (stored && Number.isFinite(stored.x) && Number.isFinite(stored.y)) return stored;
    return { x: node.seed.x, y: node.seed.y };
  }

  // 073 panel persistence: the show/hide state of the legend and default panels lives under the
  // reserved UI_KEY inside this widget's existing position store (no new sanctioned localStorage
  // owner). Absent/unreadable ⇒ both panels start hidden, each opened on demand from the toolbar.
  function loadUi() {
    const ui = positions[UI_KEY];
    legendVisible = !!(ui && ui.legend === true);
    defaultVisible = !!(ui && ui.default === true); // both panels start hidden, opened on demand
  }
  function saveUi() {
    positions[UI_KEY] = { legend: legendVisible, default: defaultVisible };
    savePositions(positions);
  }
  function applyLegendVisibility() { legend.hidden = !legendVisible; }

  // 073 internet cloud helpers. The default-originating ASBRs are the 0.0.0.0/0 external origins
  // that are known nodes; the cloud is drawn only when that set is non-empty.
  function defaultAsbrIds() {
    if (!topology) return [];
    const seen = new Set();
    const ids = [];
    for (const external of topology.externals) {
      if (external.prefix === '0.0.0.0/0' && nodeById.has(external.router_id)
          && !seen.has(external.router_id)) {
        seen.add(external.router_id);
        ids.push(external.router_id);
      }
    }
    return ids;
  }
  function cloudPresent() { return defaultAsbrIds().length > 0; }
  // The cloud's world position — precedence (086, contract ospf-layout.md §5): a local drag
  // (reserved CLOUD_KEY) → the snapshot's blessed cloud_seed → a deterministic default seed
  // (centroid-x of the default ASBRs, above their topmost node; world y is up).
  function cloudWorld() {
    const stored = positions[CLOUD_KEY];
    if (stored && Number.isFinite(stored.x) && Number.isFinite(stored.y)) return stored;
    const blessed = topology && topology.cloud_seed;
    if (blessed && Number.isFinite(blessed.x) && Number.isFinite(blessed.y)) return blessed;
    const ids = defaultAsbrIds();
    let sumX = 0, maxY = -Infinity, minY = Infinity;
    for (const id of ids) {
      const w = worldOf(nodeById.get(id));
      sumX += w.x;
      if (w.y > maxY) maxY = w.y;
      if (w.y < minY) minY = w.y;
    }
    const cx = ids.length ? sumX / ids.length : 0;
    const spanY = (maxY - minY) || 100;
    return { x: cx, y: maxY + Math.max(spanY * 0.4, 80) };
  }

  function worldToScreen(x, y) {
    return [host.clientWidth / 2 + (x - view.cx) * view.scale,
      host.clientHeight / 2 - (y - view.cy) * view.scale];
  }
  function screenToWorld(sx, sy) {
    return [view.cx + (sx - host.clientWidth / 2) / view.scale,
      view.cy - (sy - host.clientHeight / 2) / view.scale];
  }

  // 086: the blessed-layout document — every router's EFFECTIVE position (local drag if
  // present, else its current seed) plus the cloud's, in the position-store shape the
  // snapshot builder parses verbatim. Sorted keys (stable diffs), one-decimal coordinates,
  // and NEVER __ui__ (panel state is browser UI, not layout — contract ospf-layout.md §5).
  function layoutDocument() {
    if (!topology) return '{}';
    const doc = {};
    for (const node of topology.nodes) {
      const w = worldOf(node);
      doc[node.router_id] = { x: round1(w.x), y: round1(w.y) };
    }
    if (cloudPresent()) {
      const cw = cloudWorld();
      doc[CLOUD_KEY] = { x: round1(cw.x), y: round1(cw.y) };
    }
    const sorted = {};
    for (const key of Object.keys(doc).sort()) sorted[key] = doc[key];
    return JSON.stringify(sorted, null, 2);
  }

  function fitViewport() {
    if (!topology || !topology.nodes.length) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const node of topology.nodes) {
      const w = worldOf(node);
      minX = Math.min(minX, w.x); maxX = Math.max(maxX, w.x);
      minY = Math.min(minY, w.y); maxY = Math.max(maxY, w.y);
    }
    if (cloudPresent()) { // 073: the internet cloud counts toward the fit extent
      const cw = cloudWorld();
      minX = Math.min(minX, cw.x); maxX = Math.max(maxX, cw.x);
      minY = Math.min(minY, cw.y); maxY = Math.max(maxY, cw.y);
    }
    view.cx = (minX + maxX) / 2;
    view.cy = (minY + maxY) / 2;
    const spanX = Math.max(maxX - minX, 1);
    const spanY = Math.max(maxY - minY, 1);
    const scaleX = (host.clientWidth - 120) / spanX;
    const scaleY = (host.clientHeight - 120) / spanY;
    view.scale = Math.max(0.05, Math.min(scaleX, scaleY, 4));
  }

  // Group every router-pair connection (formed edges AND ghosts whose intended peer is a known
  // node) so parallel links between one pair fan out on distinct curve offsets (053).
  function rebuildConnections() {
    connections = [];
    const groups = new Map();
    const push = (kind, ref, a, b) => {
      const item = { kind, ref, a, b, level: 0 };
      connections.push(item);
      const key = [a, b].sort().join('|');
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    };
    for (const edge of topology.edges) {
      if (nodeById.has(edge.a) && nodeById.has(edge.b)) push('edge', edge, edge.a, edge.b);
    }
    for (const ghost of topology.ghosts) {
      if (ghost.peer_router_id && nodeById.has(ghost.peer_router_id) && nodeById.has(ghost.router_id)) {
        push('ghost', ghost, ghost.router_id, ghost.peer_router_id);
      }
    }
    for (const group of groups.values()) {
      group.forEach((item, index) => { item.level = index - (group.length - 1) / 2; });
    }
  }

  // The screen-space geometry of one connection: endpoints + quadratic control point offset
  // perpendicular to the chord by the connection's parallel level.
  function geometryOf(item) {
    const a = nodeById.get(item.a), b = nodeById.get(item.b);
    const wa = worldOf(a), wb = worldOf(b);
    const [ax, ay] = worldToScreen(wa.x, wa.y);
    const [bx, by] = worldToScreen(wb.x, wb.y);
    const dx = bx - ax, dy = by - ay;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len; // unit normal
    const bow = item.level * PARALLEL_GAP * 2; // control offset ⇒ curve peak ≈ level * gap
    const cx = (ax + bx) / 2 + nx * bow;
    const cy = (ay + by) / 2 + ny * bow;
    return { ax, ay, bx, by, cx, cy, nx, ny };
  }

  // --- rendering -----------------------------------------------------------------------------

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(host.clientWidth * ratio));
    canvas.height = Math.max(1, Math.round(host.clientHeight * ratio));
    canvas.style.width = `${host.clientWidth}px`;
    canvas.style.height = `${host.clientHeight}px`;
    c.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  function spfTree() {
    return (topology && currentRoot && topology.spf[currentRoot]) || {};
  }

  function edgeOnTree(edge, tree) {
    return tree[edge.a] === edge.b || tree[edge.b] === edge.a;
  }

  function strokeCurve(g, color, width) {
    c.strokeStyle = color; c.lineWidth = width;
    c.beginPath(); c.moveTo(g.ax, g.ay); c.quadraticCurveTo(g.cx, g.cy, g.bx, g.by); c.stroke();
  }

  // One side's label — the interface name with that side's outbound cost right next to it (053:
  // costs can differ per direction, so each end is labeled individually). 054: drawn ROTATED
  // along the curve's tangent (flipped to stay upright), so a label reads as part of its own
  // link instead of floating over its neighbors.
  function sideLabel(g, t, text, color) {
    if (!text) return;
    const [x, y] = qPoint(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, t);
    // quadratic tangent: B'(t) = 2(1-t)(C-P0) + 2t(P2-C)
    const u = 1 - t;
    const dx = 2 * u * (g.cx - g.ax) + 2 * t * (g.bx - g.cx);
    const dy = 2 * u * (g.cy - g.ay) + 2 * t * (g.by - g.cy);
    let angle = Math.atan2(dy, dx);
    if (angle > Math.PI / 2 || angle < -Math.PI / 2) angle += Math.PI; // never upside down
    c.save();
    c.translate(x, y);
    c.rotate(angle);
    c.font = '9px ui-monospace, monospace';
    c.fillStyle = color;
    c.textAlign = 'center';
    c.fillText(text, 0, -4); // just above the stroke in the label's own frame
    c.restore();
  }

  function labelFor(iface, cost) {
    const costText = cost == null ? '?' : String(cost);
    return iface ? `${iface} · ${costText}` : costText;
  }

  // 054 level-of-detail: short connections label costs only; the interface names join when the
  // link has room on screen (zoomed in) or when the connection is selected.
  function connectionLabels(item, g) {
    const chord = Math.hypot(g.bx - g.ax, g.by - g.ay);
    const isSelected = selected && selected.id === item.ref.id;
    return chord >= LABEL_FULL_PX || isSelected;
  }

  function draw() {
    if (!topology) return;
    const w = host.clientWidth, h = host.clientHeight;
    c.clearRect(0, 0, w, h);
    const tree = spfTree();

    for (const item of connections) {
      const g = geometryOf(item);
      if (item.kind === 'edge') {
        const edge = item.ref;
        const style = tierStyle(edge.tier);
        const onTree = edgeOnTree(edge, tree);
        // SPF emphasis WITHOUT opacity (073): tree = solid + thicker, backup = dashed. Full colour
        // both ways, so the on-screen stroke matches its legend swatch (no globalAlpha colour shift).
        if (edge.id === focusId) { c.setLineDash([]); strokeCurve(g, '#e6edf3', style.width + 4); }
        c.setLineDash(onTree ? [] : OFFTREE_DASH);
        strokeCurve(g, style.color, onTree ? style.width + 0.8 : style.width);
        c.setLineDash([]);
        if (edge.bridge) drawBridgeMarker(g); // SPOF link
        // per-side labels: interface + outbound cost at each end (LOD: costs only until the
        // link has screen room or is selected — 054)
        const full = connectionLabels(item, g);
        sideLabel(g, 0.22, full ? labelFor(edge.iface_a, edge.cost_ab)
          : String(edge.cost_ab ?? '?'), MUTED);
        sideLabel(g, 0.78, full ? labelFor(edge.iface_b, edge.cost_ba)
          : String(edge.cost_ba ?? '?'), MUTED);
      } else {
        const ghost = item.ref;
        const style = GHOST_STYLE[ghost.class] ?? GHOST_STYLE.unpaired;
        c.setLineDash(style.dash);
        c.globalAlpha = ghost.id === focusId ? 1 : 0.9;
        strokeCurve(g, style.color, style.width);
        c.setLineDash([]);
        c.globalAlpha = 1;
        sideLabel(g, 0.22, connectionLabels(item, g) ? labelFor(ghost.interface, ghost.cost)
          : String(ghost.cost ?? '?'), style.color);
      }
    }

    // unpaired ghosts: a short stub off the owner (no peer exists to connect to)
    for (const ghost of topology.ghosts) {
      const owner = nodeById.get(ghost.router_id);
      if (!owner) continue;
      if (ghost.peer_router_id && nodeById.has(ghost.peer_router_id)) continue; // drawn above
      const style = GHOST_STYLE[ghost.class] ?? GHOST_STYLE.unpaired;
      const [ox, oy] = worldToScreen(worldOf(owner).x, worldOf(owner).y);
      const [gx, gy] = stubEnd(owner, ghost);
      c.setLineDash(style.dash);
      c.globalAlpha = ghost.id === focusId ? 1 : 0.9;
      c.strokeStyle = style.color; c.lineWidth = style.width;
      c.beginPath(); c.moveTo(ox, oy); c.lineTo(gx, gy); c.stroke();
      c.setLineDash([]);
      c.globalAlpha = 1;
      c.font = '9px ui-monospace, monospace';
      c.fillStyle = style.color;
      c.textAlign = 'center';
      // LOD (054): a stub shows its cost; the interface name appears when selected (or in
      // the detail panel) — twelve always-on stub names were half the label soup.
      const stubText = selected && selected.id === ghost.id
        ? labelFor(ghost.interface, ghost.cost) : String(ghost.cost ?? '?');
      c.fillText(stubText, gx, gy - 4);
    }

    // pseudonodes (broadcast segments) as squares
    for (const pseudo of topology.pseudonodes) {
      const dr = nodeById.get(pseudo.dr);
      if (!dr) continue;
      const [x, y] = worldToScreen(worldOf(dr).x, worldOf(dr).y);
      c.fillStyle = PSEUDO_FILL;
      c.fillRect(x - 5, y - 5, 10, 10);
    }

    // 073 internet cloud + default-egress links (drawn under the nodes)
    drawCloudAndLinks();

    // nodes
    const placedLabels = []; // 054: greedy node-label declutter — flip below on overlap
    for (const node of topology.nodes) {
      const [x, y] = worldToScreen(worldOf(node).x, worldOf(node).y);
      if (node.spof) { // articulation warning ring
        c.beginPath(); c.arc(x, y, NODE_RADIUS + 4, 0, Math.PI * 2);
        c.strokeStyle = RING_SPOF; c.lineWidth = 2; c.stroke();
      }
      c.beginPath(); c.arc(x, y, NODE_RADIUS, 0, Math.PI * 2);
      c.fillStyle = node.router_id === currentRoot ? NODE_ROOT
        : node.enrichment === 'ok' ? NODE_OK : NODE_STALE; // stale/absent dimmer
      c.fill();
      if (selected && selected.kind === 'node' && selected.id === node.router_id) {
        c.strokeStyle = '#e6edf3'; c.lineWidth = 2; c.stroke();
      }
      // external-route badge on ASBRs
      if (node.is_asbr) {
        c.fillStyle = ASBR_DOT;
        c.beginPath(); c.arc(x + NODE_RADIUS, y - NODE_RADIUS, 3.5, 0, Math.PI * 2); c.fill();
      }
      // label: above-right by default; when that rectangle overlaps an already-placed label,
      // flip below the node (co-sited pairs stop garbling into each other — 054).
      c.font = '12px ui-monospace, monospace';
      c.textAlign = 'left';
      const label = node.identity ?? node.router_id;
      const width = c.measureText(label).width;
      const lx = x + NODE_RADIUS + 4;
      let ly = y - NODE_RADIUS - 2;
      let rect = { x0: lx, y0: ly - 12, x1: lx + width, y1: ly + 2 };
      const collides = (r) => placedLabels.some(
        (p) => r.x0 < p.x1 && r.x1 > p.x0 && r.y0 < p.y1 && r.y1 > p.y0);
      let flipped = false;
      if (collides(rect)) {
        ly = y + NODE_RADIUS + 12;
        rect = { x0: lx, y0: ly - 12, x1: lx + width, y1: ly + 2 };
        flipped = true;
      }
      placedLabels.push(rect);
      c.fillStyle = TEXT;
      c.fillText(label, lx, ly);
      if (node.lint_count > 0) {
        c.fillStyle = '#eab308';
        c.fillText(`⚠${node.lint_count}`, lx, flipped ? ly + 12 : y + NODE_RADIUS + 10);
      }
    }
  }

  // 073: one link per default-originating ASBR to the internet cloud; the link to the SELECTED
  // root's active egress is emphasized (solid/green), the fallbacks dashed/dim (standby).
  function drawCloudAndLinks() {
    const ids = defaultAsbrIds();
    if (!ids.length) return;
    const cw = cloudWorld();
    const [cx, cy] = worldToScreen(cw.x, cw.y);
    const perRoot = topology.default_by_root?.[currentRoot] ?? null;
    const activeId = perRoot?.active?.router_id ?? null;
    for (const id of ids) {
      const w = worldOf(nodeById.get(id));
      const [ax, ay] = worldToScreen(w.x, w.y);
      const active = id === activeId;
      const style = active ? DEFAULT_LINK_ACTIVE : DEFAULT_LINK_STANDBY;
      // active = green solid; standby = its own grey dashed. Full colour (no opacity — see
      // OFFTREE_DASH): the standby grey stays the grey the legend would show, not a blended tint.
      c.setLineDash(active ? [] : style.dash);
      c.strokeStyle = style.color; c.lineWidth = style.width;
      c.beginPath(); c.moveTo(ax, ay); c.lineTo(cx, cy); c.stroke();
    }
    c.setLineDash([]);
    drawCloudGlyph(cx, cy);
  }

  function drawCloudGlyph(x, y) {
    c.save();
    c.fillStyle = CLOUD_STYLE.fill;
    // a puffy cloud from four overlapping discs (fill unions into one silhouette)
    for (const [dx, dy, r] of [[-12, 2, 8], [-2, -5, 10], [10, -2, 8], [4, 5, 9]]) {
      c.beginPath(); c.arc(x + dx, y + dy, r, 0, Math.PI * 2); c.fill();
    }
    c.strokeStyle = CLOUD_STYLE.stroke; c.lineWidth = 1.4;
    c.beginPath(); c.ellipse(x, y, 20, 12, 0, 0, Math.PI * 2); c.stroke();
    c.fillStyle = CLOUD_STYLE.text; c.font = '9px ui-monospace, monospace'; c.textAlign = 'center';
    c.fillText('internet', x, y + 22);
    c.restore();
  }

  function drawBridgeMarker(g) {
    const [mx, my] = qPoint(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, 0.5);
    c.save(); c.setLineDash([]); c.strokeStyle = RING_SPOF; c.lineWidth = 2;
    c.beginPath();
    c.moveTo(mx + g.nx * 6, my + g.ny * 6);
    c.lineTo(mx - g.nx * 6, my - g.ny * 6);
    c.stroke(); c.restore();
  }

  function stubEnd(owner, ghost) {
    // a short stub off the owner, angle derived deterministically from the ghost id (no random)
    const [x, y] = worldToScreen(worldOf(owner).x, worldOf(owner).y);
    let hash = 0;
    for (const ch of ghost.id) hash = (hash * 31 + ch.charCodeAt(0)) & 0xffff;
    const angle = (hash / 0xffff) * Math.PI * 2;
    return [x + Math.cos(angle) * 46, y + Math.sin(angle) * 46];
  }

  // --- legend / toolbar / default-route panel --------------------------------------------------

  // 076: the aux overlays (legend, default-route) wear the shared panel head chrome — a title +
  // a ✕ that toggles the overlay off (the same state flip as its toolbar button; __ui__ persistence
  // untouched). No pop-out on these (D5): they are persistent toggles, not createPanel lifecycles.
  function overlayHead(titleText, onClose) {
    const head = makeEl('div', 'kit-panel-head');
    const heading = makeEl('span', 'kit-panel-title');
    heading.textContent = titleText;
    const close = makeEl('button', 'kit-panel-close');
    close.type = 'button';
    close.setAttribute('aria-label', 'close');
    close.textContent = '✕';
    close.addEventListener('click', onClose);
    head.append(heading, close);
    return head;
  }

  function closeLegend() { legendVisible = false; applyLegendVisibility(); saveUi(); }
  function closeDefaultPanel() { defaultVisible = false; renderDefault(); saveUi(); }

  function renderLegend() {
    legend.replaceChildren();
    legend.appendChild(overlayHead('legend', closeLegend));
    const title = makeEl('div', 'ospf-legend-title');
    title.textContent = 'cost';
    legend.appendChild(title);
    for (const key of [1, 100, 200, 300, 1000]) {
      const style = TIER_STYLE[key];
      const row = makeEl('div', 'ospf-legend-row');
      const swatch = makeEl('span', 'ospf-swatch');
      swatch.style.background = style.color;
      const text = makeEl('span', 'ospf-legend-label');
      text.textContent = style.label; // 053: the bare value — no commentary
      row.append(swatch, text);
      legend.appendChild(row);
    }
    for (const [cls, style] of Object.entries(GHOST_STYLE)) {
      const row = makeEl('div', 'ospf-legend-row');
      const swatch = makeEl('span', 'ospf-swatch ospf-swatch-dash');
      swatch.style.color = style.color; // dash draws in currentColor — set it to the ghost's colour
      const text = makeEl('span', 'ospf-legend-label');
      text.textContent = `ghost · ${cls}`;
      row.append(swatch, text);
      legend.appendChild(row);
    }
    // 073: the plain-language "what the map shows" block (FR-012) — every glyph named.
    const mapTitle = makeEl('div', 'ospf-legend-title');
    mapTitle.textContent = 'map';
    legend.appendChild(mapTitle);
    const shape = (swatchClass, color, label, extra) => {
      const row = makeEl('div', 'ospf-legend-row');
      const swatch = makeEl('span', `ospf-swatch ${swatchClass}`);
      if (color) swatch.style.background = color;
      if (extra) extra(swatch);
      const text = makeEl('span', 'ospf-legend-label');
      text.textContent = label;
      row.append(swatch, text);
      legend.appendChild(row);
    };
    shape('ospf-swatch-dot', NODE_ROOT, 'SPF root (selected vantage)');
    shape('ospf-swatch-dot', NODE_OK, 'router (data fresh)');
    shape('ospf-swatch-dot', NODE_STALE, 'router (data stale / absent)');
    shape('ospf-swatch-ring', null, 'SPOF — removing it splits the network',
      (sw) => { sw.style.borderColor = RING_SPOF; });
    shape('ospf-swatch-dot', ASBR_DOT, 'ASBR — injects external routes');
    shape('ospf-swatch', RING_SPOF, 'SPOF link — its loss splits the network');
    shape('ospf-swatch-square', PSEUDO_FILL, 'broadcast segment');
    shape('ospf-swatch-dot', CLOUD_STYLE.stroke, 'internet — links show default (0.0.0.0/0) egress');
    shape('ospf-swatch', DEFAULT_LINK_ACTIVE.color, "this router's active default egress");
    // 073: the dash is now the "backup" cue (links keep their true colour; no opacity dimming).
    shape('ospf-swatch-dash', null, "dashed = backup (off this root's shortest path)",
      (sw) => { sw.style.color = MUTED; });
  }

  function renderToolbar() {
    rootSelect.replaceChildren();
    const nodes = [...topology.nodes].sort((a, b) =>
      (a.identity ?? a.router_id).localeCompare(b.identity ?? b.router_id));
    for (const node of nodes) {
      const option = document.createElement('option');
      option.value = node.router_id;
      option.textContent = node.identity ?? node.router_id;
      rootSelect.appendChild(option);
    }
    if (currentRoot) rootSelect.value = currentRoot;
    lintButton.textContent = `lint (${topology.lint.length})`;
  }

  // 073: the default-route panel is now PER-ROOT (from the selected SPF root's point of view) and
  // structured — one route per line (active, then each standby), not a run-on paragraph. Toggled
  // by the `0.0.0.0/0` button; the network-wide conflict flag is still surfaced.
  function renderDefault() {
    defaultPanel.replaceChildren();
    if (!defaultVisible || !topology) { defaultPanel.hidden = true; return; }
    const conflict = topology.default_route?.conflict === true;
    const rootName = nodeById.get(currentRoot)?.identity ?? currentRoot ?? '?';
    defaultPanel.hidden = false;
    defaultPanel.classList.toggle('conflict', conflict);
    defaultPanel.appendChild(overlayHead(
      conflict ? '⚠ default route — CONFLICT' : `default route · from ${rootName}`,
      closeDefaultPanel,
    ));

    const line = (text, cls) => {
      const div = makeEl('div', `ospf-default-line${cls ? ' ' + cls : ''}`);
      div.textContent = text;
      defaultPanel.appendChild(div);
    };
    const egressText = (choice) =>
      `${choice.identity ?? choice.router_id} (E${choice.metric_type}, metric ${choice.metric ?? '?'})`;

    const hasAnyDefault = topology.externals.some((e) => e.prefix === '0.0.0.0/0');
    const perRoot = topology.default_by_root?.[currentRoot] ?? null;
    if (!hasAnyDefault) {
      line('No router originates a default route.');
      return;
    }
    if (!perRoot || !perRoot.active) {
      line(`No default route reachable from ${rootName}.`);
      return;
    }
    line(`Default: ${egressText(perRoot.active)}`, 'active');
    perRoot.standby.forEach((choice, index) => {
      line(`${index === 0 ? 'Next candidate' : 'Then'}: ${egressText(choice)}`, 'standby');
    });
    if (conflict) {
      line('WARNING: two routers originate a same-type default simultaneously.', 'warn');
    }
  }

  // Focus a lint finding: center its subject element and pulse a highlight (SC-006). Reached
  // from the lint WINDOW via the same-page focus event (053) — the map no longer hosts the list.
  function focusFinding(finding) {
    focusId = finding.edge_id ?? finding.ghost_id ?? finding.router_id;
    let target = null;
    if (finding.edge_id) {
      const edge = topology.edges.find((e) => e.id === finding.edge_id);
      if (edge) target = midWorld(edge.a, edge.b);
      selected = { kind: 'edge', id: finding.edge_id };
    } else if (finding.ghost_id) {
      const ghost = topology.ghosts.find((g) => g.id === finding.ghost_id);
      if (ghost) {
        const owner = nodeById.get(ghost.router_id);
        const peer = ghost.peer_router_id && nodeById.get(ghost.peer_router_id);
        if (owner && peer) target = midWorld(ghost.router_id, ghost.peer_router_id);
        else if (owner) target = worldOf(owner);
        showDetailForGhost(ghost);
      }
    } else {
      const node = nodeById.get(finding.router_id);
      if (node) { target = worldOf(node); selected = { kind: 'node', id: node.router_id }; showDetailForNode(node); }
    }
    if (finding.edge_id) showDetailForFinding(finding);
    if (target) { view.cx = target.x; view.cy = target.y; }
    draw();
    setTimeout(() => { focusId = null; draw(); }, 1600); // brief pulse
  }

  function onFocusEvent(event) {
    const id = event.detail?.id;
    if (!id || !topology) return;
    const finding = topology.lint.find((f) => f.id === id);
    if (finding) focusFinding(finding);
  }
  document.addEventListener(FOCUS_EVENT, onFocusEvent);

  function midWorld(aId, bId) {
    const wa = worldOf(nodeById.get(aId)), wb = worldOf(nodeById.get(bId));
    return { x: (wa.x + wb.x) / 2, y: (wa.y + wb.y) / 2 };
  }

  // --- detail panel --------------------------------------------------------------------------

  function row(label, value) {
    const div = makeEl('div', 'kit-facts'); // 076: shared fact-pair container (was ospf-row)
    const k = makeEl('span', 'k'); k.textContent = label;
    const v = makeEl('span', 'v'); v.textContent = value;
    div.append(k, v);
    return div;
  }

  function showDetailForNode(node) {
    const body = makeEl('div', 'ospf-detail-body');
    body.appendChild(row('router-id', node.router_id));
    if (node.address) body.appendChild(row('address', node.address));
    body.appendChild(row('enrichment', node.enrichment));
    if (node.is_asbr) body.appendChild(row('ASBR', 'originates external routes'));
    if (node.spof) body.appendChild(row('SPOF', 'articulation point'));
    for (const ext of topology.externals.filter((e) => e.router_id === node.router_id)) {
      body.appendChild(row('external', `${ext.prefix} E${ext.metric_type}/${ext.metric}`));
    }
    // 076 US2: the panel's pop-out IS the drill-down (one affordance, not two) — the old in-body
    // ospf-drill button was retired. A node has a live window kind, so it pops out to ospf:<device>.
    detail.open({
      title: node.identity ?? node.router_id,
      body,
      popOut: { open: () => ctx.openOspf?.(node.identity ?? node.router_id) },
    });
  }

  function showDetailForEdge(edge) {
    const a = nodeById.get(edge.a), b = nodeById.get(edge.b);
    const body = makeEl('div', 'ospf-detail-body');
    body.appendChild(row(`cost → (${edge.iface_a ?? '?'})`, String(edge.cost_ab ?? '?')));
    body.appendChild(row(`cost ← (${edge.iface_b ?? '?'})`, String(edge.cost_ba ?? '?')));
    if (!edge.symmetric) body.appendChild(row('asymmetric', 'yes'));
    if (edge.neighbor_state) body.appendChild(row('state', edge.neighbor_state));
    if (edge.adjacency_age) body.appendChild(row('uptime', edge.adjacency_age));
    if (edge.shared_fate_group) body.appendChild(row('shared fate', edge.shared_fate_group));
    if (edge.bridge) body.appendChild(row('SPOF link', 'a single point of failure'));
    detail.open({ title: `${a?.identity ?? edge.a} ↔ ${b?.identity ?? edge.b}`, body });
  }

  function showDetailForGhost(ghost) {
    const owner = nodeById.get(ghost.router_id);
    const peer = ghost.peer_router_id && nodeById.get(ghost.peer_router_id);
    const body = makeEl('div', 'ospf-detail-body');
    body.appendChild(row('ghost', ghost.class));
    body.appendChild(row('interface', ghost.interface));
    if (ghost.cost != null) body.appendChild(row('configured cost', String(ghost.cost)));
    const why = makeEl('div', 'ospf-default-body');
    why.textContent = ghost.detail;
    body.appendChild(why);
    detail.open({ title: peer
      ? `${owner?.identity ?? ghost.router_id} ⇢ ${peer.identity ?? ghost.peer_router_id}`
      : `${owner?.identity ?? ghost.router_id} ⇢ ?`, body });
  }

  function showDetailForFinding(finding) {
    if (finding.edge_id) {
      const edge = topology.edges.find((e) => e.id === finding.edge_id);
      if (edge) showDetailForEdge(edge);
    } else {
      const node = nodeById.get(finding.router_id);
      if (node) showDetailForNode(node);
    }
  }

  // 076: the panel owns dismissal; its onClose clears the selection + redraws. A no-op when the
  // panel is already closed (an empty-canvas click with nothing selected — was harmless before too).
  function closeDetail() { detail.close(); }

  // --- interaction: pan / pinch / zoom / node drag / click-to-open / edge select ---------------

  let drag = null; // {node} while dragging a node | {pan} while panning
  let moved = false;
  const pointers = new Map(); // active pointers (two ⇒ pinch zoom)
  let pinchStart = null;

  function nodeAt(sx, sy) {
    for (const node of topology.nodes) {
      const [x, y] = worldToScreen(worldOf(node).x, worldOf(node).y);
      if (Math.hypot(sx - x, sy - y) <= NODE_RADIUS + 6) return node;
    }
    return null;
  }

  // 073: the internet cloud is draggable like a node, but is never a router (no drill-down/re-root).
  function cloudAt(sx, sy) {
    if (!cloudPresent()) return false;
    const cw = cloudWorld();
    const [cx, cy] = worldToScreen(cw.x, cw.y);
    return Math.hypot(sx - cx, sy - cy) <= CLOUD_STYLE.radius + 4;
  }

  // Hit-test connections by sampling each curve (parallel links are curved, so a straight
  // point-to-segment test would miss them).
  function connectionAt(sx, sy) {
    let best = null, bestDist = 9;
    for (const item of connections) {
      const g = geometryOf(item);
      let [px, py] = [g.ax, g.ay];
      for (let step = 1; step <= 24; step += 1) {
        const [qx, qy] = qPoint(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, step / 24);
        const d = distanceToSegment(sx, sy, px, py, qx, qy);
        if (d < bestDist) { bestDist = d; best = item; }
        [px, py] = [qx, qy];
      }
    }
    return best;
  }

  function distanceToSegment(px, py, ax, ay, bx, by) {
    const dx = bx - ax, dy = by - ay;
    const lengthSq = dx * dx + dy * dy;
    const t = lengthSq === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSq));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
    if (pointers.size === 2) {
      const [p1, p2] = [...pointers.values()];
      pinchStart = { dist: Math.hypot(p1[0] - p2[0], p1[1] - p2[1]), scale: view.scale };
      drag = null;
      return;
    }
    moved = false;
    const node = nodeAt(e.offsetX, e.offsetY);
    if (node) drag = { node, start: [e.offsetX, e.offsetY] };
    else if (cloudAt(e.offsetX, e.offsetY)) drag = { cloud: true, start: [e.offsetX, e.offsetY] };
    else drag = { pan: true, start: [e.offsetX, e.offsetY], cx: view.cx, cy: view.cy };
  });

  canvas.addEventListener('pointermove', (e) => {
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
    if (pointers.size === 2 && pinchStart) {
      const [p1, p2] = [...pointers.values()];
      const dist = Math.hypot(p1[0] - p2[0], p1[1] - p2[1]);
      if (dist > 0 && pinchStart.dist > 0) {
        view.scale = Math.max(0.02, Math.min(pinchStart.scale * (dist / pinchStart.dist), 8));
        draw();
      }
      return;
    }
    if (!drag) return;
    const dx = e.offsetX - drag.start[0], dy = e.offsetY - drag.start[1];
    if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
    if (drag.node) {
      const [wx, wy] = screenToWorld(e.offsetX, e.offsetY);
      positions[drag.node.router_id] = { x: round1(wx), y: round1(wy) };
      draw();
    } else if (drag.cloud) {
      const [wx, wy] = screenToWorld(e.offsetX, e.offsetY);
      positions[CLOUD_KEY] = { x: round1(wx), y: round1(wy) };
      draw();
    } else if (drag.pan) {
      view.cx = drag.cx - dx / view.scale;
      view.cy = drag.cy + dy / view.scale;
      draw();
    }
  });

  function releasePointer(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinchStart = null;
  }

  canvas.addEventListener('pointerup', (e) => {
    releasePointer(e);
    if (drag && drag.node) {
      if (moved) {
        // persist ONLY the dragged nodes; drop stale ids on save (validated against the snapshot)
        savePositions(prunePositions());
      } else {
        // 053: a click opens the router's OSPF drill-down window (re-rooting moved to the
        // toolbar's SPF-root selector).
        selected = { kind: 'node', id: drag.node.router_id };
        draw();
        ctx.openOspf?.(drag.node.identity ?? drag.node.router_id);
      }
    } else if (drag && drag.cloud) {
      if (moved) savePositions(prunePositions()); // persist the dragged cloud (reserved key)
    } else if (drag && drag.pan && !moved) {
      const hit = connectionAt(e.offsetX, e.offsetY);
      if (hit && hit.kind === 'edge') { selected = { kind: 'edge', id: hit.ref.id }; showDetailForEdge(hit.ref); draw(); }
      else if (hit && hit.kind === 'ghost') { selected = { kind: 'ghost', id: hit.ref.id }; showDetailForGhost(hit.ref); draw(); }
      else closeDetail();
    }
    drag = null;
  });
  canvas.addEventListener('pointercancel', (e) => { releasePointer(e); drag = null; });

  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0015);
    const [wx, wy] = screenToWorld(e.offsetX, e.offsetY);
    view.scale = Math.max(0.02, Math.min(view.scale * factor, 8));
    view.cx = wx - (e.offsetX - host.clientWidth / 2) / view.scale;
    view.cy = wy + (e.offsetY - host.clientHeight / 2) / view.scale;
    draw();
  }, { passive: false });

  function prunePositions() {
    const valid = {};
    for (const key of Object.keys(positions)) {
      // reserved keys (cloud position, UI state — 073) are not router ids; always keep them.
      if (key.startsWith('__')) { valid[key] = positions[key]; continue; }
      if (nodeById.has(key)) valid[key] = positions[key];
    }
    positions = valid;
    return valid;
  }
  function round1(n) { return Math.round(n * 10) / 10; }

  // --- data lifecycle ------------------------------------------------------------------------

  function rebuild(fresh) {
    const firstLoad = topology === null;
    topology = fresh;
    nodeById.clear();
    for (const node of topology.nodes) nodeById.set(node.router_id, node);
    // default root = the snapshot's primary source router (by identity), else the first node
    if (currentRoot === null || !nodeById.has(currentRoot)) {
      const source = topology.nodes.find((n) => n.identity === topology.meta.source_router);
      currentRoot = source?.router_id ?? topology.nodes[0]?.router_id ?? null;
    }
    positions = prunePositions(); // ignore stored ids no longer in the snapshot
    loadUi(); // restore persisted legend/default panel visibility (073)
    rebuildConnections();
    if (firstLoad) fitViewport();
    renderLegend();
    applyLegendVisibility();
    renderToolbar();
    renderDefault();
    status.hidden = true;
    draw();
    updateBadge();
  }

  function updateBadge() {
    if (!topology) { ctx.setBadge?.(null); return; }
    const parts = [`${topology.nodes.length} routers`, `${topology.lint.length} lint`];
    const age = formatAge(topology.generated_at ?? topology.meta?.generated_at);
    if (age) parts.push(age.replace('data ', ''));
    if (refreshFailing) parts.push('refresh failing');
    ctx.setBadge?.(parts.join(' · '));
  }

  async function pollOnce() {
    try {
      const got = await fetchSnapshot();
      if (got.missing) {
        if (topology === null) showStatus('no OSPF snapshot yet — run: obi web snapshot (cron)');
        return;
      }
      refreshFailing = false;
      rebuild(got.data);
    } catch (err) {
      refreshFailing = true;
      if (topology === null) showStatus(`OSPF map failed to load (${err.message}) — retrying every 5 min`);
      updateBadge();
    }
  }

  function showStatus(text) {
    status.textContent = text;
    status.hidden = false;
  }

  const observer = new ResizeObserver(resize);
  observer.observe(host);
  resize();
  await pollOnce();
  const pollTimer = setInterval(pollOnce, POLL_MS);
  const ageTimer = setInterval(updateBadge, 30 * 1000);

  return {
    unmount() {
      observer.disconnect();
      clearInterval(pollTimer);
      clearInterval(ageTimer);
      document.removeEventListener(FOCUS_EVENT, onFocusEvent);
      detail.destroy();
      canvas.remove();
    },
  };
}
