// TALK widget (feature 037; multi-window + right-click context + tap-or-hold mic, 056).
//
// A pure renderer over the proven voice.js machinery (032/033/056): one createTalkControl per
// mount = one conversation (same in-flight lock, same context semantics), a tap-or-hold mic plus
// a typed ask box, every line in a session-scoped scrollback. No protocol changes beyond the
// additive context part: the NDJSON events of POST /api/turn arrive exactly as they do for the
// headset — this widget just gives them history and a per-window identity.
//
// 056:
//   • many windows coexist (US1); each owns its conversation, transcript, mic, title.
//   • a right-click-seeded window (ctx.params.seed) mounts with a visible, removable context
//     chip (what you see IS what is sent — consent) and is titled `re: <subject>` at once (US2).
//   • the mic is tap-toggle OR press-and-hold, every path visibly stated; the mic can be busy in
//     another window (US3). ownerLabel = this window's live title so a loser can name it.
//   • voice policy (US4) lives in voice.js: a mic-asked answer is spoken + shown; a typed answer
//     is text only.
//
// Degraded states, deliberately distinct (spec FR-021):
//   server voice OFF  -> the whole turn engine is absent (POST /api/turn 404s): explicit
//                        unavailable box naming the fix; mic AND typing disabled.
//   no mic client-side -> typing works fully; the mic explains itself when used.

import {
  captureDeviceLabel,
  captureReady,
  createTalkControl,
  ensureCapture,
  fetchVoiceStatus,
  resumeCapture,
} from '../voice.js';

const MAX_ENTRIES = 200; // drop-oldest scrollback cap (data-model.md: TranscriptEntry)
const TITLE_MAX = 40; // auto-title truncation (research R8) — the window is tellable at a glance
const HINT_MS = 3000; // a transient hint/busy line clears itself if nothing else happens

function truncate(text, max) {
  const collapsed = String(text || '').trim().replace(/\s+/g, ' ');
  return collapsed.length > max ? `${collapsed.slice(0, max - 1)}…` : collapsed;
}

// feature 082: render obi's markdown answer to safe HTML. Answer content is HTML-escaped FIRST so
// it can never inject active markup (INV-4), then the small set of constructs obi emits is
// formatted. Kept dependency-free and inline — the page CSP forbids CDN/vendored markdown libs.
// Exported for the headless harness (tests/web_harness/talk_render.mjs).
export function renderMarkdown(text) {
  const src = String(text ?? '');
  const stash = [];
  const keep = (html) => `\0${stash.push(html) - 1}\0`;
  const escape = (s) =>
    s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');

  let s = escape(src);
  // Code first — protected so no emphasis is applied inside it, and restored last.
  s = s.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_m, code) =>
    keep(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`),
  );
  s = s.replace(/`([^`]+)`/g, (_m, code) => keep(`<code>${code}</code>`));
  // Links: http/https only (text + href were already escaped above).
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_m, label, url) =>
    keep(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`),
  );
  // Emphasis: bold-italic → bold → strikethrough → italic.
  s = s.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/___(.+?)___/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__(.+?)__/g, '<strong>$1</strong>');
  s = s.replace(/~~(.+?)~~/g, '<del>$1</del>');
  s = s.replace(/(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])/g, '<em>$1</em>');
  s = s.replace(/(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])/g, '<em>$1</em>');

  // Block layout: headings → bold line, - / * / + and 1. runs → lists, blank-line-separated
  // paragraphs with intra-paragraph newlines as <br>.
  const lines = s.split('\n');
  const blocks = [];
  let para = [];
  const flushPara = () => {
    if (para.length) {
      blocks.push(`<p>${para.join('<br>')}</p>`);
      para = [];
    }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const heading = line.match(/^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/);
    if (heading) {
      flushPara();
      blocks.push(`<p><strong>${heading[1]}</strong></p>`);
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*[-*+]\s+(.*)$/);
        if (!m) break;
        items.push(`<li>${m[1]}</li>`);
        i++;
      }
      i--;
      blocks.push(`<ul>${items.join('')}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*\d+\.\s+(.*)$/);
        if (!m) break;
        items.push(`<li>${m[1]}</li>`);
        i++;
      }
      i--;
      blocks.push(`<ol>${items.join('')}</ol>`);
      continue;
    }
    if (line.trim() === '') {
      flushPara();
      continue;
    }
    para.push(line);
  }
  flushPara();
  s = blocks.join('');
  // Restore protected code/link spans.
  s = s.replace(/\0(\d+)\0/g, (_m, i) => stash[Number(i)]);
  return s;
}

// feature 089: render the per-turn report (contracts/turn-report.md) as safe HTML — a muted
// token/cost line, expandable (native <details>) into the call list when tools ran. Everything
// is escaped first (same discipline as renderMarkdown); results are literal text, never markup.
// Pure string -> string, exported for the headless harness.
const escapeReport = (s) =>
  String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

function formatTokens(count) {
  const n = Number(count) || 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`;
  return String(n);
}

function formatCost(cost) {
  if (cost === null || cost === undefined) return 'cost unknown';
  const c = Number(cost);
  if (c >= 0.01 || c === 0) return `~$${c.toFixed(2)}`;
  return `~$${c.toFixed(3)}`;
}

function toolSummary(names) {
  const counts = new Map();
  for (const name of names) counts.set(name, (counts.get(name) || 0) + 1);
  return [...counts].map(([name, n]) => (n === 1 ? name : `${name} ×${n}`)).join(', ');
}

export function renderReport(report) {
  if (!report || !report.usage) return '';
  const usage = report.usage;
  const totalIn =
    (Number(usage.input_tokens) || 0) +
    (Number(usage.cache_creation_input_tokens) || 0) +
    (Number(usage.cache_read_input_tokens) || 0);
  const usagePart =
    `${formatTokens(totalIn)} in / ${formatTokens(usage.output_tokens)} out` +
    ` · ${formatCost(report.cost_usd)}`;
  const tools = Array.isArray(report.tools) ? report.tools : [];
  if (!tools.length) {
    // FR-004: a tool-free turn offers no expandable detail — just the token/cost line.
    return `<div class="talk-report-line">${escapeReport(usagePart)}</div>`;
  }
  const summary = `🔧 ${toolSummary(tools.map((t) => t.name))} · ${usagePart}`;
  const calls = tools
    .map((tool, i) => {
      const label = `${i + 1}. ${tool.name}${tool.error ? ' — error' : ''}`;
      let args = '';
      try {
        args = JSON.stringify(tool.arguments ?? {});
      } catch {
        args = String(tool.arguments);
      }
      const note = tool.truncated ? '<div class="talk-report-note">result truncated for display — full text in the audit trail</div>' : '';
      return (
        `<details class="talk-report-call${tool.error ? ' error' : ''}">` +
        `<summary>${escapeReport(label)}</summary>` +
        `<pre class="talk-report-args">${escapeReport(args)}</pre>` +
        `<pre class="talk-report-result">${escapeReport(tool.result)}</pre>` +
        note +
        `</details>`
      );
    })
    .join('');
  return (
    `<details class="talk-report">` +
    `<summary>${escapeReport(summary)}</summary>` +
    calls +
    `</details>`
  );
}

export async function mount(host, ctx) {
  const seed = ctx.params?.seed ?? null; // 056 US2: {subject, contextText} on a right-click chat

  host.innerHTML = `
    <div class="talk">
      <div class="talk-log"></div>
      <div class="talk-form">
        <button type="button" class="talk-mic" title="tap or hold to talk"
                aria-label="tap or hold to talk to obi">🎤</button>
        <button type="button" class="talk-cancel" hidden aria-label="discard the recording"
                title="discard (Esc)">✕</button><!-- 076: keeps its bespoke mic-SIZE round discard
                shape (test_discard_button_matches_the_mic_size); .kit-btn's all:unset would wipe it,
                so only the small chip ✕ below adopts the kit dismiss-button base. -->
        <input type="text" class="talk-ask" autocomplete="off" spellcheck="false"
               placeholder="ask obi — type, or tap the mic" aria-label="ask obi by text">
        <button type="button" class="talk-send" title="send"
                aria-label="send the typed question">⏎</button><!-- phones: some mobile keyboards
                give no usable Enter, so the send affordance must exist on screen too -->
      </div>
    </div>`;
  const log = host.querySelector('.talk-log');
  const micButton = host.querySelector('.talk-mic');
  const cancelButton = host.querySelector('.talk-cancel');
  const askInput = host.querySelector('.talk-ask');
  const sendButton = host.querySelector('.talk-send');

  // The window's live title, reported to voice.js as ownerLabel so a window losing the mic can
  // say "busy in «this title»". Seeded windows are titled at mount; others on the first question.
  let windowTitle = seed ? `re: ${seed.subject}` : 'ask obi';
  let titled = Boolean(seed);
  if (seed) ctx.setName(windowTitle); // FR-005: seeded windows title immediately, never retitle

  function maybeTitle(text) {
    // Auto-title exactly once, from the first submitted question (research R8).
    if (titled) return;
    const name = truncate(text, TITLE_MAX);
    if (!name) return;
    titled = true;
    windowTitle = name;
    ctx.setName(name);
  }

  let statusEl = null; // the one mutable "listening/sending/consulting…" line
  let statusTimer = 0;

  function append(role, text, { error = false } = {}) {
    const entry = document.createElement('div');
    entry.className = `talk-entry ${role}` + (error ? ' error' : '');
    // 082: render obi's markdown answers to safe HTML; operator/status lines stay literal text.
    if (role === 'obi') entry.innerHTML = renderMarkdown(text);
    else entry.textContent = text;
    log.appendChild(entry);
    while (log.children.length > MAX_ENTRIES) log.firstChild.remove();
    log.scrollTop = log.scrollHeight;
    return entry;
  }

  function setStatus(text, { transient = false } = {}) {
    if (!statusEl) statusEl = append('status', text);
    else statusEl.textContent = text;
    log.scrollTop = log.scrollHeight;
    clearTimeout(statusTimer);
    if (transient) statusTimer = setTimeout(clearStatus, HINT_MS);
  }

  function clearStatus() {
    clearTimeout(statusTimer);
    statusEl?.remove();
    statusEl = null;
  }

  function failStatus(message) {
    // Terminal failure: the status line becomes a persistent error entry.
    clearStatus();
    append('status', message, { error: true });
  }

  // --- the context chip (US2): visible, expandable, removable BEFORE the first submit ----------
  let chipConsumed = false;
  let chipEl = null;
  function renderChip() {
    if (!seed) return;
    chipEl = document.createElement('div');
    chipEl.className = 'talk-chip';
    chipEl.innerHTML = `
      <div class="talk-chip-head">
        <button type="button" class="talk-chip-toggle" aria-expanded="false"
                title="show exactly what will be sent">▸</button>
        <span class="talk-chip-subject"></span>
        <button type="button" class="talk-chip-remove kit-btn" aria-label="remove this context">✕</button><!-- 076: shared kit dismiss-button base -->
      </div>
      <pre class="talk-chip-body" hidden></pre>`;
    chipEl.querySelector('.talk-chip-subject').textContent = seed.subject;
    chipEl.querySelector('.talk-chip-body').textContent = seed.contextText; // byte-identical to sent
    const toggle = chipEl.querySelector('.talk-chip-toggle');
    const body = chipEl.querySelector('.talk-chip-body');
    toggle.addEventListener('click', () => {
      const open = body.hidden;
      body.hidden = !open;
      toggle.textContent = open ? '▾' : '▸';
      toggle.setAttribute('aria-expanded', String(open));
    });
    chipEl.querySelector('.talk-chip-remove').addEventListener('click', () => {
      control.clearContext(); // the question goes out with no context payload (FR-011)
      chipEl.remove();
      chipEl = null;
    });
    host.querySelector('.talk').insertBefore(chipEl, log.nextSibling);
  }

  function consumeChip() {
    // The context rode this turn; the chip collapses into the transcript as a muted marker.
    if (chipConsumed) return;
    chipConsumed = true;
    if (chipEl) {
      chipEl.remove();
      chipEl = null;
    }
    if (seed) append('status', `(context attached: ${seed.subject})`);
  }

  const voiceStatus = await fetchVoiceStatus();
  if (!voiceStatus.enabled) {
    const box = document.createElement('div');
    box.className = 'talk-unavailable';
    box.textContent =
      'voice is off on the server — it mounts by default with `obi web serve`; a missing ' +
      'STT/TTS dependency or `[web] voice = false` disables it';
    log.appendChild(box);
    if (seed) renderChip(); // still show what would have been attached (edge case: voice-off seed)
    micButton.classList.add('off');
    micButton.title = 'voice is off on the server';
    askInput.disabled = true;
    askInput.placeholder = 'voice unavailable on the server';
    sendButton.disabled = true;
    return { unmount() {} };
  }

  let echoedQuestion = false; // the operator line for this turn is already in the log
  let turns = 0;
  // 136: the static public demo injects a replay transport (demo/replay.js); live leaves it unset.
  const demo = globalThis.OBI_DEMO?.chatTransport ?? null;

  const control = createTalkControl({
    ...(demo ? { postText: demo.postText } : {}),
    maxSeconds: voiceStatus.maxSeconds,
    ownerLabel: () => windowTitle, // 056 US3: a busy report names THIS window by its live title
    onState: (state, detail) => {
      const listening = state === 'listening';
      micButton.classList.toggle('listening', listening);
      cancelButton.hidden = !listening; // a visible cancel affordance while recording (FR-017)
      switch (state) {
        case 'listening':
          setStatus(
            detail.mode === 'toggle'
              ? 'listening — tap the mic to send (Esc to cancel)'
              : 'listening — release to send',
          );
          break;
        case 'busy-elsewhere':
          setStatus(`mic busy in “${detail.owner}” — finish there first`, { transient: true });
          break;
        case 'hint':
          setStatus(detail.message || 'nothing sent', { transient: true });
          break;
        case 'submitting':
          consumeChip(); // the attached context (if any) rides this turn, then is consumed
          setStatus(detail.capped ? 'sending (hit the cap)…' : 'sending…');
          break;
        case 'thinking':
          if (detail.transcript && !echoedQuestion) {
            // The spoken question, echoed once as an operator line (typed questions are echoed at
            // submit time). The spoken question is also what auto-titles an untitled window.
            echoedQuestion = true;
            clearStatus();
            append('operator', detail.transcript);
            maybeTitle(detail.transcript);
            setStatus('thinking…');
          } else {
            setStatus(detail.note || 'thinking…');
          }
          break;
        case 'speaking': {
          clearStatus();
          if (detail.contextReset && turns > 0) append('status', '(new conversation context)');
          const answerEl = append('obi', detail.text || '');
          // 089: the turn's tool/token/cost report rides under the answer it describes.
          const reportHtml = renderReport(detail.report);
          const addReport = () => {
            if (!reportHtml) return;
            const el = document.createElement('div');
            el.className = 'talk-entry report';
            el.innerHTML = reportHtml;
            log.appendChild(el);
            while (log.children.length > MAX_ENTRIES) log.firstChild.remove();
            log.scrollTop = log.scrollHeight;
          };
          if (demo) demo.typeOut(answerEl).then(addReport); // 136: the recorded answer types out
          else addReport();
          turns += 1;
          break;
        }
        case 'answered':
          clearStatus();
          echoedQuestion = false;
          break;
        case 'idle':
          clearStatus();
          break;
        case 'error':
        case 'unavailable':
          echoedQuestion = false;
          failStatus(detail.message || 'voice failed — try again');
          break;
        default:
          break;
      }
    },
  });

  if (seed) {
    control.attachContext(seed.contextText); // rides the first submit (typed OR spoken)
    renderChip();
  }

  // 136: in the static demo the window auto-plays the recorded preview; the mic is off (typing
  // still reaches the transport, which answers with the demo notice).
  let player = null;
  if (demo) {
    micButton.disabled = true;
    micButton.classList.add('off');
    micButton.title = "voice isn't part of the recorded demo";
    let strip = null;
    player = demo.player({
      submit: (question) => control.submitText(question),
      reset: () => {
        log.replaceChildren();
        statusEl = null;
        echoedQuestion = false;
        turns = 0;
      },
      onChange: (i) => strip?.update(i),
    });
    strip = demo.mountPreviewStrip(host.querySelector('.talk'), player);
    player.play(0);
  }

  // A chat window the operator just opened is ready to type into — the caret starts in the ask box
  // (FR-008, generalized from the seeded window to every launch path: mnemonic, menu, right-click).
  // A window rebuilt from a saved layout at boot passes autofocus:false — nothing the operator did
  // asked for the caret, and several restored chats would race for it.
  if (ctx.autofocus !== false) askInput.focus();

  function submitTyped() {
    const question = askInput.value.trim();
    if (!question) return;
    askInput.value = '';
    append('operator', question);
    echoedQuestion = true; // the transcript event for a typed turn echoes this same text
    maybeTitle(question);
    control.submitText(question);
  }

  askInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') submitTyped();
  });
  // The on-screen send button: pointerdown is prevented so tapping it never steals focus from the
  // ask box — on a phone the keyboard stays up for the follow-up question.
  sendButton.addEventListener('pointerdown', (event) => event.preventDefault());
  sendButton.addEventListener('click', submitTyped);

  // --- the tap-or-hold mic (056 US3) -----------------------------------------------------------
  // First interaction on a fresh page ARMS the mic (grants permission) and stops — no recording
  // starts under the arming gesture (avoids a press/release race). Subsequent gestures record:
  // pointerdown → press (start), pointerup → release (tap⇒keep listening, hold⇒send).
  let arming = false;
  micButton.addEventListener('pointerdown', async (event) => {
    event.preventDefault();
    micButton.setPointerCapture(event.pointerId);
    if (!captureReady()) {
      if (arming) return;
      arming = true;
      try {
        await ensureCapture();
        const device = captureDeviceLabel();
        setStatus(
          `microphone ready${device ? ` (${device})` : ''} — tap or hold the mic to talk`,
          { transient: true },
        );
      } catch (err) {
        failStatus(`microphone unavailable: ${err.message} — typing still works`);
      } finally {
        arming = false;
      }
      return; // this gesture only armed the mic; the next one records
    }
    await resumeCapture(); // the pointerdown gesture unlocks the AudioContext
    control.press();
  });
  micButton.addEventListener('pointerup', () => control.release());
  micButton.addEventListener('pointercancel', () => control.cancel());
  cancelButton.addEventListener('click', () => control.escapeCancel());

  // Escape discards an in-progress toggle capture (FR-017). Only the window currently recording
  // acts — control.escapeCancel() no-ops otherwise, and only one window records at a time, so a
  // page-wide listener is correct and needs no focus bookkeeping.
  function onKeydown(event) {
    if (event.key === 'Escape' && control.isRecording()) control.escapeCancel();
  }
  document.addEventListener('keydown', onKeydown);

  return {
    unmount() {
      document.removeEventListener('keydown', onKeydown);
      clearTimeout(statusTimer);
      player?.stop();
      control.cancel(); // drops any in-progress capture and stops this window's playback
    },
  };
}
