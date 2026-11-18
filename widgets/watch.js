// WATCH board (feature 040) — the autonomous watcher's findings via /api/watch.json (014).
//
// The table shows a few columns; clicking a row opens the full report (the /api/watch.json
// payload already carries description, evidence, likely cause, remediation and investigation
// per finding — the board just doesn't have the width to show them inline).

import { createBoard, labeledFields } from '../boards.js';
import { createCopyButton, createPanel, severityColor } from '../kit.js';

// What each finding's `source` means — the origin that produced it (shown as a column
// tooltip, and in the report header). "llm" is the original judge pass; the rest are the
// deterministic detectors bolted on since (027/030/034).
const SOURCE_HELP = {
  llm: 'LLM judge read the device syslog window and flagged this',
  metrics: 'Deterministic: an interface error/discard counter is climbing (SNMP, 027)',
  reachability: 'Deterministic: the device stopped reporting SNMP metrics — went dark (030)',
  'uisp-links': 'Deterministic: a UISP wireless link went down or its signal dropped (034)',
  config: 'Deterministic: the device\'s stored config changed vs the last watch baseline (026/070)',
  icmp: 'Deterministic: the device stopped answering host-originated ICMP pings (072)',
  configs: 'Deterministic: obi can no longer capture the device\'s config over SSH (085/087)',
  health: 'Deterministic: a hardware-health series breached its threshold — temp/PSU/fan/optics/CPU/memory/disk (096)',
};

function fmtTs(iso) {
  // Tidy the store's ISO string for display without any timezone math: "2026-07-11 14:23:45".
  return iso ? String(iso).replace('T', ' ').slice(0, 19) : '';
}

function reportTitle(finding) {
  const sev = (finding.severity || '').toUpperCase();
  return `${sev}: ${finding.summary || finding.device || 'finding'}`;
}

function section(title, node) {
  const wrap = document.createElement('div');
  wrap.className = 'wf-section';
  const heading = document.createElement('div');
  heading.className = 'wf-section-title';
  heading.textContent = title;
  wrap.append(heading, node);
  return wrap;
}

function para(text) {
  const el = document.createElement('div');
  el.className = 'wf-text';
  el.textContent = text;
  return el;
}

function renderReport(finding) {
  const root = document.createElement('div');
  root.className = 'wf-report';

  const dl = document.createElement('dl');
  dl.className = 'wf-meta';
  const fact = (label, value) => {
    if (value == null || value === '') return;
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = String(value);
    dl.append(dt, dd);
  };
  const addr = finding.device_address ? ` (${finding.device_address})` : '';
  fact('device', `${finding.device}${addr}`);
  fact('severity', finding.severity);
  fact('confidence', finding.confidence);
  fact('source', `${finding.source}${SOURCE_HELP[finding.source] ? ` — ${SOURCE_HELP[finding.source]}` : ''}`);
  fact('detected', fmtTs(finding.created_at));
  if (finding.window_start || finding.window_end) {
    fact('window', `${fmtTs(finding.window_start)} → ${fmtTs(finding.window_end)}`);
  }
  root.appendChild(dl);

  root.appendChild(section('What happened', para(finding.description || finding.summary || '')));

  const evidence = finding.evidence ?? [];
  if (evidence.length) {
    const list = document.createElement('ul');
    list.className = 'wf-evidence';
    for (const ev of evidence) {
      const item = document.createElement('li');
      const meta = [ev.host, ev.at ? fmtTs(ev.at) : null].filter(Boolean).join(' · ');
      item.textContent = `[${ev.kind ?? 'log'}] ${ev.text ?? ''}${meta ? `  — ${meta}` : ''}`;
      list.appendChild(item);
    }
    root.appendChild(section('Evidence', list));
  }

  if (finding.likely_cause) {
    root.appendChild(section('Possible cause (not verified)', para(finding.likely_cause)));
  }
  if (finding.suggested_remediation) {
    root.appendChild(section(
      'Suggested manual remediation (NOT performed by obi)', para(finding.suggested_remediation)));
  }

  const investigation = finding.investigation ?? [];
  if (investigation.length) {
    const pre = document.createElement('pre');
    pre.className = 'wf-invest';
    pre.textContent = investigation.map((step) => JSON.stringify(step, null, 2)).join('\n\n');
    root.appendChild(section('Investigation', pre));
  }

  const note = document.createElement('div');
  note.className = 'wf-note';
  note.textContent =
    'obi watch is read-only: it identifies and reports problems; it never changes device state.';
  root.appendChild(note);

  // Copy the whole report (049): a finding is immutable, so snapshot the assembled section text
  // (children joined by newlines) before prepending the control that shouldn't copy itself.
  const reportText = Array.from(root.children).map((child) => child.textContent).join('\n');
  const tools = document.createElement('div');
  tools.className = 'pane-tools';
  tools.appendChild(createCopyButton(() => reportText, { title: 'copy this report' }));
  root.prepend(tools);

  return root;
}

export async function mount(host, ctx) {
  // 076: the finding report is a modal panel (kit.createPanel) — its long-form reading mode, the
  // board-detail semantics generalized. No popOut declared ⇒ the pop-out button offers a snapshot.
  const panel = createPanel(host, ctx, { modal: true });
  const board = createBoard(host, ctx, {
    source: { kind: 'api', url: '/api/watch.json' },
    columns: [
      { key: 'time', label: 'time', width: '12ch' },
      { key: 'device', label: 'device', width: '18ch' },
      { key: 'severity', label: 'sev', width: '9ch', color: severityColor },
      { key: 'source', label: 'source', width: '13ch', title: '_source_help' },
      { key: 'summary', label: 'summary' },
    ],
    filters: [
      { key: 'device', kind: 'text', label: 'device', placeholder: 'substring' },
      { key: 'severity', kind: 'select', label: 'sev',
        options: [['', 'all'], ['info', 'info'], ['warning', 'warning'],
                  ['critical', 'critical']] },
      { key: 'window', kind: 'select', label: 'window',
        options: [['24h', '24h'], ['7d', '7d'], ['30d', '30d']], value: '24h' },
    ],
    query: (values) => {
      const params = new URLSearchParams();
      for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
      return `/api/watch.json?${params.toString()}`;
    },
    // `_finding` (the whole payload) and `_source_help` (the tooltip) are row metadata: the
    // leading underscore keeps them out of the visible columns and the text filter (boards.js).
    rows: (payload) => payload.findings.map((finding) => ({
      time: `${finding.created_at.slice(5, 10)} ${finding.created_at.slice(11, 16)}`,
      device: finding.device,
      severity: finding.severity,
      source: finding.source ?? '',
      summary: finding.summary,
      _source_help: SOURCE_HELP[finding.source] ?? null,
      _finding: finding,
    })),
    onRowClick: (row) => {
      if (row._finding) panel.open({ title: reportTitle(row._finding), body: renderReport(row._finding) });
    },
    // 056: right-click a finding → ask obi about it; the visible fields ride the question (obi can
    // re-query the finding's full report through its own read-only tools).
    contextFor: ({ row, columns }) => ({
      subject: `${row.severity} finding — ${row.device}`,
      elementText: labeledFields(row, columns),
    }),
    counts: (payload) => `${payload.findings.length} finding(s)`,
    age: (payload) => payload.findings[0]?.created_at ?? null,
  });

  return {
    unmount() {
      panel.destroy();
      board.unmount();
    },
  };
}
