// Per-router OSPF drill-down window (feature 052) — one router's slice of the OSPF snapshot.
//
// The 047/051 synthetic-window pattern for OSPF: opened from a map node (ctx.openOspf) or
// `/OSP <device>`, keyed by the device IDENTITY in ctx.params.device. Read-only and device-blind:
// it reads the SAME /snapshots/ospf.json the map polls and filters it to this router — NO new live
// device route (constitution: the drill-down reads the snapshot only). Four sections: neighbors
// (state/uptime/changes), interfaces with costs + template status, instance/redistribute summary,
// and this router's lint findings. Renders on the shared 038/046 table primitive; no bespoke store.

import { createFeed, createPoller } from '../kit.js';

const REFRESH_MS = 5 * 60 * 1000;

// 076: lint severity ('warn'/'info') → the canonical token colour class the category span wears
// (was a `.sev-*` row modifier). Not severityColor() — that maps 'info'→kit-c-ok, not muted.
function lintSeverityToken(severity) {
  return severity === 'warn' ? 'kit-c-warn' : 'kit-c-muted';
}

export async function mount(host, ctx) {
  const device = ctx.params?.device ?? '';
  ctx.setName?.(device);

  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.style.minHeight = '0';
  host.classList.add('ospfr');

  const summary = document.createElement('div');
  summary.className = 'ospfr-summary';

  const nbrLabel = section('neighbors');
  const nbrHost = feedHost();
  const ifLabel = section('interfaces & templates');
  const ifHost = feedHost();
  const lintLabel = section('lint');
  const lintHost = document.createElement('div');
  lintHost.className = 'ospfr-lint';

  host.append(summary, nbrLabel, nbrHost, ifLabel, ifHost, lintLabel, lintHost);

  const nbrFeed = createFeed(nbrHost, {
    tail: false,
    persist: 'ospf-router-neighbors', // shared across drill-down windows (the 046 twin-window rule)
    columns: [
      { key: 'peer', label: 'peer', width: '16ch' },
      { key: 'state', label: 'state', width: '8ch' },
      { key: 'cost', label: 'cost →', width: '7ch', align: 'right' },
      { key: 'rcost', label: 'cost ←', width: '7ch', align: 'right' },
      { key: 'iface', label: 'interface', width: '20ch' },
      { key: 'uptime', label: 'uptime', width: '14ch' },
      { key: 'changes', label: 'chg', width: '5ch', align: 'right' },
    ],
  });
  const ifFeed = createFeed(ifHost, {
    tail: false,
    persist: 'ospf-router-interfaces',
    columns: [
      { key: 'iface', label: 'interface', width: '22ch' },
      { key: 'status', label: 'status', width: '12ch' },
      { key: 'cost', label: 'cost', width: '7ch', align: 'right' },
      { key: 'detail', label: 'detail' },
    ],
  });
  nbrFeed.showState('loading', 'loading…');
  ifFeed.showState('loading', 'loading…');

  function section(text) {
    const div = document.createElement('div');
    div.className = 'ospfr-section';
    div.textContent = text;
    return div;
  }
  function feedHost() {
    const div = document.createElement('div');
    div.className = 'ospfr-feed';
    return div;
  }

  function resolveNode(data) {
    const key = device.toLowerCase();
    return (data.nodes ?? []).find(
      (n) => (n.identity ?? '').toLowerCase() === key
        || n.router_id?.toLowerCase() === key
        || (n.address ?? '').toLowerCase() === key,
    ) ?? null;
  }

  function renderSummary(node, data) {
    summary.replaceChildren();
    if (!node) return;
    const facts = [
      ['router-id', node.router_id],
      ['address', node.address],
      ['enrichment', node.enrichment],
      ['areas', (node.areas ?? []).join(', ')],
    ];
    const inst = node.instance;
    if (inst) {
      if (inst.redistribute) facts.push(['redistribute', inst.redistribute]);
      if (inst.originate_default) facts.push(['originate-default', inst.originate_default]);
      if (inst.out_filter_chain) facts.push(['out-filter-chain', inst.out_filter_chain]);
    }
    if (node.is_asbr) {
      const externals = (data.externals ?? []).filter((e) => e.router_id === node.router_id);
      for (const ext of externals) {
        facts.push(['external', `${ext.prefix} E${ext.metric_type}/${ext.metric}`]);
      }
    }
    if (node.spof) facts.push(['SPOF', 'articulation point']);
    for (const [label, value] of facts) {
      if (value == null || value === '') continue;
      const cell = document.createElement('div');
      cell.className = 'kit-facts'; // 076: shared fact-pair container (was ospfr-fact)
      const k = document.createElement('span'); k.className = 'k'; k.textContent = label;
      const v = document.createElement('span'); v.className = 'v'; v.textContent = String(value);
      cell.append(k, v);
      summary.appendChild(cell);
    }
  }

  function renderNeighbors(node, data) {
    const rows = [];
    for (const edge of data.edges ?? []) {
      const isA = edge.a === node.router_id;
      const isB = edge.b === node.router_id;
      if (!isA && !isB) continue;
      const peer = isA ? edge.b : edge.a;
      const peerNode = (data.nodes ?? []).find((n) => n.router_id === peer);
      rows.push({
        peer: peerNode?.identity ?? peer,
        state: edge.neighbor_state ?? '—',
        cost: (isA ? edge.cost_ab : edge.cost_ba) ?? '',
        rcost: (isA ? edge.cost_ba : edge.cost_ab) ?? '',
        iface: (isA ? edge.iface_a : edge.iface_b) ?? '',
        uptime: edge.adjacency_age ?? '',
        changes: edge.state_changes ?? '',
      });
    }
    nbrFeed.replace(rows);
    if (!rows.length) nbrFeed.showState('empty', 'no formed adjacencies');
  }

  function renderInterfaces(node, data) {
    const rows = [];
    for (const edge of data.edges ?? []) {
      const isA = edge.a === node.router_id;
      const isB = edge.b === node.router_id;
      if (!isA && !isB) continue;
      const iface = (isA ? edge.iface_a : edge.iface_b);
      if (!iface) continue;
      rows.push({
        iface,
        status: `formed (${edge.neighbor_state ?? 'up'})`,
        cost: (isA ? edge.cost_ab : edge.cost_ba) ?? '',
        detail: edge.shared_fate_group ? `shared fate: ${edge.shared_fate_group}` : '',
      });
    }
    for (const ghost of (data.ghosts ?? []).filter((g) => g.router_id === node.router_id)) {
      rows.push({
        iface: ghost.interface,
        status: `ghost · ${ghost.class}`,
        cost: ghost.cost ?? '',
        detail: ghost.detail ?? '',
      });
    }
    ifFeed.replace(rows);
    if (!rows.length) ifFeed.showState('empty', 'no interfaces');
  }

  function renderLint(node, data) {
    lintHost.replaceChildren();
    const findings = (data.lint ?? []).filter((f) => f.router_id === node.router_id);
    if (!findings.length) {
      const clean = document.createElement('div');
      clean.className = 'kit-message'; // 076: shared muted-message box (was ospfr-clean)
      clean.textContent = 'no lint findings for this router';
      lintHost.appendChild(clean);
      return;
    }
    for (const finding of findings) {
      const row = document.createElement('div');
      row.className = 'ospfr-lint-row';
      const cat = document.createElement('span');
      // 076: the category colour is a canonical token on the span (was a `.sev-*` row modifier).
      cat.className = `ospfr-lint-cat ${lintSeverityToken(finding.severity)}`;
      cat.textContent = `${finding.severity} · ${finding.category}`;
      const msg = document.createElement('span');
      msg.className = 'ospfr-lint-msg';
      msg.textContent = finding.message; // data, never markup
      row.append(cat, msg);
      lintHost.appendChild(row);
    }
  }

  function showMessage(text) {
    summary.replaceChildren();
    nbrFeed.clear(); nbrFeed.showState('empty', text);
    ifFeed.clear(); ifFeed.showState('empty', text);
    lintHost.replaceChildren();
  }

  async function refresh() {
    let body = null;
    try {
      const response = await fetch('/snapshots/ospf.json', { cache: 'no-store' });
      if (response.status === 404) { showMessage('no OSPF snapshot yet — run: obi web snapshot'); return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      body = await response.json();
    } catch (err) {
      if (!nbrFeed) return;
      nbrFeed.showState('degraded', `refresh failing (${err.message}) — keeping last data`);
      return;
    }
    const data = body.data;
    const node = resolveNode(data);
    if (!node) {
      showMessage(`unknown router "${device}" in the OSPF snapshot`);
      ctx.setTitle?.('unknown');
      ctx.setBadge?.(null);
      return;
    }
    ctx.setName?.(node.identity ?? node.router_id);
    renderSummary(node, data);
    renderNeighbors(node, data);
    renderInterfaces(node, data);
    renderLint(node, data);
    const nbrCount = (data.edges ?? []).filter(
      (e) => e.a === node.router_id || e.b === node.router_id,
    ).length;
    ctx.setBadge?.(`${nbrCount} adj · ${node.lint_count ?? 0} lint`);
  }

  const poller = createPoller(refresh, REFRESH_MS);

  return {
    unmount() {
      poller.stop();
      nbrFeed.destroy();
      ifFeed.destroy();
    },
  };
}
