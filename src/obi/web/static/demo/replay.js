// Static public demo (feature 136): the chat-preview replay. It is only ever copied into demo
// bundles.
//
// A recording (contracts/recording.md) plays through the REAL chat window: createReplayTransport
// returns a `postText` for createTalkControl. A turn is CUED, then submitted through the live
// control.submitText. The transport answers that question with exactly the live /api/turn NDJSON
// events (transcript → progress per tool step → answer), so the operator bubble, status line,
// markdown and 🔧 report render through the live code paths. Any question the visitor types
// is not cued, and gets the demo `error` event. Nothing here ever touches a network.

export const PROGRESS_CAP_MS = 1500;
export const REVEAL_CAP_MS = 4000;
const REVEAL_MS_PER_CHAR = 18;
const TURN_GAP_MS = 1200;
const FRAME_MS = 40;

export const DEMO_MESSAGE = "This is a recorded preview — live chat isn't part of the demo.";

export function revealMs(text) {
  return Math.min(REVEAL_CAP_MS, (text || '').length * REVEAL_MS_PER_CHAR);
}

const realSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createReplayTransport(recordings, { sleep = realSleep } = {}) {
  let cued = null; // the scripted turn the NEXT matching submit plays

  async function postText(_conversationId, question, onEvent) {
    const turn = cued;
    cued = null;
    if (!turn || question !== turn.question.trim()) {
      await onEvent({ event: 'error', reason: 'demo', message: DEMO_MESSAGE });
      return;
    }
    await onEvent({ event: 'transcript', text: turn.question, typed: true });
    for (const step of turn.steps || []) {
      await sleep(Math.min(Math.max(step.after_ms || 0, 0), PROGRESS_CAP_MS));
      await onEvent({ event: 'progress', tool: step.tool });
    }
    await onEvent({
      event: 'answer', text: turn.answer, context_reset: false, report: turn.report || null,
    });
  }

  // The player: plays recording i's turns in order through `submit` (the window's
  // control.submitText). onChange(index) fires as each recording starts. A newer play() cancels
  // an older one between turns.
  function player({ submit, reset = () => {}, onChange = () => {} }) {
    let index = 0;
    let generation = 0;

    async function play(i = index) {
      if (!recordings.length) return;
      index = ((i % recordings.length) + recordings.length) % recordings.length;
      const mine = ++generation;
      reset();
      onChange(index);
      for (const turn of recordings[index].turns || []) {
        if (mine !== generation) return;
        cued = turn;
        await submit(turn.question.trim());
        await sleep(revealMs(turn.answer) + TURN_GAP_MS);
      }
    }

    return {
      play,
      stop: () => { generation += 1; cued = null; },
      replay: () => play(index),
      next: () => play(index + 1),
      get index() { return index; },
      get count() { return recordings.length; },
    };
  }

  // talk.js reaches the UI helpers through this object; it never imports a demo module.
  return { postText, player, recordings, typeOut, mountPreviewStrip };
}

// The typing animation: reveal an already-rendered answer element's text progressively, over
// revealMs(text). It walks text nodes, so the live markdown structure is kept.
export function typeOut(el, { sleep = realSleep } = {}) {
  const nodes = [];
  const walker = el.ownerDocument.createTreeWalker(el, 4 /* NodeFilter.SHOW_TEXT */);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push([n, n.textContent]);
  const total = nodes.reduce((sum, [, t]) => sum + t.length, 0);
  if (!total) return Promise.resolve();
  for (const [n] of nodes) n.textContent = '';
  const frames = Math.max(1, Math.round(Math.min(REVEAL_CAP_MS, total * REVEAL_MS_PER_CHAR) / FRAME_MS));
  return (async () => {
    for (let f = 1; f <= frames; f++) {
      let budget = Math.ceil((total * f) / frames);
      for (const [n, full] of nodes) {
        n.textContent = full.slice(0, Math.max(0, budget));
        budget -= full.length;
      }
      if (f < frames) await sleep(FRAME_MS);
    }
  })();
}

// "▶ recorded preview · N of M · replay · next", in the kit widget language (kit-toolbar,
// kit-status, kit-btn — docs/development/web-widget-language.md).
export function mountPreviewStrip(container, controller) {
  const doc = container.ownerDocument;
  const strip = doc.createElement('div');
  strip.className = 'kit-toolbar talk-demo-strip';
  const label = doc.createElement('span');
  label.className = 'kit-status';
  const button = (text, onClick) => {
    const b = doc.createElement('button');
    b.type = 'button';
    b.className = 'kit-btn';
    b.textContent = text;
    b.addEventListener('click', onClick);
    return b;
  };
  const next = button('next', () => controller.next());
  next.hidden = controller.count < 2;
  strip.append(label, button('replay', () => controller.replay()), next);
  container.prepend(strip);
  const update = (i) => {
    label.textContent = `▶ recorded preview · ${i + 1} of ${controller.count}`;
  };
  update(controller.index);
  return { update, el: strip };
}
