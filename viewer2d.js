// obi 2D viewer (feature 033) — the flat-browser version of the network view.
//
// Thin adapter, same discipline as viewer.js: NO geodesy and NO physics here. Every number
// this module draws is precomputed by `obi web export` — positions come from the export's
// per-site ENU dictionaries (east/north meters relative to an anchor site), health and
// clearance verdicts arrive as fields, and the elevation-profile chart renders precomputed
// terrain samples, endpoint AMSL heights, and Fresnel radii. The Fresnel envelope drawn in
// the chart is the 2D cross-section of the exact ellipsoid the 3D viewer scales to
// (D/2, r, r): half-height r·2√(t(1−t)). Plain Canvas 2D; zero dependencies.

import {
  bandOf, bandsPresent, capacityLine, formatRate, HEALTH_COLORS, lineWidth2d, linkColor, linkKey,
  loadHiddenBands, loadTerrainOn, MAINTENANCE_COLOR, saveHiddenBands, saveTerrainOn, usageFraction,
  usagePercent,
} from './bands.js';
import { createPanel } from './kit.js';
import { formatAge } from './boards.js'; // 076: the widgets' shared age vocabulary (was a local copy)

const POLL_MS = 5 * 60 * 1000; // the 3D viewer's cadence (022) — one contract, two versions

// 055 US1: parallel links between the same site pair fan out on symmetric screen-space curves
// (the 052/054 OSPF-map treatment). The gap is in CSS px, not meters, so the separation survives
// any zoom — a metric gap vanishes at kilometer scale (research R7). 057: this is the EDGE gap;
// each group's level spacing adds its thickest member's stroke width on top.
const GAP_PX = 14;
// 055 US2: the utilization fill — the theme's light ink at half alpha, reading as "pipe fill"
// over every health color (research R8). One inner line, worst-direction fraction wide.
const USAGE_FILL = 'rgba(230, 237, 243, 0.5)';
// 055 US4: the terrain toggle persists per surface, this key never crosses to viewer.js.
const TERRAIN_KEY = 'obi.viewer2d.terrain';
const SHADE_ALPHA = 0.5; // the hillshade underlay's fixed alpha over the dark background
const PEAK_COLOR = '#7d8a99'; // muted summit markers that never compete with site labels

// Health palette + linkColor + MAINTENANCE_COLOR now live in bands.js (063), imported above — the
// same colors the 3D scene derives its numeric form from, so the two surfaces cannot drift.

// Band visibility persists per surface (044 FR-005) — this key never crosses to viewer.js.
const BANDS_KEY = 'obi.viewer2d.bands';

const NO_EXPORT_HELP =
  'No link data yet.\n' +
  'The export could not be loaded — run: obi web export\n' +
  'This page retries automatically every 5 minutes (or reload).';

// --- pure helpers -----------------------------------------------------------------------------

function distanceToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const lengthSq = dx * dx + dy * dy;
  const t = lengthSq === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSq));
  const cx = ax + t * dx, cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

// A quadratic curve point P0→control→P2 at t (055 US1 — the OSPF map's qPoint, ported).
function qPoint(ax, ay, cx, cy, bx, by, t) {
  const u = 1 - t;
  return [u * u * ax + 2 * u * t * cx + t * t * bx,
    u * u * ay + 2 * u * t * cy + t * t * by];
}

function clearanceFlagged(link) {
  return link.clearance_verdict === 'marginal' || link.clearance_verdict === 'obstructed';
}

// 076: the map's freshness readout now speaks the widgets' shared age vocabulary (boards.formatAge:
// '<1m'/'Nm'/'Nh'/'age ?') instead of a local "data N min old" copy — one age wording across surfaces.

// --- the viewer -------------------------------------------------------------------------------

export async function start2D(host, { ageTarget, openLink } = {}) {
  // 058: `openLink` (when the host is a workspace window) opens the clicked link in a real LINK
  // window instead of the in-canvas panel; the full-screen `?w=map` page passes none, so it keeps
  // the panel as its fallback. The canvas selection highlight behaves the same either way.
  // DOM scaffold: full-host canvas + status box + detail panel (styles in theme.css).
  // 037: hosted as the MAP widget (or the ?w=map page) — sizing follows the HOST element
  // via ResizeObserver, and the returned handle's dispose() releases everything, so a
  // window close/reopen cycle leaks nothing. Voice moved to the TALK widget.
  const canvas = document.createElement('canvas');
  canvas.id = 'canvas2d';
  canvas.style.touchAction = 'none'; // pan/pinch are ours, not the browser's
  host.appendChild(canvas);
  const status = document.createElement('div');
  status.id = 'status2d';
  status.hidden = true;
  host.appendChild(status);
  // 076: the in-canvas link detail is the shared floating panel (kit.createPanel). Only the
  // full-screen ?w=map page reaches it — a workspace MAP opens the link's own window via openLink
  // (D6) — so there is no ctx here (fullscreen has no window manager) and no pop-out button renders
  // (FR-010). Its onClose deselects the link + redraws, exactly what closeDetail() used to do.
  const panel = createPanel(host, null, { onClose: () => { selectedId = null; draw(); } });
  const chips = document.createElement('div');
  chips.id = 'bands2d';
  chips.hidden = true;
  host.appendChild(chips);
  // 057: map controls live top-right (operator request), out of the band row entirely;
  // the detail panel sits below them (theme.css) so an open panel never hides fit.
  const controls = document.createElement('div');
  controls.id = 'controls2d';
  controls.hidden = true;
  host.appendChild(controls);

  const ctx = canvas.getContext('2d');

  // Band visibility (044 US1): hidden bands are neither drawn nor hit-testable; the chip
  // row offers exactly the bands present in the current export (contracts/viewer-bands.md).
  const hiddenBands = loadHiddenBands(BANDS_KEY);

  // 057: the control chips (top-right #controls2d). `fit` is always present once geometry
  // loads (055 US3); `terrain` only when a usable shade artifact + a rect for the current
  // anchor exist (055 US4/FR-011). Band chips stay in their own bottom-left row (044).
  function rebuildControls() {
    controls.replaceChildren();
    if (geometry) {
      const fit = document.createElement('button');
      fit.type = 'button';
      fit.className = 'chip';
      fit.textContent = 'fit';
      fit.addEventListener('click', () => { fitViewport(); draw(); }); // one-tap re-frame
      controls.appendChild(fit);
      if (terrainAvailable()) {
        const terr = document.createElement('button');
        terr.type = 'button';
        terr.className = 'chip';
        terr.textContent = 'terrain';
        terr.classList.toggle('off', !terrainOn);
        terr.addEventListener('click', () => {
          terrainOn = !terrainOn;
          saveTerrainOn(TERRAIN_KEY, terrainOn);
          terr.classList.toggle('off', !terrainOn);
          if (terrainOn) ensureShadeImage();
          draw();
        });
        controls.appendChild(terr);
      }
    }
    controls.hidden = controls.children.length === 0;
  }

  function rebuildChips() {
    const present = geometry ? bandsPresent(geometry.links) : [];
    chips.replaceChildren();
    for (const band of present) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.textContent = band;
      chip.classList.toggle('off', hiddenBands.has(band));
      chip.addEventListener('click', () => {
        if (hiddenBands.has(band)) hiddenBands.delete(band);
        else hiddenBands.add(band);
        saveHiddenBands(BANDS_KEY, hiddenBands);
        chip.classList.toggle('off', hiddenBands.has(band));
        const selected = geometry.links.find((l) => l.id === selectedId);
        if (selected && hiddenBands.has(bandOf(selected))) closeDetail();
        draw();
      });
      chips.appendChild(chip);
    }
    chips.hidden = chips.children.length === 0;
  }

  // State: the current export, the once-fetched terrain, the viewport, the selection.
  let geometry = null;
  let terrain = null; // {profiles: {linkId: {samples_amsl}}, shade: {...}, ...} or null
  let anchorName = null; // ENU anchor site — stable across rebuilds when it still exists
  let refreshFailing = false;
  const view = { scale: 0, cx: 0, cy: 0 }; // px per meter; ENU meters at canvas center
  let selectedId = null;
  let endpointSiteMap = new Map(); // endpoint name → site (055 US1: parallel grouping key)

  // 055 US4: terrain underlay state. The preference persists per-surface through bands.js (the
  // sanctioned storage owner — the widget never touches the browser store directly); the shade
  // image is fetched lazily, at most once per load, only after the first enable.
  let terrainOn = loadTerrainOn(TERRAIN_KEY);
  let shadeImage = null;
  let shadeLoadState = 'idle'; // 'idle' | 'loading' | 'ready' | 'failed'

  // The shade image's rectangle in the current anchor's ENU frame, or null (no underlay for
  // this anchor). (e_min, n_min, e_max, n_max); row 0 of the image is the NORTH edge (n_max).
  function anchorRect() {
    const site = anchorSite();
    const rect = site && terrain?.shade?.sites ? terrain.shade.sites[site.name] : null;
    return Array.isArray(rect) && rect.length === 4 ? rect : null;
  }

  // The terrain toggle exists only when a shade artifact AND a rect for this anchor exist
  // (FR-011). Image fetchability is discovered lazily on first enable.
  function terrainAvailable() {
    return anchorRect() !== null;
  }

  function ensureShadeImage() {
    if (shadeLoadState !== 'idle') return;
    shadeLoadState = 'loading';
    shadeImage = new Image();
    shadeImage.onload = () => { shadeLoadState = 'ready'; draw(); };
    shadeImage.onerror = () => {
      shadeLoadState = 'failed';
      terrainOn = false; // Session-only. The eager up-front fetch can 404 on a deployment that
      rebuildControls(); // simply has no terrain artifact yet, so a load failure must NOT persist
      draw();            // an opt-out the operator never chose (a reload retries).
    };
    shadeImage.src = 'terrain-shade.png'; // one fetch per page load, never inside the poll
  }

  function showStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle('error', Boolean(isError));
    status.hidden = false;
  }

  function anchorSite() {
    if (!geometry) return null;
    return geometry.sites.find((s) => s.name === anchorName) ?? geometry.sites[0] ?? null;
  }

  function updateAge() {
    if (!ageTarget) return;
    if (!geometry) {
      ageTarget.textContent = '';
      return;
    }
    ageTarget.textContent =
      formatAge(geometry.generated_at) + (refreshFailing ? ' · refresh failing' : '');
  }

  // --- viewport ------------------------------------------------------------------------------

  function resizeCanvas() {
    const ratio = window.devicePixelRatio || 1;
    const { clientWidth, clientHeight } = host;
    canvas.width = Math.max(1, Math.round(clientWidth * ratio));
    canvas.height = Math.max(1, Math.round(clientHeight * ratio));
    canvas.style.width = `${clientWidth}px`;
    canvas.style.height = `${clientHeight}px`;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  // 057: fit frames the CONTENT bounding box, per axis, centered. The original fit framed a
  // square of max |e|,|n| around the anchor ORIGIN — content asymmetric around the anchor
  // (all of it north, say) left half the window blank and the network off-center.
  const FIT_PAD_PX = 44; // clears the control/band chip rows and the HUD on every side
  function fitViewport() {
    const site = anchorSite();
    if (!site) return;
    const points = Object.values(site.enu);
    if (points.length === 0) return;
    let eMin = Infinity, eMax = -Infinity, nMin = Infinity, nMax = -Infinity;
    for (const [e, n] of points) {
      eMin = Math.min(eMin, e); eMax = Math.max(eMax, e);
      nMin = Math.min(nMin, n); nMax = Math.max(nMax, n);
    }
    const spanE = Math.max(eMax - eMin, 200); // a one-site export still gets a sane scale
    const spanN = Math.max(nMax - nMin, 200);
    view.scale = Math.min(
      (host.clientWidth - 2 * FIT_PAD_PX) / spanE,
      (host.clientHeight - 2 * FIT_PAD_PX) / spanN,
    );
    view.cx = (eMin + eMax) / 2;
    view.cy = (nMin + nMax) / 2;
  }

  function worldToScreen([e, n]) {
    return [
      host.clientWidth / 2 + (e - view.cx) * view.scale,
      host.clientHeight / 2 - (n - view.cy) * view.scale,
    ];
  }

  function screenToWorld(x, y) {
    return [
      view.cx + (x - host.clientWidth / 2) / view.scale,
      view.cy - (y - host.clientHeight / 2) / view.scale,
    ];
  }

  // --- rendering -------------------------------------------------------------------------------

  // 055 US1: parallel levels over the VISIBLE links, grouped by unordered SITE pair (co-sited
  // radios share coordinates, so the site pair is the overlap unit at map scale). Member i of a
  // group of n takes level i-(n-1)/2, scaled to a pixel offset by the group's width-aware
  // spacing (057); a lone member is offset 0 (straight). draw() and the hit-test each filter
  // hidden bands and read these shared offsets (recomputed each draw).
  function levelsByLinkId() {
    const site = anchorSite();
    const enu = site ? site.enu : {};
    const groups = new Map();
    for (const link of geometry.links) {
      if (hiddenBands.has(bandOf(link))) continue; // hidden bands don't participate (044)
      if (!enu[link.a] || !enu[link.b]) continue;
      const sa = endpointSiteMap.get(link.a) ?? link.a;
      const sb = endpointSiteMap.get(link.b) ?? link.b;
      const key = [sa, sb].sort().join('\u0000');
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(link);
    }
    const levels = new Map();
    for (const members of groups.values()) {
      members.sort((a, b) => String(a.id).localeCompare(String(b.id)));
      // 057: level spacing grows with the thickest member — capacity-scaled strokes must
      // separate by EDGE gap, not center distance (the 60/70 GHz Showers↔Mountain pair sat
      // 14 px apart center-to-center under ~6 px strokes and read as one line).
      const spacing = GAP_PX + Math.max(...members.map((m) => lineWidth2d(m)));
      members.forEach((link, i) => {
        const level = i - (members.length - 1) / 2;
        levels.set(link.id, level * spacing);
      });
    }
    return levels;
  }

  // Screen-space curve geometry for one link at a parallel offset: a quadratic whose control
  // point is the chord midpoint offset along the screen normal by 2× the level offset (the
  // curve's apex then sits ~offset from the chord); a zero offset is a straight line.
  // The bow side is CANONICAL to the chord, not to the link's own a→b order: the live 60/70 GHz
  // Showers↔Mountain pair stores its endpoints in opposite order, which flipped one member's
  // normal and made symmetric ±levels bow to the SAME side — an exact overlap (057 hotfix).
  function curveFor(enu, link, offset) {
    const [ax, ay] = worldToScreen(enu[link.a]);
    const [bx, by] = worldToScreen(enu[link.b]);
    const dx = bx - ax, dy = by - ay;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len; // unit normal (a→b handed)
    const flip = bx < ax || (bx === ax && by < ay) ? -1 : 1; // canonical chord orientation
    const bow = offset * 2 * flip; // control offset; curve peak ~ the level offset itself
    return {
      ax, ay, bx, by,
      cx: (ax + bx) / 2 + nx * bow,
      cy: (ay + by) / 2 + ny * bow,
      curved: bow !== 0,
    };
  }

  function tracePath(g) {
    ctx.beginPath();
    ctx.moveTo(g.ax, g.ay);
    if (g.curved) ctx.quadraticCurveTo(g.cx, g.cy, g.bx, g.by);
    else ctx.lineTo(g.bx, g.by); // single-member groups stay straight (exactly as today)
  }

  function apexOf(g) {
    return g.curved
      ? qPoint(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, 0.5)
      : [(g.ax + g.bx) / 2, (g.ay + g.by) / 2];
  }

  // 055 US4: the hillshade underlay + named peaks, drawn FIRST (beneath everything). Its failure
  // is caught locally and may NEVER prevent the link layer from drawing (FR-012).
  function drawTerrainUnderlay() {
    const rect = anchorRect();
    if (!rect || shadeLoadState !== 'ready' || !shadeImage) return;
    try {
      const [x0, y0] = worldToScreen([rect[0], rect[3]]); // west edge, north edge (image row 0)
      ctx.save();
      ctx.globalAlpha = SHADE_ALPHA;
      ctx.drawImage(
        shadeImage, x0, y0,
        (rect[2] - rect[0]) * view.scale, (rect[3] - rect[1]) * view.scale,
      );
      ctx.restore();
      drawPeaks();
    } catch {
      shadeLoadState = 'failed'; // decode/paint failed mid-session — keep drawing links
      terrainOn = false;
      rebuildControls();
    }
  }

  function drawPeaks() {
    const site = anchorSite();
    const peaks = site && terrain?.sites ? terrain.sites[site.name]?.peaks : null;
    if (!Array.isArray(peaks)) return;
    ctx.save();
    ctx.fillStyle = PEAK_COLOR;
    ctx.font = '10px system-ui, sans-serif'; // UI-scale, never zoom-scaled
    ctx.textAlign = 'left';
    for (const peak of peaks) {
      if (!Array.isArray(peak.enu)) continue;
      const [x, y] = worldToScreen([peak.enu[0], peak.enu[1]]);
      ctx.beginPath();
      ctx.moveTo(x, y - 4);
      ctx.lineTo(x - 4, y + 3);
      ctx.lineTo(x + 4, y + 3);
      ctx.closePath();
      ctx.fill();
      if (peak.name) ctx.fillText(peak.name, x + 6, y + 3);
    }
    ctx.restore();
  }

  function draw() {
    const w = host.clientWidth, h = host.clientHeight;
    ctx.clearRect(0, 0, w, h);
    const site = anchorSite();
    if (!site) return;
    const enu = site.enu;

    // Terrain underlay first — beneath links, dots, labels, HUD (055 US4 draw order).
    if (terrainOn) drawTerrainUnderlay();

    // Links (055 US1): each on its own curve; health color; clearance dash; selection halo;
    // utilization fill; capacity label at the apex. Hidden bands are skipped entirely (044).
    const levels = levelsByLinkId();
    const visibleEndpoints = new Set();
    for (const link of geometry.links) {
      if (hiddenBands.has(bandOf(link))) continue;
      const a = enu[link.a], b = enu[link.b];
      if (!a || !b) continue;
      visibleEndpoints.add(link.a);
      visibleEndpoints.add(link.b);
      const g = curveFor(enu, link, levels.get(link.id) ?? 0);
      const isSelected = link.id === selectedId;
      // Thickness follows capacity (044 US2): the shared scale in bands.js; no capacity
      // data = the neutral default width, never a fabricated size.
      const width = lineWidth2d(link);
      ctx.setLineDash(clearanceFlagged(link) ? [7, 5] : []);
      if (isSelected) {
        ctx.strokeStyle = '#e6edf3';
        ctx.lineWidth = width + 3.5;
        tracePath(g);
        ctx.stroke();
      }
      ctx.strokeStyle = linkColor(link);
      ctx.lineWidth = isSelected ? width + 1 : width;
      tracePath(g);
      ctx.stroke();

      // Utilization fill (055 US2): a semi-transparent inner line, worst-direction fraction of
      // the stroke wide, along the SAME curve; no computable fraction ⇒ no fill (≠ zero).
      const fraction = usageFraction(link);
      if (fraction != null && fraction > 0) {
        ctx.setLineDash([]);
        ctx.strokeStyle = USAGE_FILL;
        ctx.lineWidth = width * Math.min(fraction, 1); // clamped to the stroke
        tracePath(g);
        ctx.stroke();
      }

      // Capacity/usage readout at the curve apex (044 FR-008) — only when the export carries it.
      const capacity = capacityLine(link);
      if (capacity) {
        ctx.setLineDash([]);
        ctx.font = '10px system-ui, sans-serif';
        ctx.fillStyle = '#9fb0c0';
        ctx.textAlign = 'center';
        const [lx, ly] = apexOf(g);
        ctx.fillText(capacity, lx, ly - 6 - width / 2);
      }
    }
    ctx.setLineDash([]);

    // Endpoint dots + one label per site at its radios' centroid (the debug-map recipe).
    // Site labels stay band-independent (their centroid uses every endpoint); a dot draws
    // only while some visible link uses that radio (044, contracts/viewer-bands.md).
    const endpointSite = new Map(geometry.endpoints.map((e) => [e.name, e.site]));
    // A maintenance device's dot goes orange (044 US3) — same meaning as its links.
    const endpointMaintenance = new Map(
      geometry.endpoints.map((e) => [e.name, e.maintenance === true])
    );
    const centroids = new Map();
    for (const [name, coords] of Object.entries(enu)) {
      const [x, y] = worldToScreen(coords);
      if (visibleEndpoints.has(name)) {
        ctx.fillStyle = endpointMaintenance.get(name) ? MAINTENANCE_COLOR : '#e6edf3';
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
      const label = endpointSite.get(name) ?? name;
      const c = centroids.get(label) ?? { sx: 0, sy: 0, count: 0 };
      centroids.set(label, { sx: c.sx + x, sy: c.sy + y, count: c.count + 1 });
    }
    ctx.font = '12px system-ui, sans-serif';
    ctx.fillStyle = '#9fb0c0';
    ctx.textAlign = 'left';
    for (const [name, c] of centroids) {
      ctx.fillText(name, c.sx / c.count + 7, c.sy / c.count - 7);
    }

    // Orientation cues: north arrow + scale bar (FR-004 — plain background, labeled).
    ctx.fillStyle = '#9fb0c0';
    ctx.textAlign = 'center';
    ctx.fillText('N', 22, 22);
    ctx.beginPath();
    ctx.moveTo(22, 46); ctx.lineTo(22, 28);
    ctx.moveTo(17, 35); ctx.lineTo(22, 28); ctx.lineTo(27, 35);
    ctx.strokeStyle = '#9fb0c0';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    const kmCandidates = [0.2, 0.5, 1, 2, 5, 10, 20, 50];
    const barKm = kmCandidates.find((km) => km * 1000 * view.scale > 70) ?? 50;
    const barPx = barKm * 1000 * view.scale;
    ctx.beginPath();
    ctx.moveTo(w - 16 - barPx, h - 18); ctx.lineTo(w - 16, h - 18);
    ctx.stroke();
    ctx.fillText(`${barKm} km`, w - 16 - barPx / 2, h - 26);
  }

  // --- selection + detail panel (FR-005) --------------------------------------------------------

  function row(label, value) {
    const div = document.createElement('div');
    div.className = 'kit-facts'; // 076: shared fact-pair chrome (was .map-detail .row)
    const dt = document.createElement('span');
    dt.className = 'k';
    dt.textContent = label;
    const dd = document.createElement('span');
    dd.className = 'v';
    dd.textContent = value;
    div.append(dt, dd);
    return div;
  }

  function closeDetail() { panel.close(); } // onClose deselects + redraws

  function showDetail(link) {
    // 076: the panel chrome (title + ✕) is the kit panel's; showDetail builds only the body node.
    const body = document.createElement('div');
    body.className = 'map-detail';

    const endpointSite = new Map(geometry.endpoints.map((e) => [e.name, e.site]));
    const health = document.createElement('div');
    health.className = 'health';
    // Maintenance wins the color but never hides the underlying health (044 US3).
    health.textContent = link.maintenance === true
      ? `maintenance · ${link.health}` : link.health;
    health.style.color = linkColor(link);
    body.appendChild(health);

    body.appendChild(row('A', `${link.a} (${endpointSite.get(link.a) ?? '?'})`));
    body.appendChild(row('B', `${link.b} (${endpointSite.get(link.b) ?? '?'})`));
    if (link.band) body.appendChild(row('band', link.band));
    if (link.frequency_ghz != null) {
      const provenance =
        link.frequency_provenance && link.frequency_provenance !== 'controller'
          ? ` (${link.frequency_provenance})`
          : '';
      body.appendChild(row('frequency', `${link.frequency_ghz} GHz${provenance}`));
    }
    if (link.length_m != null) {
      body.appendChild(row('length', `${(link.length_m / 1000).toFixed(2)} km`));
    }
    if (link.signal_dbm != null) body.appendChild(row('signal', `${link.signal_dbm} dBm`));

    // Capacity + usage rows (044 FR-009): down/up split when known; explicit wording when
    // the controller reports nothing (a dark radio), so absence never reads as zero.
    if (link.capacity_down_bps != null || link.capacity_up_bps != null) {
      body.appendChild(row(
        'capacity',
        `${formatRate(link.capacity_down_bps) ?? '—'} ↓ / ${formatRate(link.capacity_up_bps) ?? '—'} ↑`,
      ));
      if (link.usage_down_bps != null || link.usage_up_bps != null) {
        const percent = usagePercent(link);
        body.appendChild(row(
          'usage',
          `${formatRate(link.usage_down_bps) ?? '—'} ↓ / ${formatRate(link.usage_up_bps) ?? '—'} ↑` +
          (percent ? ` (${percent})` : ''),
        ));
      }
    } else {
      body.appendChild(row('capacity', 'unavailable'));
    }

    if (link.clearance_verdict != null) {
      const verdict = row(
        'clearance',
        `${link.clearance_verdict} · worst ${link.clearance_margin_m?.toFixed(1)} m ` +
        `at ${Math.round((link.clearance_at_fraction ?? 0) * 100)}% of the path`,
      );
      verdict.classList.add(`verdict-${link.clearance_verdict}`);
      body.appendChild(verdict);
    }

    // Elevation-profile chart (clarified 2026-07-08): only for links whose export carries
    // a terrain profile; absence degrades to the verdict fields above, chartless.
    const profile = terrain && terrain.profiles ? terrain.profiles[link.id] : null;
    if (profile && Array.isArray(profile.samples_amsl) && profile.samples_amsl.length >= 2) {
      const chart = document.createElement('canvas');
      chart.className = 'profile';
      body.appendChild(chart);
      const caption = document.createElement('div');
      caption.className = 'caption';
      caption.textContent =
        'Path profile: terrain, line of sight, first Fresnel zone; ● = worst clearance.';
      body.appendChild(caption);
      requestAnimationFrame(() => drawProfile(chart, link, profile));
    }

    panel.open({ title: link.name, body }); // in-place refresh: re-open replaces the body
  }

  function drawProfile(chart, link, profile) {
    const ratio = window.devicePixelRatio || 1;
    const cssWidth = chart.clientWidth || (chart.parentElement?.clientWidth ?? 300) - 24;
    const cssHeight = 150;
    chart.width = Math.round(cssWidth * ratio);
    chart.height = Math.round(cssHeight * ratio);
    chart.style.height = `${cssHeight}px`;
    const c = chart.getContext('2d');
    c.setTransform(ratio, 0, 0, ratio, 0, 0);

    const samples = profile.samples_amsl;
    const byName = new Map(geometry.endpoints.map((e) => [e.name, e]));
    const heightA = byName.get(link.a)?.height_amsl_m;
    const heightB = byName.get(link.b)?.height_amsl_m;
    const radius = link.fresnel_radius_m ?? 0;
    const los = (t) =>
      heightA != null && heightB != null ? heightA + (heightB - heightA) * t : null;
    // The first-Fresnel half-height at path fraction t — the (D/2, r, r) ellipsoid's section.
    const envelope = (t) => radius * 2 * Math.sqrt(t * (1 - t));

    let lo = Math.min(...samples);
    let hi = Math.max(...samples);
    if (heightA != null && heightB != null) {
      lo = Math.min(lo, heightA - radius, heightB - radius);
      hi = Math.max(hi, heightA + radius, heightB + radius);
    }
    const pad = Math.max(2, (hi - lo) * 0.12);
    lo -= pad;
    hi += pad;
    const left = 34, right = 6, top = 6, bottom = 16;
    const plotW = cssWidth - left - right;
    const plotH = cssHeight - top - bottom;
    const sx = (t) => left + t * plotW;
    const sy = (amsl) => top + (1 - (amsl - lo) / (hi - lo)) * plotH;

    c.clearRect(0, 0, cssWidth, cssHeight);

    // terrain: filled silhouette
    c.beginPath();
    c.moveTo(sx(0), sy(samples[0]));
    for (let i = 1; i < samples.length; i++) {
      c.lineTo(sx(i / (samples.length - 1)), sy(samples[i]));
    }
    c.lineTo(sx(1), cssHeight - bottom);
    c.lineTo(sx(0), cssHeight - bottom);
    c.closePath();
    c.fillStyle = 'rgba(148, 121, 91, 0.45)';
    c.fill();

    if (heightA != null && heightB != null) {
      // Fresnel envelope: translucent band around the line of sight
      c.beginPath();
      const steps = 48;
      for (let i = 0; i <= steps; i++) {
        const t = i / steps;
        const y = sy(los(t) - envelope(t));
        if (i === 0) c.moveTo(sx(t), y);
        else c.lineTo(sx(t), y);
      }
      for (let i = steps; i >= 0; i--) {
        const t = i / steps;
        c.lineTo(sx(t), sy(los(t) + envelope(t)));
      }
      c.closePath();
      c.fillStyle = 'rgba(96, 165, 250, 0.18)';
      c.fill();

      // line of sight
      c.beginPath();
      c.moveTo(sx(0), sy(los(0)));
      c.lineTo(sx(1), sy(los(1)));
      c.strokeStyle = '#60a5fa';
      c.lineWidth = 1.5;
      c.stroke();
    }

    // worst-clearance marker (precomputed: at_fraction + margin)
    if (link.clearance_at_fraction != null && heightA != null && heightB != null) {
      const t = link.clearance_at_fraction;
      const y = sy(los(t) - envelope(t));
      c.beginPath();
      c.arc(sx(t), y, 4, 0, Math.PI * 2);
      c.fillStyle = clearanceFlagged(link) ? '#ef4444' : '#22c55e';
      c.fill();
      if (link.clearance_margin_m != null) {
        c.font = '11px system-ui, sans-serif';
        c.fillStyle = '#e6edf3';
        c.textAlign = t > 0.5 ? 'right' : 'left';
        c.fillText(
          `${link.clearance_margin_m.toFixed(1)} m`,
          sx(t) + (t > 0.5 ? -8 : 8),
          y - 8,
        );
      }
    }

    // axes: elevation ticks + span label
    c.font = '10px system-ui, sans-serif';
    c.fillStyle = '#9fb0c0';
    c.textAlign = 'left';
    c.fillText(`${Math.round(hi)} m`, 2, top + 8);
    c.fillText(`${Math.round(lo)} m`, 2, cssHeight - bottom);
    if (link.length_m != null) {
      c.textAlign = 'center';
      c.fillText(`${(link.length_m / 1000).toFixed(2)} km`, left + plotW / 2, cssHeight - 4);
    }
  }

  // --- interaction: pan / pinch-zoom / wheel / tap-select (FR-011: touch-first) -----------------

  const pointers = new Map(); // pointerId -> {x, y}
  let pinchStart = null; // {distance, scale, mid:[x,y], world:[e,n]}
  let dragStart = null; // {x, y, cx, cy, moved}

  canvas.addEventListener('pointerdown', (event) => {
    canvas.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
    if (pointers.size === 1) {
      dragStart = { x: event.offsetX, y: event.offsetY, cx: view.cx, cy: view.cy, moved: false };
    } else if (pointers.size === 2) {
      const [p1, p2] = [...pointers.values()];
      pinchStart = {
        distance: Math.hypot(p1.x - p2.x, p1.y - p2.y),
        scale: view.scale,
        mid: [(p1.x + p2.x) / 2, (p1.y + p2.y) / 2],
      };
      pinchStart.world = screenToWorld(...pinchStart.mid);
      dragStart = null;
    }
  });

  canvas.addEventListener('pointermove', (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
    if (pointers.size === 2 && pinchStart) {
      const [p1, p2] = [...pointers.values()];
      const distance = Math.hypot(p1.x - p2.x, p1.y - p2.y);
      if (distance > 0) {
        view.scale = pinchStart.scale * (distance / pinchStart.distance);
        // keep the pinch midpoint anchored to the same world position
        const mid = [(p1.x + p2.x) / 2, (p1.y + p2.y) / 2];
        view.cx = pinchStart.world[0] - (mid[0] - host.clientWidth / 2) / view.scale;
        view.cy = pinchStart.world[1] + (mid[1] - host.clientHeight / 2) / view.scale;
        draw();
      }
    } else if (pointers.size === 1 && dragStart) {
      const dx = event.offsetX - dragStart.x;
      const dy = event.offsetY - dragStart.y;
      if (Math.abs(dx) + Math.abs(dy) > 6) dragStart.moved = true;
      view.cx = dragStart.cx - dx / view.scale;
      view.cy = dragStart.cy + dy / view.scale;
      draw();
    }
  });

  function endPointer(event) {
    const wasTap = pointers.size === 1 && dragStart && !dragStart.moved;
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinchStart = null;
    if (wasTap) selectAt(event.offsetX, event.offsetY);
    if (pointers.size === 0) dragStart = null;
  }
  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', (event) => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinchStart = null;
    if (pointers.size === 0) dragStart = null;
  });

  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.0015);
    const world = screenToWorld(event.offsetX, event.offsetY);
    view.scale *= factor;
    view.cx = world[0] - (event.offsetX - host.clientWidth / 2) / view.scale;
    view.cy = world[1] + (event.offsetY - host.clientHeight / 2) / view.scale;
    draw();
  }, { passive: false });

  function selectAt(x, y) {
    // 055 US1: hit-test each link's ACTUAL curve (sampled) so a bowed parallel is reachable —
    // a straight point-to-segment test would miss it and select its sibling. Hidden bands are
    // skipped (044). Same 18 px phone tolerance as before.
    const site = anchorSite();
    if (!site) return;
    const enu = site.enu;
    const levels = levelsByLinkId();
    let best = null;
    let bestDistance = 18;
    for (const link of geometry.links) {
      if (hiddenBands.has(bandOf(link))) continue;
      if (!enu[link.a] || !enu[link.b]) continue;
      const g = curveFor(enu, link, levels.get(link.id) ?? 0);
      let px = g.ax, py = g.ay;
      if (g.curved) {
        for (let s = 1; s <= 24; s += 1) {
          const [qx, qy] = qPoint(g.ax, g.ay, g.cx, g.cy, g.bx, g.by, s / 24);
          const distance = distanceToSegment(x, y, px, py, qx, qy);
          if (distance < bestDistance) { bestDistance = distance; best = link; }
          px = qx; py = qy;
        }
      } else {
        const distance = distanceToSegment(x, y, g.ax, g.ay, g.bx, g.by);
        if (distance < bestDistance) { bestDistance = distance; best = link; }
      }
    }
    if (best) {
      selectedId = best.id;
      // 058: in a workspace, open the link's own window; full-screen keeps the in-canvas panel.
      if (openLink) openLink(linkKey(best.a, best.b), best.name);
      else showDetail(best);
    } else if (openLink) {
      selectedId = null; // window mode has no panel — just deselect the highlight
    } else {
      closeDetail();
    }
    draw();
  }

  // --- data lifecycle: initial load, once-only terrain, 5-minute poll (FR-006) ------------------

  function rebuild(fresh) {
    const firstLoad = geometry === null;
    geometry = fresh;
    endpointSiteMap = new Map(geometry.endpoints.map((e) => [e.name, e.site])); // 055 US1 key
    if (anchorName === null || !geometry.sites.some((s) => s.name === anchorName)) {
      anchorName = geometry.sites[0]?.name ?? null;
    }
    if (firstLoad) fitViewport();
    if (selectedId !== null) {
      // Keep the selection highlight across a refresh while the link still exists and is visible;
      // the in-canvas panel is refreshed in place only in full-screen (fallback) mode — in a
      // workspace the link's own window polls itself (058).
      const still = geometry.links.find((l) => l.id === selectedId);
      if (!still || hiddenBands.has(bandOf(still))) {
        selectedId = null;
        if (!openLink) closeDetail();
      } else if (!openLink) {
        showDetail(still); // refresh in place (fallback mode)
      }
    }
    rebuildChips(); // the toggle set follows the data; hidden state persists (044)
    rebuildControls(); // fit appears with the first geometry; terrain follows anchor/shade (057)
    status.hidden = true;
    updateAge();
    draw();
  }

  async function pollOnce() {
    try {
      const response = await fetch('links.json', { cache: 'no-cache' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const fresh = await response.json();
      refreshFailing = false;
      rebuild(fresh);
    } catch (err) {
      refreshFailing = true;
      if (geometry === null) showStatus(`${NO_EXPORT_HELP}\n(${err})`, true);
      updateAge(); // keep the last good render; say the refresh is failing
    }
  }

  async function fetchTerrainOnce() {
    try {
      const response = await fetch('terrain.json', { cache: 'no-cache' });
      if (response.ok) terrain = await response.json();
    } catch (err) {
      terrain = null; // chartless degrade — verdict fields still shown
    }
    // 055: now that the shade meta is known, offer the terrain chip and, if the operator's
    // saved preference is on, lazily fetch the underlay and repaint (never inside the poll).
    rebuildControls();
    if (terrainOn && terrainAvailable()) ensureShadeImage();
    draw();
  }

  // Sizing follows the host element, not the browser window: inside a workspace window the
  // viewport rarely changes when the WINDOW does (drag-resize, snap, maximize). The observer
  // also fires once on observe(), covering the initial layout in both hosts (037, research R5).
  const observer = new ResizeObserver(resizeCanvas);
  observer.observe(host);
  resizeCanvas();
  // The hillshade PNG is the map's heaviest asset (≈4 MB, vs the ~2 MB terrain metadata and ~68 KB
  // link geometry). Its old trigger fired only after BOTH JSON artifacts resolved, so the big image
  // queued behind two serial downloads before the terrain could paint. Terrain now defaults on, so
  // start that download up front — it overlaps the JSON fetches instead of waiting on them. The
  // image URL is a constant, and drawTerrainUnderlay() still gates on the anchor rect + a 'ready'
  // image, so an early arrival simply waits (harmlessly) for the geometry to paint.
  if (terrainOn) ensureShadeImage();
  // The two JSON artifacts fetch concurrently; each path redraws when it lands. Both swallow their
  // own errors, so neither settles as a rejection (safe for Promise.all).
  await Promise.all([pollOnce(), fetchTerrainOnce()]); // initial load; NO_EXPORT_HELP if no export
  const pollTimer = setInterval(pollOnce, POLL_MS);
  const ageTimer = setInterval(updateAge, 30 * 1000); // the age readout ticks between polls

  return {
    dispose() {
      observer.disconnect();
      clearInterval(pollTimer);
      clearInterval(ageTimer);
      canvas.remove();
      status.remove();
      panel.destroy();
      chips.remove();
      controls.remove();
    },
  };
}
