// Router detail widget (feature 047; 075 adds the tab shell) — one router's LIVE, read-only
// drill-down. The window now carries two tabs: L3 (this file's original content — /api/router.json:
// summary + interfaces + IP addresses) and PHY (widgets/phy.js, lazy-imported on first activation —
// /api/phy.json: the physical faceplate + per-port link/SFP/PoE/L2 console). Only the ACTIVE tab
// polls, so a window runs at most one live read cycle per interval (FR-014/FR-015). L3 is the
// default and its behavior is unchanged (SC-007). Each router window is independent and degrades
// honestly — an unreachable device shows a clear state and keeps the last data it had.

import { formatAge, formatUptime, labeledFields } from '../boards.js';
import {
  createFeed, createPoller, createRefreshButton, createStatusLine, createTabs, fetchJson,
  severityColor,
} from '../kit.js';

const REFRESH_MS = 120 * 1000; // slow auto-refresh — a router console is read on demand, never hammered

function ifaceStatus(iface) {
  if (iface.disabled) return 'disabled';
  if (iface.running === true) return 'up';
  if (iface.running === false) return 'down';
  return '';
}

function bytesToMib(n) {
  return n == null ? '' : `${Math.round(n / (1024 * 1024))} MiB`;
}

// --- the L3 pane: the pre-075 router window, moved verbatim into a gated pane -------------------
// Returns a controller: activate() starts its poller (immediate read + interval), deactivate()
// stops it (so it never polls while the PHY tab is showing), destroy() tears it down.
function mountL3(host, ctx, address, getIdentity, setIdentity) {
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';

  // --- toolbar: manual refresh + a live status line (076: the shared kit chrome) -------------
  const toolbar = document.createElement('div');
  toolbar.className = 'kit-toolbar';
  const refreshBtn = createRefreshButton(() => {
    status.set('reading…');
    if (!poller) { poller = createPoller(refresh, REFRESH_MS); return Promise.resolve(); }
    return poller.poke(); // off-cycle read, busy until it settles (respecting the in-flight guard)
  });
  toolbar.append(refreshBtn);
  const status = createStatusLine(toolbar);

  // --- summary header (device facts) ---------------------------------------------------------
  const summary = document.createElement('div');
  summary.className = 'router-summary';

  // --- the two tables ------------------------------------------------------------------------
  const ifLabel = document.createElement('div');
  ifLabel.className = 'router-section';
  ifLabel.textContent = 'interfaces';
  const ifHost = document.createElement('div');
  ifHost.className = 'router-feed';

  const addrLabel = document.createElement('div');
  addrLabel.className = 'router-section';
  addrLabel.textContent = 'ip addresses';
  const addrHost = document.createElement('div');
  addrHost.className = 'router-feed';

  host.append(toolbar, summary, ifLabel, ifHost, addrLabel, addrHost);

  const ifFeed = createFeed(ifHost, {
    tail: false,
    persist: 'router-interfaces', // shared across router windows — the 046 twin-window rule
    columns: [
      { key: 'name', label: 'name', width: '14ch' },
      { key: 'type', label: 'type', width: '10ch' },
      { key: 'status', label: 'status', width: '9ch', color: severityColor },
      { key: 'mac', label: 'mac', width: '17ch' },
      { key: 'mtu', label: 'mtu', width: '6ch', align: 'right' },
      { key: 'comment', label: 'comment' },
    ],
    // 056: right-click an interface row → ask obi about that port on this router.
    contextFor: ({ row, columns }) => ({
      subject: `interface ${row.name} on ${getIdentity() || address}`,
      elementText: labeledFields(row, columns),
    }),
  });
  const addrFeed = createFeed(addrHost, {
    tail: false,
    persist: 'router-addresses',
    columns: [
      { key: 'address', label: 'address', width: '20ch' },
      { key: 'interface', label: 'interface', width: '14ch' },
      { key: 'dynamic', label: 'dynamic', width: '9ch' },
      { key: 'comment', label: 'comment' },
    ],
    contextFor: ({ row, columns }) => ({
      subject: `address ${row.address} on ${getIdentity() || address}`,
      elementText: labeledFields(row, columns),
    }),
  });
  ifFeed.showState('loading', 'loading…');
  addrFeed.showState('loading', 'loading…');

  function renderSummary(device) {
    summary.replaceChildren();
    if (!device) return;
    const pairs = [
      ['address', device.address],
      ['model', device.model],
      ['version', device.version],
      ['uptime', formatUptime(device.uptime_seconds)],
      ['cpu', device.cpu_load_percent == null ? '' : `${device.cpu_load_percent}%`],
      ['free mem', bytesToMib(device.free_memory_bytes)],
    ];
    for (const [label, value] of pairs) {
      if (value === '' || value == null) continue;
      const cell = document.createElement('div');
      cell.className = 'kit-facts'; // 076: shared fact pair (was router-fact — identical style)
      const k = document.createElement('span');
      k.className = 'k';
      k.textContent = label;
      const v = document.createElement('span');
      v.className = 'v';
      v.textContent = String(value);
      cell.append(k, v);
      summary.appendChild(cell);
    }
  }

  let lastGood = false; // once we have rendered real rows, keep them on later failures

  function applyOk(payload) {
    const device = payload.device;
    if (device?.identity && device.identity !== getIdentity()) {
      setIdentity(device.identity);
      ctx.setName?.(device.identity); // fix the window title (restored windows open by address)
    }
    renderSummary(device);
    ifFeed.replace(payload.interfaces.map((iface) => ({
      name: iface.name,
      type: iface.type ?? '',
      status: ifaceStatus(iface),
      mac: iface.mac ?? '',
      mtu: iface.mtu ?? '',
      comment: iface.comment ?? '',
    })));
    if (!payload.interfaces.length) ifFeed.showState('empty', 'no interfaces reported');
    addrFeed.replace(payload.addresses.map((addr) => ({
      address: addr.address,
      interface: addr.interface,
      dynamic: addr.dynamic ? 'yes' : '',
      comment: addr.comment ?? '',
    })));
    if (!payload.addresses.length) addrFeed.showState('empty', 'no addresses reported');
    lastGood = true;
    const age = payload.read?.read_at ? formatAge(payload.read.read_at) : '?';
    status.set(`reachable · read ${age} ago`, 'ok');
    ctx.setTitle?.('reachable');
    ctx.setBadge?.(
      `${payload.interfaces.length} if · ${payload.addresses.length} addr · ${age}`,
    );
  }

  function applyState(message, cls) {
    // A non-ok read: show the reason, keep whatever good data we already had.
    status.set(message, cls);
    ctx.setTitle?.(cls === 'ok' ? 'reachable' : cls);
    if (!lastGood) {
      renderSummary(null);
      ifFeed.showState(cls === 'error' || cls === 'down' ? 'error' : 'empty', message);
      addrFeed.clear();
      addrFeed.showState('empty', message);
    }
  }

  async function refresh() {
    if (!address) {
      applyState('no device address', 'error');
      return;
    }
    let payload = null;
    try {
      payload = await fetchJson(`/api/router.json?device=${encodeURIComponent(address)}`);
    } catch (err) {
      applyState(`refresh failing (${err.message}) — keeping last data`, 'down');
      return;
    }
    const outcome = payload.read?.outcome ?? 'error';
    if (payload.store === 'absent') {
      applyState(payload.hint ?? 'RouterOS not configured', 'muted');
      return;
    }
    if (outcome === 'unknown_device') {
      applyState(`unknown router "${getIdentity() || address}"`, 'muted');
      return;
    }
    if (outcome !== 'ok') {
      // Show the roster-known summary even when the live read failed.
      if (payload.device && !lastGood) renderSummary(payload.device);
      applyState(`${outcome}${payload.read?.detail ? ` — ${payload.read.detail}` : ''}`,
        outcome === 'timeout' ? 'warn' : 'down');
      return;
    }
    applyOk(payload);
  }

  let poller = null;
  // 076: the manual refresh is wired via createRefreshButton above (busy while the read is in flight).

  return {
    activate() {
      if (!poller) poller = createPoller(refresh, REFRESH_MS); // immediate read + interval
    },
    deactivate() {
      poller?.stop();
      poller = null; // never poll router.json while the PHY tab is showing (FR-015)
    },
    destroy() {
      poller?.stop();
      poller = null;
      ifFeed.destroy();
      addrFeed.destroy();
    },
  };
}

export async function mount(host, ctx) {
  const address = ctx.params?.address ?? '';
  let identity = ctx.params?.identity ?? '';

  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';

  const tabHost = document.createElement('div');
  const l3Pane = document.createElement('div');
  l3Pane.className = 'router-pane';
  const phyPane = document.createElement('div');
  phyPane.className = 'router-pane';
  const healthPane = document.createElement('div');
  healthPane.className = 'router-pane';
  host.append(tabHost, l3Pane, phyPane, healthPane);

  // The panes set their own display:flex on mount, which would beat the `hidden` attribute, so we
  // toggle display directly (last-write-wins on the same inline style).
  const showPane = (pane, show) => { pane.style.display = show ? 'flex' : 'none'; };
  showPane(phyPane, false);
  showPane(healthPane, false);

  const l3 = mountL3(l3Pane, ctx, address, () => identity, (value) => { identity = value; });
  let phy = null; // lazy-mounted PHY controller ({ activate, deactivate, unmount })
  let health = null; // 096: lazy-mounted HEALTH controller (same shape)

  createTabs(tabHost, {
    tabs: [
      { id: 'l3', label: 'L3' },
      { id: 'phy', label: 'PHY' },
      { id: 'health', label: 'HEALTH' },
    ],
    persist: 'router-tab', // per-kind: all router windows share the last-selected tab (046 rule)
    onActivate: async (id) => {
      if (id === 'l3') {
        phy?.deactivate?.();
        health?.deactivate?.();
        showPane(phyPane, false);
        showPane(healthPane, false);
        showPane(l3Pane, true);
        l3.activate();
      } else if (id === 'phy') {
        l3.deactivate();
        health?.deactivate?.();
        showPane(l3Pane, false);
        showPane(healthPane, false);
        if (!phy) {
          const mod = await import('./phy.js'); // the heavy PHY code loads only on first PHY open
          phy = await mod.mount(phyPane, { ...ctx, params: { address, identity } });
        }
        showPane(phyPane, true);
        phy.activate?.();
      } else {
        l3.deactivate();
        phy?.deactivate?.();
        showPane(l3Pane, false);
        showPane(phyPane, false);
        if (!health) {
          const mod = await import('./rhealth.js'); // 096: loads only on first HEALTH open
          health = await mod.mount(healthPane, { ...ctx, params: { address, identity } });
        }
        showPane(healthPane, true);
        health.activate?.();
      }
    },
  });

  return {
    unmount() {
      l3.destroy();
      phy?.unmount?.();
      health?.unmount?.();
    },
  };
}
