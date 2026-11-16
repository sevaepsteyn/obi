// Headless check of the static-demo chat replay (feature 136, T034).
//
// Drives the REAL createTalkControl (voice.js) with the replay transport (demo/replay.js), using
// an instant, recording sleep. It prints one JSON line of what happened, so the pytest wrapper
// can assert:
//   - the contracts/recording.md event sequence;
//   - the pacing caps;
//   - the demo error for a visitor's own question, with no network call;
//   - replay and next across recordings;
//   - the report rendered by the real renderReport;
//   - the typing reveal.
//
// Usage: node demo_replay.mjs <voice.js> <replay.js> <talk.js>
import { randomUUID } from 'node:crypto';

const [voicePath, replayPath, talkPath] = process.argv.slice(2);
if (!globalThis.crypto || !globalThis.crypto.randomUUID) globalThis.crypto = { randomUUID };
const fetches = [];
globalThis.fetch = async (url) => {
  fetches.push(String(url));
  throw new Error('no network in the demo');
};

const { createTalkControl } = await import(voicePath);
const replay = await import(replayPath);
const { renderReport } = await import(talkPath);

const report = {
  model: 'claude-sonnet-5',
  tools: [{ name: 'uisp_links', arguments: {}, result: 'all up', truncated: false, error: false }],
  usage: { input_tokens: 900, output_tokens: 120, cache_creation_input_tokens: 0,
           cache_read_input_tokens: 0, model_calls: 2 },
  cost_usd: 0.0045,
};
const recordings = [
  { id: 'rec-aaaaaaaa', turns: [
    { question: 'is anything down?', answer: 'All **20** links are up.', report,
      steps: [{ tool: 'uisp_links', after_ms: 99999 }, { tool: 'dash_status', after_ms: 300 }] },
    { question: 'and the backbone?', answer: 'x'.repeat(5000), report: null, steps: [] },
  ] },
  { id: 'rec-bbbbbbbb', turns: [
    { question: 'cpu on core-gw-1?', answer: '7%.', report: null, steps: [] },
  ] },
];

const slept = [];
const sleep = async (ms) => { slept.push(ms); };
const transport = replay.createReplayTransport(recordings, { sleep });

const states = [];
const control = createTalkControl({
  maxSeconds: 30,
  onState: (state, detail) => states.push([state, detail.transcript ?? detail.note ??
                                          detail.text ?? detail.message ?? null]),
  postText: transport.postText,
});

// 1. recording 1, both turns, through the player
const changes = [];
const player = transport.player({
  submit: (q) => control.submitText(q),
  reset: () => states.push(['reset', null]),
  onChange: (i) => changes.push(i),
});
await player.play(0);
const firstPlay = states.splice(0);
const firstSlept = slept.splice(0);

// 2. a visitor types their own question
await control.submitText('can you reboot core-gw-1?');
const visitor = states.splice(0);

// 3. next → recording 2; next again wraps to recording 1; replay stays
await player.next();
const afterNext = player.index;
states.splice(0);
await player.next();
const afterWrap = player.index;
states.splice(0);
await player.replay();
const afterReplay = player.index;

// 4. the report HTML the speaking state carries, through the real renderer
const reportHtml = renderReport(report);

// 5. the typing reveal over a stub DOM: progressive, never exceeding the cap
const texts = [{ textContent: 'All ' }, { textContent: '20' }, { textContent: ' links are up.' }];
const el = { ownerDocument: { createTreeWalker: () => {
  let i = -1;
  return { nextNode: () => texts[++i] ?? null };
} } };
const frames = [];
const revealSleeps = [];
await replay.typeOut(el, { sleep: async (ms) => {
  revealSleeps.push(ms);
  frames.push(texts.map((t) => t.textContent).join(''));
} });

console.log(JSON.stringify({
  firstPlay, firstSlept, visitor, changes, afterNext, afterWrap, afterReplay, reportHtml,
  frames, revealTotal: revealSleeps.reduce((a, b) => a + b, 0),
  revealed: texts.map((t) => t.textContent).join(''),
  fetches, demoMessage: replay.DEMO_MESSAGE,
  caps: { progress: replay.PROGRESS_CAP_MS, reveal: replay.REVEAL_CAP_MS },
  revealLong: replay.revealMs('x'.repeat(5000)),
}));
