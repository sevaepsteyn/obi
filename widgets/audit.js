// AUD widget (feature 042) — inspect obi's audit trail via /api/audit.json.
//
// Four views over the local audit event store, all read-only (and — FR-012 — never
// themselves audited): a filterable live event list; one event's full payload (an LLM
// call renders as its conversation, a tool call as args+result, a device command as the
// exact operation); a session trace (the whole conversation / watch cycle in causal
// order); and analytics (token usage over time via the 041 chart primitive, volumes,
// top tools/targets). Payload content is data, never markup — always textContent.

import { createChart } from '../chart.js';
import {
  annotateContext, createCopyButton, createFilterRow, createPoller, fetchJson as apiFetch,
} from '../kit.js';

const POLL_MS = 60 * 1000; // the list keeps up with live activity without manual reload
const KINDS = [
  ['', 'all kinds'], ['llm_call', 'llm_call'], ['tool_call', 'tool_call'],
  ['device_command', 'device_command'], ['human_message', 'human_message'],
  ['report', 'report'], ['action', 'action'],
];
const GLYPHS = {
  human_message: '🗣', llm_call: '🧠', tool_call: '🔧',
  device_command: '📡', report: '📄', action: '·',
};

function localTime(iso) {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
}

export async function mount(host, ctx) {
  const filtersHost = document.createElement('div');
  const content = document.createElement('div');
  content.className = 'audit-content';
  host.style.display = 'flex';
  host.style.flexDirection = 'column';
  host.append(filtersHost, content);

  // nav: {view:'list'} | {view:'detail', id} | {view:'trace', session} | {view:'stats'}
  let nav = { view: 'list' };
  let filters = { kind: '', source: '', outcome: '', window: '24h', text: '' };
  let chart = null;

  function showMessage(text) {
    content.replaceChildren();
    const box = document.createElement('div');
    box.className = 'kit-message'; // 076: shared muted bordered box (was audit-message)
    box.textContent = text;
    content.appendChild(box);
  }

  // 068: URL builder over the shared kit.fetchJson (which does the same no-store fetch +
  // {error}||HTTP-status this widget used to inline).
  const fetchJson = (params) => apiFetch(`/api/audit.json?${params.toString()}`);

  function entryButton(event, onOpen) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'audit-entry';
    const time = document.createElement('span');
    time.textContent = `${localTime(event.ts)}  `;
    const kind = document.createElement('span');
    kind.className = 'k';
    kind.textContent = `${GLYPHS[event.kind] ?? '·'} ${event.kind}`;
    const mark = document.createElement('span');
    mark.className = event.outcome === 'error' ? 'err' : '';
    mark.textContent = event.outcome === 'error' ? ' ✗ ' : '  ';
    const text = document.createElement('span');
    text.className = 't';
    text.textContent = `[${event.source}] ${event.summary}`;
    row.append(time, kind, mark, text);
    row.addEventListener('click', onOpen);
    // 056: right-click an audit event → ask obi about it (this list is custom-rendered, so the
    // row is annotated directly rather than through the kit feed). contextFor composes the text.
    annotateContext(row, {
      contextFor: () => ({
        subject: `audit ${event.kind} #${event.id}`,
        elementText: [
          `time: ${localTime(event.ts)} (${event.ts})`,
          `kind: ${event.kind}`,
          `source: ${event.source}`,
          `target: ${event.target ?? ''}`,
          `outcome: ${event.outcome}${event.error ? ` — ${event.error}` : ''}`,
          `summary: ${event.summary}`,
        ].join('\n'),
      }),
    });
    return row;
  }

  function bar(items) {
    const el = document.createElement('div');
    el.className = 'kit-toolbar'; // 076: shared flex control row (was audit-bar)
    el.append(...items);
    return el;
  }

  function backButton(label, target) {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'kit-btn'; // 076: shared plain text button (was audit-back)
    back.textContent = label;
    back.addEventListener('click', () => {
      nav = target;
      poller.poke();
    });
    return back;
  }

  function renderPayloadBlock(title, text) {
    const block = document.createElement('div');
    block.className = 'audit-payload';
    const role = document.createElement('span');
    role.className = 'role';
    role.textContent = `${title}\n`;
    const bodyText = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
    const body = document.createElement('span');
    body.textContent = bodyText;
    const copyButton = createCopyButton(() => bodyText, { title: 'copy this payload' });
    copyButton.classList.add('audit-copy');
    block.append(copyButton, role, body); // the copy control floats to the block's top-right
    return block;
  }

  function renderDetail(event) {
    content.replaceChildren();
    const session = event.session_id;
    const items = [backButton('← events', { view: 'list' })];
    if (session) items.push(backButton(`session ${session} →`, { view: 'trace', session }));
    content.appendChild(bar(items));

    const detail = document.createElement('div');
    detail.className = 'audit-detail';
    const dl = document.createElement('dl');
    const fact = (label, value) => {
      if (value == null || value === '') return;
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = String(value);
      dl.append(dt, dd);
    };
    fact('event', `#${event.id} — ${event.kind} (${event.source})`);
    fact('time', `${localTime(event.ts)} (${event.ts})`);
    fact('target', event.target);
    fact('outcome', event.outcome + (event.error ? ` — ${event.error}` : ''));
    if (event.duration_ms != null) fact('duration', `${Math.round(event.duration_ms)} ms`);
    if (event.truncated) fact('truncated', `payload bounded (original ${event.original_bytes} B)`);
    fact('summary', event.summary);
    detail.appendChild(dl);

    const payload = event.payload ?? {};
    if (event.kind === 'llm_call') {
      if (payload.system) detail.appendChild(renderPayloadBlock('system', payload.system));
      for (const message of payload.messages ?? []) {
        detail.appendChild(renderPayloadBlock(message.role ?? 'message', message.content));
      }
      for (const block of payload.response ?? []) {
        const title = block.type === 'tool_use' ? `assistant → ${block.name}` : 'assistant';
        detail.appendChild(renderPayloadBlock(title, block.text ?? block.input));
      }
      detail.appendChild(renderPayloadBlock(
        'usage', `${payload.input_tokens ?? 0} in / ${payload.output_tokens ?? 0} out ` +
        `(${payload.stop_reason ?? ''})`));
    } else if (event.kind === 'tool_call') {
      detail.appendChild(renderPayloadBlock(`tool ${payload.tool}`, payload.arguments ?? {}));
      detail.appendChild(renderPayloadBlock('result', payload.result ?? ''));
    } else if (event.kind === 'device_command') {
      detail.appendChild(renderPayloadBlock(
        `${payload.transport} command`, payload.operation ?? ''));
      if (payload.response_excerpt != null) {
        detail.appendChild(renderPayloadBlock('response', payload.response_excerpt));
      }
    } else {
      detail.appendChild(renderPayloadBlock('payload', payload));
    }
    content.appendChild(detail);
    ctx.setBadge(`event #${event.id}`);
  }

  function renderEntries(hostEl, events) {
    for (const event of events) {
      hostEl.appendChild(entryButton(event, () => {
        nav = { view: 'detail', id: event.id };
        poller.poke();
      }));
    }
  }

  async function refresh() {
    try {
      if (nav.view === 'detail') {
        const data = await fetchJson(new URLSearchParams({ view: 'detail', id: nav.id }));
        renderDetail(data.event);
        return;
      }
      if (nav.view === 'trace') {
        const data = await fetchJson(
          new URLSearchParams({ view: 'trace', session: nav.session }));
        content.replaceChildren();
        content.appendChild(bar([
          backButton('← events', { view: 'list' }),
          Object.assign(document.createElement('span'),
            { textContent: `session ${nav.session} — ${data.events.length} event(s)` }),
        ]));
        const list = document.createElement('div');
        list.className = 'audit-list';
        renderEntries(list, data.events);
        content.appendChild(list);
        ctx.setBadge(`trace · ${data.events.length}`);
        return;
      }
      if (nav.view === 'stats') {
        const data = await fetchJson(
          new URLSearchParams({ view: 'stats', since: filters.window || '24h' }));
        content.replaceChildren();
        const stats = document.createElement('div');
        stats.className = 'audit-stats';
        const chartHost = document.createElement('div');
        chartHost.className = 'audit-chart';
        const facts = document.createElement('div');
        facts.className = 'audit-facts';
        stats.append(chartHost, facts);
        content.appendChild(stats);

        chart?.destroy();
        chart = createChart(chartHost);
        const buckets = data.tokens_over_time ?? [];
        chart.render([
          { label: 'tokens in',
            buckets: buckets.map((b) => ({ start: b.t, avg: b.input, n: 1 })) },
          { label: 'tokens out',
            buckets: buckets.map((b) => ({ start: b.t, avg: b.output, n: 1 })) },
        ], { unit: 'tok' });

        const line = (label, value) => {
          const el = document.createElement('div');
          const b = document.createElement('b');
          b.textContent = `${label}: `;
          el.append(b, document.createTextNode(value || 'none'));
          facts.appendChild(el);
        };
        const dict = (obj) => Object.entries(obj ?? {})
          .sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k}=${v}`).join('  ');
        line('tokens', `${data.tokens.total_input} in / ${data.tokens.total_output} out`);
        line('by kind', dict(data.by_kind));
        line('by source', dict(data.by_source));
        line('top tools', (data.top_tools ?? [])
          .map((t) => `${t.tool}×${t.count}`).join('  '));
        line('top targets', (data.top_targets ?? [])
          .map((t) => `${t.target}×${t.count}`).join('  '));
        line('errors', String(data.errors ?? 0));
        ctx.setBadge(`${data.tokens.total_input + data.tokens.total_output} tok · ` +
          `${data.errors} err`);
        return;
      }
      // list view
      const params = new URLSearchParams({ view: 'list', limit: '200' });
      if (filters.kind) params.set('kind', filters.kind);
      if (filters.source) params.set('source', filters.source);
      if (filters.outcome) params.set('outcome', filters.outcome);
      if (filters.text) params.set('text', filters.text);
      if (filters.window) params.set('since', filters.window);
      const data = await fetchJson(params);
      if (!data.store.present) {
        showMessage(data.hint ?? 'no audit events yet');
        ctx.setBadge(null);
        return;
      }
      content.replaceChildren();
      const list = document.createElement('div');
      list.className = 'audit-list';
      if (!data.events.length) {
        showMessage('no matching audit events');
        ctx.setBadge('0 events');
        return;
      }
      renderEntries(list, data.events);
      content.appendChild(list);
      ctx.setBadge(`${data.events.length} of ${data.total}`);
    } catch (err) {
      showMessage(`refresh failing (${err.message}) — retrying`);
    }
  }

  const poller = createPoller(refresh, POLL_MS);

  createFilterRow(filtersHost, [
    { key: 'view', kind: 'select', label: 'view',
      options: [['list', 'events'], ['stats', 'analytics']], value: 'list' },
    { key: 'kind', kind: 'select', label: 'kind', options: KINDS },
    { key: 'source', kind: 'text', label: 'source', placeholder: 'chat / watch / cli:…' },
    { key: 'outcome', kind: 'select', label: 'outcome',
      options: [['', 'all'], ['ok', 'ok'], ['error', 'error']] },
    { key: 'window', kind: 'select', label: 'window',
      options: [['1h', '1h'], ['24h', '24h'], ['7d', '7d'], ['30d', '30d']], value: '24h' },
    { key: 'text', kind: 'text', label: 'search', placeholder: 'free text' },
  ], (values) => {
    filters = values;
    nav = values.view === 'stats' ? { view: 'stats' } : { view: 'list' };
    poller.poke();
  });

  return {
    unmount() {
      poller.stop();
      chart?.destroy();
    },
  };
}
