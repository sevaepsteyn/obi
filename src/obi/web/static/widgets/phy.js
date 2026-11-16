// Router PHY console (feature 075) — one router's LIVE, read-only PHYSICAL drill-down, the PHY tab
// of the ros:<address> window (lazy-imported by widgets/router.js on first PHY activation).
//
// Fetches GET /api/phy.json?device=<address> (a read-only RouterOS `print` + `monitor … once` read
// cycle over one login; constitution 1.11.0) and renders: a device summary (model · serial ·
// version · firmware current/upgrade + behind-flag), an inline-SVG faceplate whose ports are lit
// per a device-LED mimic (lit/amber/dark/disabled) with cage occupancy, and — added by later
// stories — an inspector, per-port tables, and ⚠ degradation badges. The heavy read runs ONLY while
// this tab is active (FR-014): the poller is started on activate() and stopped on deactivate().
//
// Degrades honestly at every layer: store-absent shows the hint, an unreachable device shows its
// state and keeps the last-good faceplate, a null field renders blank — never an error (FR-012/016).

import { labeledFields } from '../boards.js';
import {
  annotateContext, createFeed, createPanel, createRefreshButton, createStatusLine, createTooltip,
  fetchJson, severityColor,
} from '../kit.js';
import { layoutFor } from '../faceplates.js';

const REFRESH_MS = 120 * 1000; // same cadence as the L3 tab — a physical console is read on demand
const SVG_NS = 'http://www.w3.org/2000/svg';
const PHY_SFP_TEMP_WARN_C = 70; // SFP optic over-temperature threshold (D13; not configurable in v1)

// Faceplate grid geometry (SVG user units; CSS scales the whole thing to fit the window).
const CELL = 24;
const GAP = 6;
const PAD = 10;
const LABEL_H = 14;

// A port's real ceiling is the LESSER of two limits: the cage's, from the faceplate layout, and the
// installed module's, which only the device knows (transceiver.max_rate_mbps, from `sfp-supported`).
// Neither alone is the answer — a 1G S-RJ01 in a 10G cage tops out at the module's 1G and is healthy
// there, while a 25G XS+DA0001 DAC in that same cage tops out at the cage's 10G. Either limit can be
// unknown (an older payload, a module that doesn't report) ⇒ fall back to whichever we do know.
function effectiveMaxMbps(port, maxMbps) {
  const cage = typeof maxMbps === 'number' ? maxMbps : null;
  const mod = typeof port?.transceiver?.max_rate_mbps === 'number'
    ? port.transceiver.max_rate_mbps
    : null;
  if (cage === null) return mod;
  if (mod === null) return cage;
  return Math.min(cage, mod);
}

// --- LED language (spec device-mimic palette; distinct from kit severity classes) ---------------
// A port's base state from disabled/link/rate-vs-max. The ⚠ badge overlay (US5) is the layer where
// "obi knows more"; these classes are just the physical link LED a device itself would show.
function ledClass(port, maxMbps) {
  if (!port) return 'phy-off'; // layout has the port, device didn't report it → dark/unknown
  if (port.disabled === true) return 'phy-disabled';
  if (port.link_up === true) {
    const max = effectiveMaxMbps(port, maxMbps);
    if (typeof port.rate_mbps === 'number' && typeof max === 'number' && port.rate_mbps < max) {
      return 'phy-amber'; // linked, but below what this port + its module can actually do
    }
    return 'phy-lit';
  }
  return 'phy-off'; // no-link or unknown link state
}

// The short label drawn on a port: the trailing ordinal of the default name (ether7 → "7",
// sfp-sfpplus3 → "S3", sfp28-1 → "X1", qsfpplus1-1 → "Q1"), so a dense faceplate stays legible.
function portGlyph(defaultName) {
  const id = String(defaultName || '');
  const num = (id.match(/(\d+)(?:-\d+)?$/) || [])[1] || '';
  // QSFP channels keep their full channel number, INCLUDING the -1 primary (Q1-1 … Q2-4): the
  // interface really is qsfpplus1-1, and a bare "Q1" glyph misread as "the whole cage" (T036).
  const chan = id.match(/^qsfp\D*(\d+)-(\d+)$/);
  if (chan) return `Q${chan[1]}-${chan[2]}`;
  if (id.startsWith('qsfp')) return `Q${num}`;
  if (id.startsWith('sfp28')) return `X${num}`;
  if (id.startsWith('sfp')) return `S${num}`;
  return num || id;
}

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

// --- display helpers (honest blanks: a null field renders "—", never an error) -----------------

function dash(value) {
  return value == null || value === '' ? '—' : String(value);
}

function portStatus(port) {
  if (!port) return 'unknown';
  if (port.disabled === true) return 'disabled';
  if (port.link_up === true) return 'up';
  if (port.link_up === false) return 'down';
  return 'unknown';
}

// Mbps → a compact rate label ("10 Gbps", "2.5 Gbps", "100 Mbps"); null ⇒ "—".
function rateLabel(mbps) {
  if (typeof mbps !== 'number') return '—';
  if (mbps >= 1000) return `${(mbps / 1000).toString().replace(/\.0$/, '')} Gbps`;
  return `${mbps} Mbps`;
}

function duplexLabel(port) {
  if (port.full_duplex === true) return 'full';
  if (port.full_duplex === false) return 'half';
  return '—';
}

// --- inspector field builders — each section renders only when its data applies (honest empties).
// US2 fills linkFields; transceiver/poe/l2/neighbor/warning are extended by US3/US4/US5.

function linkFields(port) {
  return [
    ['default name', dash(port.default_name)],
    ['status', portStatus(port)],
    ['rate', port.link_up ? rateLabel(port.rate_mbps) : '—'],
    ['duplex', port.link_up ? duplexLabel(port) : '—'],
    ['auto-neg', dash(port.auto_negotiation)],
    ['mac', dash(port.mac)],
  ];
}

function transceiverFields(port) {
  const t = port.transceiver;
  if (!t) return []; // copper port, no cage
  const rows = [['sfp module', t.present ? 'present' : 'empty cage']];
  if (t.vendor) rows.push(['sfp vendor', t.vendor]);
  if (t.part) rows.push(['sfp part', t.part]);
  if (t.max_rate_mbps != null) rows.push(['sfp max rate', rateLabel(t.max_rate_mbps)]);
  if (t.wavelength_nm != null) rows.push(['wavelength', `${t.wavelength_nm} nm`]);
  if (t.temperature_c != null) rows.push(['optic temp', `${t.temperature_c} °C`]);
  if (t.tx_power_dbm != null) rows.push(['tx power', `${t.tx_power_dbm} dBm`]);
  if (t.rx_power_dbm != null) rows.push(['rx power', `${t.rx_power_dbm} dBm`]);
  return rows;
}

function poeFields(port) {
  const p = port.poe;
  if (!p) return []; // no PoE-out on this port
  const rows = [['poe-out', dash(p.out)]];
  if (p.status) rows.push(['poe status', p.status]);
  if (p.voltage_v != null) rows.push(['poe voltage', `${p.voltage_v} V`]);
  if (p.current_ma != null) rows.push(['poe current', `${p.current_ma} mA`]);
  if (p.power_w != null) rows.push(['poe power', `${p.power_w} W`]);
  return rows;
}

function vlanList(ids) {
  return Array.isArray(ids) && ids.length ? ids.join(', ') : '—';
}

function l2Fields(port) {
  const l2 = port.l2;
  if (!l2) return []; // not a bridge member
  const rows = [['bridge', dash(l2.bridge)], ['pvid', dash(l2.pvid)]];
  rows.push(['tagged vlans', vlanList(l2.tagged_vlans)]);
  rows.push(['untagged vlans', vlanList(l2.untagged_vlans)]);
  if (l2.stp_state) rows.push(['stp state', l2.stp_state]);
  if (l2.stp_role) rows.push(['stp role', l2.stp_role]);
  return rows;
}

function neighborFields(port) {
  const n = port.neighbor;
  if (!n) return []; // no peer seen on this port
  const rows = [['neighbor', dash(n.identity)]];
  if (n.address) rows.push(['neighbor ip', n.address]);
  if (n.mac) rows.push(['neighbor mac', n.mac]);
  if (n.platform) rows.push(['neighbor platform', n.platform]);
  return rows;
}

// Degradation badges (US5, D3/D13) — derived client-side from payload facts + the layout maximum
// (the one place all four inputs meet). `maxMbps` is the CAGE's max, narrowed by the module's via
// effectiveMaxMbps; `errorIfnames` is the set of interface names with a recent error-counter climb
// (metrics.error_increases, joined by name).
function portWarnings(port, maxMbps, errorIfnames) {
  const warnings = [];
  if (port.link_up === true && port.full_duplex === false) warnings.push('half-duplex');
  const max = effectiveMaxMbps(port, maxMbps);
  if (port.link_up === true && typeof port.rate_mbps === 'number'
      && typeof max === 'number' && port.rate_mbps < max) {
    warnings.push(`below max rate (${rateLabel(port.rate_mbps)} of ${rateLabel(max)})`);
  }
  const t = port.transceiver;
  if (t && typeof t.temperature_c === 'number' && t.temperature_c >= PHY_SFP_TEMP_WARN_C) {
    warnings.push(`hot optic (${t.temperature_c} °C ≥ ${PHY_SFP_TEMP_WARN_C})`);
  }
  if (errorIfnames.has(port.name)) warnings.push('error counters climbing');
  return warnings;
}

export async function mount(host, ctx) {
  const address = ctx.params?.address ?? '';
  let identity = ctx.params?.identity ?? '';

  host.classList.add('phy');
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';

  // 076: the toolbar row, ⟳ refresh button, and status line are the shared kit chrome now.
  const toolbar = document.createElement('div');
  toolbar.className = 'kit-toolbar';
  const refreshBtn = createRefreshButton(() => { status.set('reading…'); return doRead(); });
  toolbar.append(refreshBtn);
  const status = createStatusLine(toolbar);

  const summary = document.createElement('div');
  summary.className = 'phy-summary';

  // Scrollable content: faceplate, then the click-pinned inspector, then the per-port tables.
  const body = document.createElement('div');
  body.className = 'phy-body';

  const faceHost = document.createElement('div');
  faceHost.className = 'phy-face-host';

  const tablesHost = document.createElement('div');
  tablesHost.className = 'phy-tables';

  body.append(faceHost, tablesHost);

  // 076: the top-level device state (loading / unreachable / not-configured) is the shared muted
  // message box (.kit-message) — the phy-state clone is retired.
  const stateEl = document.createElement('div');
  stateEl.className = 'kit-message';
  stateEl.hidden = true;

  host.append(toolbar, summary, body, stateEl);

  // 076: the faceplate hover tooltip is the shared kit tooltip (one floating hover surface).
  const tooltip = createTooltip();

  // 076: the click-pinned port inspector is now the shared floating panel (kit.createPanel),
  // anchored on the widget host — not the scrolling `.phy-body` — so it stays visible while the
  // body scrolls. Closing it (✕ / Esc) deselects the port; no popOut (its pop-out is a snapshot).
  const inspector = createPanel(host, ctx, { onClose: deselectPort });

  let lastPayload = null; // last OK payload — kept so a later failure still shows the faceplate
  let faceplatePorts = []; // the current payload ports (join source for tables + inspector)
  let selectedDefault = null; // the inspected port's default-name (persists across refreshes)
  const portEls = new Map(); // default-name → its SVG <g>, for highlight + reselect after re-render
  let layoutMaxByDefault = new Map(); // default-name → hardware maxMbps (for the below-max badge)
  let errorIfnames = new Set(); // interface names with a recent error-counter climb (US5)

  // The badge reasons for a port, using the layout maximum + the metrics error-counter set (US5).
  const warningsFor = (port) =>
    portWarnings(port, layoutMaxByDefault.get(port.default_name), errorIfnames);

  function tableSection(label) {
    const container = document.createElement('div');
    container.className = 'phy-table-section';
    const heading = document.createElement('div');
    heading.className = 'phy-section';
    heading.textContent = label;
    const feedHost = document.createElement('div');
    feedHost.className = 'phy-feed';
    container.append(heading, feedHost);
    tablesHost.appendChild(container);
    return { container, feedHost };
  }

  // A table's 056 + row-click wiring is identical across the three tables (link/transceiver/poe).
  const rowHooks = {
    onRowClick: (row) => selectPort(row.default),
    contextFor: ({ row, columns }) => ({
      subject: `port ${row.name} on ${identity || address}`,
      elementText: labeledFields(row, columns),
    }),
  };

  const portRow = (port) => {
    const warnings = warningsFor(port);
    return {
      name: port.name,
      default: port.default_name,
      status: portStatus(port),
      rate: port.link_up ? rateLabel(port.rate_mbps) : '',
      duplex: port.link_up ? duplexLabel(port) : '',
      autoneg: port.auto_negotiation ?? '',
      mac: port.mac ?? '',
      warn: warnings.length ? '⚠' : '',
      warnReasons: warnings.join('; '), // the cell title (hover) for the ⚠ marker
    };
  };

  const linkSection = tableSection('per-port link');
  const linkFeed = createFeed(linkSection.feedHost, {
    tail: false,
    persist: 'phy-ports', // shared across router windows — the 046 twin-window rule
    columns: [
      { key: 'warn', label: '', width: '3ch', title: 'warnReasons',
        color: (v) => (v ? 'kit-c-warn' : 'kit-c-muted') },
      { key: 'name', label: 'name', width: '16ch' },
      { key: 'default', label: 'default', width: '12ch' },
      { key: 'status', label: 'status', width: '9ch', color: severityColor },
      { key: 'rate', label: 'rate', width: '9ch' },
      { key: 'duplex', label: 'duplex', width: '7ch' },
      { key: 'autoneg', label: 'auto-neg', width: '9ch' },
      { key: 'mac', label: 'mac', width: '17ch' },
    ],
    ...rowHooks,
  });
  linkFeed.showState('loading', 'loading…');

  // Transceiver (SFP DDM) table — shown only when the device has cage ports (US3).
  const sfpSection = tableSection('transceivers (SFP DDM)');
  const sfpFeed = createFeed(sfpSection.feedHost, {
    tail: false,
    persist: 'phy-sfp',
    columns: [
      { key: 'name', label: 'port', width: '16ch' },
      { key: 'vendor', label: 'vendor', width: '12ch' },
      { key: 'part', label: 'part', width: '14ch' },
      { key: 'wavelength', label: 'nm', width: '6ch', align: 'right' },
      { key: 'temp', label: '°C', width: '6ch', align: 'right' },
      { key: 'tx', label: 'tx dBm', width: '8ch', align: 'right' },
      { key: 'rx', label: 'rx dBm', width: '8ch', align: 'right' },
    ],
    ...rowHooks,
  });
  sfpSection.container.hidden = true;

  // PoE-out table — structurally-no-PoE models render no section at all (US3).
  const poeSection = tableSection('PoE-out');
  const poeFeed = createFeed(poeSection.feedHost, {
    tail: false,
    persist: 'phy-poe',
    columns: [
      { key: 'name', label: 'port', width: '16ch' },
      { key: 'mode', label: 'mode', width: '10ch' },
      { key: 'status', label: 'status', width: '15ch', color: severityColor },
      { key: 'volts', label: 'V', width: '6ch', align: 'right' },
      { key: 'ma', label: 'mA', width: '7ch', align: 'right' },
      { key: 'watts', label: 'W', width: '6ch', align: 'right' },
    ],
    ...rowHooks,
  });
  poeSection.container.hidden = true;

  // Bridge / VLAN / STP table (US4).
  const l2Section = tableSection('bridge / VLAN / STP');
  const l2Feed = createFeed(l2Section.feedHost, {
    tail: false,
    persist: 'phy-l2',
    columns: [
      { key: 'name', label: 'port', width: '16ch' },
      { key: 'bridge', label: 'bridge', width: '12ch' },
      { key: 'pvid', label: 'pvid', width: '6ch', align: 'right' },
      { key: 'tagged', label: 'tagged', width: '12ch' },
      { key: 'untagged', label: 'untagged', width: '12ch' },
      { key: 'stp', label: 'stp', width: '16ch' },
    ],
    ...rowHooks,
  });
  l2Section.container.hidden = true;

  // Neighbors table (US4).
  const neighborSection = tableSection('neighbors');
  const neighborFeed = createFeed(neighborSection.feedHost, {
    tail: false,
    persist: 'phy-neighbors',
    columns: [
      { key: 'name', label: 'port', width: '16ch' },
      { key: 'neighbor', label: 'neighbor', width: '16ch' },
      { key: 'ip', label: 'address', width: '18ch' },
      { key: 'mac', label: 'mac', width: '17ch' },
      { key: 'platform', label: 'platform', width: '10ch' },
    ],
    ...rowHooks,
  });
  neighborSection.container.hidden = true;

  function findByDefault(id) {
    return faceplatePorts.find((p) => (p.default_name || p.name) === id) || null;
  }

  function portContextText(port) {
    const lines = [`port ${port.name} (${port.default_name})`];
    for (const [k, v] of [...linkFields(port), ...transceiverFields(port), ...poeFields(port),
      ...l2Fields(port), ...neighborFields(port)]) {
      lines.push(`${k}: ${v}`);
    }
    for (const warning of warningsFor(port)) lines.push(`⚠ ${warning}`);
    return lines.join('\n');
  }

  // The inspector panel's body: the port's k/v facts (link/transceiver/poe/l2/neighbor) + warnings.
  function inspectorBody(port) {
    const dl = document.createElement('dl');
    dl.className = 'phy-inspector-fields';
    const warnings = warningsFor(port);
    const rows = [...linkFields(port), ...transceiverFields(port), ...poeFields(port),
      ...l2Fields(port), ...neighborFields(port),
      ...warnings.map((w) => ['⚠ warning', w])];
    for (const [label, value] of rows) {
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = value;
      if (String(label).startsWith('⚠')) dd.classList.add('phy-warn-reason');
      dl.append(dt, dd);
    }
    return dl;
  }

  function portTitle(port) {
    return port.default_name && port.default_name !== port.name
      ? `${port.name} · ${port.default_name}`
      : port.name;
  }

  // Closing the panel (✕ / Esc) deselects the port — the panel's onClose reconciles this state.
  function deselectPort() {
    selectedDefault = null;
    for (const [, g] of portEls) g.classList.remove('phy-selected');
  }

  function selectPort(id) {
    const port = findByDefault(id);
    if (!port) { inspector.close(); return; } // gone from the payload ⇒ close (onClose deselects)
    selectedDefault = id;
    for (const [d, g] of portEls) g.classList.toggle('phy-selected', d === id);
    inspector.open({ title: portTitle(port), body: inspectorBody(port) });
  }

  function renderSfpTable(ports) {
    const cages = ports.filter((p) => p.transceiver);
    sfpSection.container.hidden = cages.length === 0;
    if (!cages.length) return;
    sfpFeed.replace(cages.map((p) => ({
      name: p.name,
      default: p.default_name,
      vendor: p.transceiver.vendor ?? '',
      part: p.transceiver.part ?? '',
      wavelength: p.transceiver.wavelength_nm ?? '',
      temp: p.transceiver.temperature_c ?? '',
      tx: p.transceiver.tx_power_dbm ?? '',
      rx: p.transceiver.rx_power_dbm ?? '',
    })));
  }

  function renderPoeTable(ports, poeDetail) {
    const poePorts = ports.filter((p) => p.poe);
    // Structural absence: a non-PoE model (layout has no PoE ports) with no PoE data ⇒ no section.
    if (!layoutHasPoe && poePorts.length === 0) {
      poeSection.container.hidden = true;
      return;
    }
    poeSection.container.hidden = false;
    if (poeDetail && poePorts.length === 0) {
      // PoE hardware expected but the read degraded — honest empty state with the detail (C2).
      poeFeed.clear();
      poeFeed.showState('degraded', `PoE read degraded — ${poeDetail}`);
      return;
    }
    poeFeed.replace(poePorts.map((p) => ({
      name: p.name,
      default: p.default_name,
      mode: p.poe.out ?? '',
      status: p.poe.status ?? '',
      volts: p.poe.voltage_v ?? '',
      ma: p.poe.current_ma ?? '',
      watts: p.poe.power_w ?? '',
    })));
    if (!poePorts.length) poeFeed.showState('empty', 'no PoE-out ports');
  }

  // L2 + neighbors are always-expected sections (unlike PoE): show them when there is data OR a
  // degradation detail; otherwise (nothing configured) keep them hidden.
  function renderL2Table(ports, detail) {
    const l2Ports = ports.filter((p) => p.l2);
    if (!l2Ports.length && !detail) {
      l2Section.container.hidden = true;
      return;
    }
    l2Section.container.hidden = false;
    if (detail && !l2Ports.length) {
      l2Feed.clear();
      l2Feed.showState('degraded', `bridge read degraded — ${detail}`);
      return;
    }
    l2Feed.replace(l2Ports.map((p) => ({
      name: p.name,
      default: p.default_name,
      bridge: p.l2.bridge ?? '',
      pvid: p.l2.pvid ?? '',
      tagged: (p.l2.tagged_vlans || []).join(',') || '',
      untagged: (p.l2.untagged_vlans || []).join(',') || '',
      stp: [p.l2.stp_state, p.l2.stp_role].filter(Boolean).join(' / '),
    })));
  }

  function renderNeighborTable(ports, detail) {
    const neighborPorts = ports.filter((p) => p.neighbor);
    if (!neighborPorts.length && !detail) {
      neighborSection.container.hidden = true;
      return;
    }
    neighborSection.container.hidden = false;
    if (detail && !neighborPorts.length) {
      neighborFeed.clear();
      neighborFeed.showState('degraded', `neighbor read degraded — ${detail}`);
      return;
    }
    neighborFeed.replace(neighborPorts.map((p) => ({
      name: p.name,
      default: p.default_name,
      neighbor: p.neighbor.identity ?? '',
      ip: p.neighbor.address ?? '',
      mac: p.neighbor.mac ?? '',
      platform: p.neighbor.platform ?? '',
    })));
  }

  function showTooltip(def, port, event) {
    const rate = port && port.link_up ? ` · ${rateLabel(port.rate_mbps)}` : '';
    tooltip.show(`${port ? port.name : def.id} · ${portStatus(port)}${rate}`, event);
  }

  // `kind` is kept in the signature (callers pass loading/empty/error) but the shared message box
  // is one muted style — the text carries the state (the .kit-message convention).
  function showState(kind, message) {
    stateEl.hidden = false;
    stateEl.className = 'kit-message';
    stateEl.textContent = message;
  }

  function clearState() {
    stateEl.hidden = true;
    stateEl.textContent = '';
    stateEl.className = 'kit-message';
  }

  function renderSummary(device) {
    summary.replaceChildren();
    if (!device) return;
    const facts = [
      ['model', device.model],
      ['serial', device.serial],
      ['version', device.version],
      ['firmware', device.current_firmware],
    ];
    for (const [label, value] of facts) {
      if (value == null || value === '') continue;
      const cell = document.createElement('div');
      cell.className = 'kit-facts'; // 076: shared fact pair (was phy-fact — identical style)
      const k = document.createElement('span');
      k.className = 'k';
      k.textContent = label;
      const v = document.createElement('span');
      v.className = 'v';
      v.textContent = String(value);
      cell.append(k, v);
      summary.appendChild(cell);
    }
    // firmware-behind: only when both firmware fields were read (payload firmware_behind is a
    // real bool then, null otherwise) — a clear ⬆ flag naming the upgrade target (FR-002).
    if (device.firmware_behind === true && device.upgrade_firmware) {
      const flag = document.createElement('div');
      flag.className = 'kit-facts phy-fw-behind'; // 076: fact-pair flex + the fw-behind warn color
      flag.textContent = `⬆ update available → ${device.upgrade_firmware}`;
      summary.appendChild(flag);
    }
  }

  let layoutHasPoe = false; // does this model's faceplate expose PoE-out ports? (structural, D4/C2)

  function renderFaceplate(payload) {
    portEls.clear();
    const device = payload.device || {};
    const ports = payload.ports || [];
    const { layout } = layoutFor(device.model, ports);
    layoutHasPoe = layout.ports.some((p) => p.poe === true);
    layoutMaxByDefault = new Map(layout.ports.map((p) => [p.id, p.maxMbps]));
    const byDefault = new Map(ports.map((p) => [p.default_name || p.name, p]));

    const layoutIds = new Set(layout.ports.map((p) => p.id));
    const overflow = ports.filter((p) => !layoutIds.has(p.default_name || p.name));

    const cols = Math.max(0, ...layout.ports.map((p) => p.col)) + 1;
    const overflowCols = overflow.length;
    const totalCols = Math.max(cols, overflowCols);
    const width = PAD * 2 + totalCols * (CELL + GAP);
    const overflowRow = overflow.length ? layout.rows : -1;
    const gridRows = layout.rows + (overflow.length ? 1 : 0);
    // Every row gets its own label lane above it — with a single shared LABEL_H, the labels of
    // rows ≥ 1 (and of the overflow row) overprinted the cells above them (T036 field pass).
    const height = PAD * 2 + gridRows * (LABEL_H + CELL + GAP) - GAP;

    const svg = svgEl('svg', {
      class: 'phy-faceplate',
      viewBox: `0 0 ${width} ${height}`,
      // Explicit intrinsic size: with only a viewBox, a flex-squeezed <svg> collapses to zero
      // height in real browsers (aspect-ratio-only replaced element); CSS max-width/height:auto
      // still scales it down proportionally in narrow windows.
      width,
      height,
      role: 'img',
    });

    for (const def of layout.ports) {
      svg.appendChild(portNode(def, byDefault.get(def.id) || null));
    }
    // Payload ports missing from the curated layout: append a generic overflow row — data always
    // wins over drawing (FR-004 spirit).
    overflow.forEach((port, i) => {
      const def = {
        id: port.default_name || port.name,
        kind: 'rj45',
        row: overflowRow,
        col: i,
        maxMbps: undefined,
      };
      svg.appendChild(portNode(def, port));
    });

    faceHost.replaceChildren(svg);
  }

  function portNode(def, port) {
    const x = PAD + def.col * (CELL + GAP);
    const y = PAD + (def.row + 1) * LABEL_H + def.row * (CELL + GAP); // one label lane per row
    const g = svgEl('g', { class: 'phy-port', 'data-port': def.id, transform: `translate(${x},${y})` });

    const rect = svgEl('rect', {
      width: CELL,
      height: CELL,
      rx: def.kind === 'rj45' ? 2 : 5,
      class: `phy-cell ${ledClass(port, def.maxMbps)} phy-kind-${def.kind}`,
    });
    g.appendChild(rect);

    // Cages show occupancy: a small centre dot, lit when a module is present.
    if (def.kind !== 'rj45') {
      const present = port?.transceiver?.present === true;
      g.appendChild(svgEl('circle', {
        cx: CELL / 2,
        cy: CELL / 2,
        r: 3.5,
        class: present ? 'phy-cage-occupied' : 'phy-cage-empty',
      }));
    }

    // A small bottom bar marks a PoE-out-capable port (US3, from the faceplate layout flag).
    if (def.poe === true) {
      g.appendChild(svgEl('rect', { x: 2, y: CELL - 3, width: CELL - 4, height: 2, class: 'phy-poe-bar' }));
    }

    const label = svgEl('text', { x: CELL / 2, y: -3, class: 'phy-port-label' });
    label.textContent = portGlyph(def.id);
    g.appendChild(label);

    // Degradation badge (US5): a ⚠ overlay when the port has any derived warning.
    if (port && warningsFor(port).length) {
      g.classList.add('phy-warn');
      const badge = svgEl('text', { x: CELL - 1, y: 9, class: 'phy-badge' });
      badge.textContent = '⚠';
      g.appendChild(badge);
    }

    // Interaction (US2): hover tooltip, click → inspector, right-click → "ask obi about this".
    portEls.set(def.id, g);
    if (def.id === selectedDefault) g.classList.add('phy-selected');
    g.addEventListener('mouseenter', (event) => showTooltip(def, port, event));
    g.addEventListener('mousemove', (event) => tooltip.move(event));
    g.addEventListener('mouseleave', () => tooltip.hide());
    g.addEventListener('click', () => selectPort(def.id));
    if (port) {
      annotateContext(g, {
        contextFor: () => ({
          subject: `port ${port.name} on ${identity || address}`,
          elementText: portContextText(port),
        }),
      });
    }
    return g;
  }

  function applyPayload(payload) {
    lastPayload = payload;
    faceplatePorts = payload.ports || [];
    // The error-counter set must be set BEFORE the faceplate + tables render (they read it).
    errorIfnames = new Set(
      (payload.metrics?.error_increases || []).map((e) => e.ifname).filter(Boolean),
    );
    if (payload.device?.identity && payload.device.identity !== identity) {
      identity = payload.device.identity;
    }
    clearState();
    renderSummary(payload.device);
    renderFaceplate(payload);
    linkFeed.replace(faceplatePorts.map(portRow));
    if (!faceplatePorts.length) linkFeed.showState('empty', 'no ports reported');
    renderSfpTable(faceplatePorts);
    renderPoeTable(faceplatePorts, payload.sections?.poe ?? null);
    renderL2Table(faceplatePorts, payload.sections?.bridge ?? null);
    renderNeighborTable(faceplatePorts, payload.sections?.neighbors ?? null);
    // Reapply the pinned selection to the freshly rendered faceplate: re-open the inspector with a
    // rebuilt body only if it was open (076 — selectPort re-calls panel.open()).
    if (selectedDefault && inspector.isOpen()) selectPort(selectedDefault);
    const up = (payload.ports || []).filter((p) => p.link_up === true).length;
    const total = (payload.ports || []).length;
    const age = payload.read?.read_at ? new Date(payload.read.read_at).toLocaleTimeString() : '?';
    status.set(`reachable · ${up}/${total} up · read ${age}`, 'ok');
    ctx.setBadge?.(`${up}/${total} up`);
    ctx.setTitle?.('PHY');
  }

  function applyNonOk(payload) {
    const outcome = payload.read?.outcome ?? 'error';
    const detail = payload.read?.detail ? ` — ${payload.read.detail}` : '';
    if (payload.store === 'absent') {
      status.set(payload.hint ?? 'RouterOS not configured', 'muted');
      if (!lastPayload) {
        summary.replaceChildren();
        faceHost.replaceChildren();
        showState('empty', payload.hint ?? 'RouterOS not configured');
      }
      return;
    }
    if (outcome === 'unknown_device') {
      status.set(`unknown router "${identity || address}"`, 'muted');
      if (!lastPayload) showState('empty', `unknown router "${identity || address}"`);
      return;
    }
    // unreachable / timeout / error: keep the last-good faceplate, surface the state.
    status.set(`${outcome}${detail} — keeping last data`, outcome === 'timeout' ? 'warn' : 'down');
    if (payload.device && !lastPayload) renderSummary(payload.device);
    if (!lastPayload) showState('error', `${outcome}${detail}`);
  }

  async function refresh() {
    if (!address) {
      showState('error', 'no device address');
      return;
    }
    let payload = null;
    try {
      payload = await fetchJson(`/api/phy.json?device=${encodeURIComponent(address)}`);
    } catch (err) {
      status.set(`refresh failing (${err.message}) — keeping last data`, 'down');
      if (!lastPayload) showState('error', `refresh failing (${err.message})`);
      return;
    }
    if (payload.store !== 'ok' || (payload.read?.outcome ?? 'error') !== 'ok') {
      applyNonOk(payload);
      return;
    }
    applyPayload(payload);
  }

  // --- poll gating (D8): the heavy read runs ONLY while the PHY tab is active -------------------
  let timer = 0;
  let inFlight = false;
  let lastReadAt = 0;

  async function doRead() {
    if (inFlight) return; // in-flight guard: a slow read skips ticks, never stacks (FR-015)
    inFlight = true;
    try {
      await refresh();
    } finally {
      inFlight = false;
      lastReadAt = Date.now();
    }
  }

  function startPolling(immediate) {
    if (timer) return;
    if (immediate) doRead();
    timer = setInterval(doRead, REFRESH_MS);
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = 0;
    }
  }

  // 076: the manual refresh is wired via createRefreshButton(() => doRead()) above — it shows the
  // 'reading…' status and disables itself while the read is in flight.

  showState('loading', 'loading…');

  return {
    activate() {
      // First activation reads immediately; a re-activation reads immediately only if the last
      // read is older than the cadence, else it shows held data and waits for the next tick (D8).
      const stale = lastReadAt === 0 || Date.now() - lastReadAt >= REFRESH_MS;
      startPolling(stale);
    },
    deactivate() {
      stopPolling();
    },
    unmount() {
      stopPolling();
      inspector.destroy();
      linkFeed.destroy();
      sfpFeed.destroy();
      poeFeed.destroy();
      l2Feed.destroy();
      neighborFeed.destroy();
      tooltip.destroy();
    },
  };
}
