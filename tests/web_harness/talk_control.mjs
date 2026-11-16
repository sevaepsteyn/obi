// Headless state-machine harness for the tap-or-hold talk control (056 R4/R10).
//
// The state machine in createTalkControl is DOM-free by design: capture + transport are
// injectable, so it runs in plain node with a stubbed recorder and a scripted turn stream — no
// AudioContext, no fetch, no DOM. This drives the control through every gesture path and prints
// one JSON line of the emitted state sequences, so the pytest wrapper can assert that NO path
// ends silently (SC-003) and that tap/hold/cap/busy resolve as specified.
//
// Usage: node talk_control.mjs <path-to-voice.js>
// Output (last line): {"tapToggle":[...], "tapCancel":[...], "hold":[...], "cap":[...],
//                      "busy":{"a":[...],"b":[...]}}
import { randomUUID } from 'node:crypto';

const modulePath = process.argv[2];

// node ≥ 19 exposes globalThis.crypto; guarantee randomUUID for newConversationId() regardless.
if (!globalThis.crypto || !globalThis.crypto.randomUUID) {
  globalThis.crypto = { randomUUID };
}

// A manual timer registry: the mode timer (300 ms) and the cap timer are fired explicitly by the
// scenarios, so tap-vs-hold and cap-auto-submit are deterministic (no wall-clock flakiness).
const timers = new Map();
let timerSeq = 1;
globalThis.setTimeout = (fn, ms) => {
  const id = timerSeq++;
  timers.set(id, { fn, ms });
  return id;
};
globalThis.clearTimeout = (id) => {
  timers.delete(id);
};
function fireTimer(ms) {
  for (const [id, timer] of timers) {
    if (timer.ms === ms) {
      timers.delete(id);
      timer.fn();
      return true;
    }
  }
  return false;
}

const { createTalkControl, HOLD_THRESHOLD_MS } = await import(modulePath);

const MAX_SECONDS = 90;

// Drain the microtask queue: the submit path (endAndSubmit/submitText → runTurn → scripted post)
// resolves entirely via microtasks (no real timers), so a handful of turns settles it.
async function flush() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
}

// A scripted turn stream: transcript then a spoken-style answer (with audio, to prove playback is
// gated on the input mode client-side, not on the payload). playAnswer no-ops (audioContext null).
async function scriptedPost(_id, _payload, onEvent, _context) {
  await onEvent({ event: 'transcript', text: 'heard the question' });
  await onEvent({ event: 'answer', text: 'here is the answer', audio_wav_b64: 'QUJDRA==', context_reset: true });
}

function makeFakeCapture() {
  // The shared arbiter for the busy-elsewhere scenario: one owner at a time, exactly like the
  // module arbiter, but with a scripted capture blob and no audio hardware.
  let owner = null;
  return {
    ready: () => true,
    acquire: (token) => {
      if (owner && owner !== token) return false;
      owner = token;
      return true;
    },
    stop: (token) => {
      if (owner !== token) return null;
      owner = null;
      return { blob: { size: 1 }, seconds: 1.2 };
    },
    discard: (token) => {
      if (owner === token) owner = null;
    },
    owner: () => owner,
  };
}

function makeControl({ capture = makeFakeCapture(), ownerLabel = () => 'this window' } = {}) {
  const states = [];
  const control = createTalkControl({
    maxSeconds: MAX_SECONDS,
    ownerLabel,
    capture,
    postAudio: scriptedPost,
    postText: scriptedPost,
    onState: (state, detail) => states.push({ state, detail: detail || {} }),
  });
  return { control, states };
}

const out = {};

// 1) tap → toggle → tap-submit
{
  const { control, states } = makeControl();
  control.press(); // listening (hold expectation while deciding)
  control.release(); // released before the threshold ⇒ toggle (keep listening)
  control.press(); // second tap ⇒ send
  await flush();
  out.tapToggle = states;
}

// 2) tap → Esc discard (visible idle, nothing submitted)
{
  const { control, states } = makeControl();
  control.press();
  control.release();
  control.escapeCancel();
  await flush();
  out.tapCancel = states;
}

// 3) hold ≥ threshold → release-submit (push-to-talk)
{
  const { control, states } = makeControl();
  control.press();
  fireTimer(HOLD_THRESHOLD_MS); // the press outlives the tap window ⇒ hold-PTT
  control.release(); // submit on release
  await flush();
  out.hold = states;
}

// 4) cap timer fires → auto stop+submit (capped visible)
{
  const { control, states } = makeControl();
  control.press();
  fireTimer(MAX_SECONDS * 1000); // the server cap → auto-submit
  await flush();
  out.cap = states;
}

// 5) busy-elsewhere: two controls share one recorder; the loser names the owner
{
  const shared = makeFakeCapture();
  const a = makeControl({ capture: shared, ownerLabel: () => 'window A' });
  const b = makeControl({ capture: shared, ownerLabel: () => 'window B' });
  a.control.press(); // A owns the recorder
  b.control.press(); // B is refused, names A
  await flush();
  out.busy = { a: a.states, b: b.states };
}

console.log(JSON.stringify(out));
