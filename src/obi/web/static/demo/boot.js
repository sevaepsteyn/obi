// Static public demo (feature 136): the bootstrap. It is only ever copied into demo bundles.
//
// The bundle's index.html loads this INSTEAD of importing shell.js directly. Before the
// workspace boots, it:
//   1. reads the bundle manifest and installs the frozen demo clock (clock.js);
//   2. wraps globalThis.fetch. Root-absolute /api/*, /snapshots/*, /voice.json and /healthz
//      requests are answered by api.js from pre-rendered files. Any other root-absolute URL is
//      re-anchored to the bundle root, so the workspace runs under any sub-path (e.g. a GitHub
//      Pages project URL);
//   3. patches Audio, so a radio-clip URL never fetches;
//   4. wires the chat replay when the bundle holds approved recordings (replay.js);
// and only then imports the real shell.js and calls its boot().

import { installDemoClock } from './clock.js';
import * as api from './api.js';

const base = new URL('../', import.meta.url); // the bundle root (this file lives in demo/)
const realFetch = globalThis.fetch.bind(globalThis);

async function loadBundleJson(rel) {
  try {
    const response = await realFetch(new URL(rel, base));
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

const DEMO_PREFIXES = ['/api/', '/snapshots/', '/voice.json', '/healthz'];

export async function bootDemo() {
  const manifest = await loadBundleJson('data/manifest.json');
  if (manifest?.demo_now) installDemoClock(manifest.demo_now);
  api.configure({ load: loadBundleJson, manifest });

  globalThis.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : (input?.href ?? input?.url);
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//')) {
      if (DEMO_PREFIXES.some((p) => url.startsWith(p))) {
        const answer = await api.resolve(url, init ?? {});
        if (answer) return answer;
      }
      return realFetch(new URL(url.slice(1), base), init);
    }
    return realFetch(input, init);
  };

  const RealAudio = globalThis.Audio;
  if (typeof RealAudio === 'function') {
    globalThis.Audio = function DemoAudio(src) {
      return typeof src === 'string' && src.startsWith('/api/') ? new RealAudio() : new RealAudio(src);
    };
  }

  globalThis.OBI_DEMO = { manifest, chatTransport: null, recordings: [] };
  const index = await loadBundleJson('data/chat/index.json');
  if (index?.recordings?.length) {
    const { createReplayTransport } = await import('./replay.js');
    const recordings = (await Promise.all(index.recordings
      .map((r) => loadBundleJson(`data/chat/${r.id}.json`)))).filter(Boolean);
    globalThis.OBI_DEMO.recordings = recordings;
    globalThis.OBI_DEMO.chatTransport = createReplayTransport(recordings);
  }
  return globalThis.OBI_DEMO;
}

await bootDemo();
const shell = await import('../shell.js');
await shell.boot();
