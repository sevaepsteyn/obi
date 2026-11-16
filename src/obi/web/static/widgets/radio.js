// RADIO widget (feature 088) — the walkie-talkie channel transcript as a workspace citizen.
//
// A pure renderer over GET /api/radio.json (specs/088-radio/contracts/radio-api.md): the
// store, segmentation, trigger decisions, and the id-cursor tail all live server-side; this
// module wires the kit's feed + filter row + poller to that route (the logs.js shape).
// Timestamps arrive as the STORE's localized ISO strings — zero browser timezone math.
//
// obi's own transmissions are visually distinct (the `who` column carries an accent class),
// and the badge shows the rollout stage (shadow / shadow-tx / live) beside freshness — the
// operator should never wonder which stage the station is in.

import { createFeed, createFilterRow, createPoller, fetchJson } from '../kit.js';
import { formatAge, formatDuration } from '../boards.js';

const TAIL_MS = 20 * 1000;
const AGE_MS = 30 * 1000;
const DISPLAY_CAP = 500;

export async function mount(host, ctx) {
  const filtersHost = document.createElement('div');
  const feedHost = document.createElement('div');
  feedHost.style.flex = '1';
  feedHost.style.minHeight = '0';
  host.append(filtersHost, feedHost);

  let filters = { kind: '', q: '', window: '24h' };
  let cursor = null; // null => next fetch is an initial page (also after filter changes)
  let newestTs = null;
  let stage = null;
  let longWindow = false;

  // One shared player: clicking a row with a clip plays it; clicking again (or another
  // row) stops/switches. Audio never leaves the LAN (constitution 1.13.0).
  let audio = null;
  let playingId = null;
  function stopPlayback() {
    if (audio) { audio.pause(); audio = null; playingId = null; }
  }
  function playRow(row) {
    if (!row.hasClip) return;
    if (playingId === row.id) { stopPlayback(); return; }
    stopPlayback();
    playingId = row.id;
    audio = new Audio(`/api/radio-clip.wav?id=${row.id}`);
    audio.addEventListener('ended', () => { playingId = null; audio = null; });
    audio.play().catch(() => { playingId = null; audio = null; });
  }

  const feed = createFeed(feedHost, {
    columns: [
      { key: 'play', label: '', width: '3ch', color: () => 'radio-play' },
      { key: 'time', label: 'time', width: '11ch', title: 'fullTs', sort: 'fullTs' },
      { key: 'dur', label: 'dur', width: '6ch', align: 'right', sort: 'durMs' },
      // who: obi in accent; a named-on-air speaker in normal text; an alternation-
      // inferred speaker dimmed italic (low confidence). Hover shows the source.
      {
        key: 'who', label: 'who', width: '12ch', title: 'whoTitle',
        color: (v, row) => {
          if (v === 'obi') return 'radio-obi';
          return row && row.speakerConf === 'low' ? 'radio-who-low' : '';
        },
      },
      // Cells are single-line ellipsis; the native tooltip carries the full text.
      { key: 'transcript', label: 'transcript', title: 'transcript' },
      { key: 'mark', label: '', width: '8ch', color: (v) => (v ? 'kit-c-warn' : '') },
    ],
    cap: DISPLAY_CAP,
    persist: 'radio',
    onRowClick: playRow,
    // 056: right-click a transmission → ask obi about it, with the surrounding exchange.
    contextFor: ({ row, rows }) => {
      const index = rows.indexOf(row);
      const near = rows.slice(Math.max(0, index - 3), index + 4);
      const lines = near.map((entry) => {
        const mark = entry === row ? '» ' : '  ';
        return `${mark}${entry.fullTs || entry.time} ${entry.who || 'rx'}: ${entry.transcript}`;
      });
      return { subject: `radio transmission — ${row.who || 'unknown'}`, elementText: lines.join('\n') };
    },
  });
  feed.showState('loading', 'loading the radio store…');

  function toRow(entry) {
    const ts = entry.timestamp || '';
    // who: obi > attributed speaker > trigger-extracted caller > blank.
    const who = entry.kind === 'obi' ? 'obi' : entry.speaker || entry.caller || '';
    let mark = '';
    if (entry.kind === 'obi') mark = entry.outbound_kind || 'tx';
    else if (entry.gate === 'no_speech') mark = 'carrier';  // key click/beep, no voice
    else if (entry.gate !== 'ok') mark = 'garbled';
    else if (entry.trigger) mark = 'call';
    return {
      id: entry.id,
      hasClip: Boolean(entry.has_clip),
      play: entry.has_clip ? '▶' : '',
      time: longWindow ? `${ts.slice(5, 10)} ${ts.slice(11, 19)}` : ts.slice(11, 19),
      fullTs: ts,
      dur: formatDuration(entry.duration_ms),
      durMs: entry.duration_ms ?? 0,
      who,
      speakerConf: entry.speaker_confidence || null,
      // undefined (not '') so the kit's != null check sets no empty tooltip.
      whoTitle: entry.speaker
        ? `${entry.speaker} (${entry.speaker_confidence || 'unknown'})`
        : undefined,
      transcript: entry.transcript || '(unintelligible)',
      mark,
    };
  }

  function updateBadge() {
    const age = newestTs ? `last ${formatAge(newestTs)}` : 'no traffic';
    const prefix = stage ? `${stage} · ` : '';
    ctx.setBadge(`${prefix}${feed.follow ? age : `${age} · tail paused`}`);
  }

  function queryString(extra) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value && !(key === 'window' && extra.after !== undefined)) params.set(key, value);
    }
    if (extra.after !== undefined) params.set('after', String(extra.after));
    return `/api/radio.json?${params.toString()}`;
  }

  async function poll() {
    let payload = null;
    try {
      payload = await fetchJson(queryString(cursor === null ? {} : { after: cursor }));
    } catch (err) {
      feed.showState('degraded', `server unreachable (${err.message}) — retrying`);
      return; // cursor unchanged: recovery neither duplicates nor skips (monotonic ids)
    }
    stage = payload.stage || null;
    if (payload.store === 'absent') {
      feed.clear();
      feed.showState('empty', payload.hint || 'no radio store yet — run: obi radio start');
      newestTs = null;
      updateBadge();
      return;
    }
    const entries = payload.entries;
    if (payload.mode === 'initial') {
      feed.replace(entries.map(toRow));
      if (!entries.length) feed.showState('empty', 'no transmissions match the current filters');
    } else if (entries.length) {
      feed.append(entries.map(toRow));
    }
    cursor = payload.cursor;
    if (entries.length) newestTs = entries[entries.length - 1].timestamp;
    updateBadge();
  }

  const poller = createPoller(poll, TAIL_MS);

  createFilterRow(filtersHost, [
    { key: 'kind', kind: 'select', label: 'kind',
      options: [['', 'channel'], ['rx', 'heard'], ['obi', 'obi'], ['self', 'self (debug)']] },
    { key: 'q', kind: 'text', label: 'text', placeholder: 'search transcripts' },
    { key: 'window', kind: 'select', label: 'window',
      options: [['1h', '1h'], ['6h', '6h'], ['24h', '24h'], ['7d', '7d']],
      value: '24h' },
  ], (values) => {
    filters = values;
    longWindow = values.window === '7d';
    cursor = null;
    feed.showState('loading', 'querying…');
    poller.poke();
  });

  feed.onFollowChange(updateBadge);
  const ageTimer = setInterval(updateBadge, AGE_MS);

  return {
    unmount() {
      stopPlayback();
      poller.stop();
      clearInterval(ageTimer);
      feed.destroy();
    },
  };
}
