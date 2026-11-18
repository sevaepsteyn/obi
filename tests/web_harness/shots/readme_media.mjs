#!/usr/bin/env node
// Capture README screenshots and screen recordings of the PUBLIC demo bundle (never the live
// app: only the sanitized demo may appear in published media). Dev tooling, zero dependencies:
// Node's built-in WebSocket drives a locally installed Chrome over the DevTools protocol.
//
//   uv run obi web serve --demo        # http://127.0.0.1:8765/obi/
//   node tests/web_harness/shots/readme_media.mjs shot  <path?query> <out.png>  [waitMs] [WxH] [jsFile]
//   node tests/web_harness/shots/readme_media.mjs video <path?query> <outdir>   [seconds] [WxH] [jsFile]
//
// `video` writes numbered JPEG frames (Page.startScreencast) plus frames.txt with timings, for
// ffmpeg to assemble. An optional jsFile is evaluated in the page last — after OBI_JS and
// OBI_CLICK; in `video`, once the recording has started (to open windows, start a chat replay,
// fly the XR camera, etc.). A string it returns is printed.
//
// Environment (both modes):
//   OBI_LAYOUT=<file.json>  a workspace layout ({"windows": [...]}, the shell's saved-layout
//                           shape) written to localStorage before the page loads; gated widgets
//                           (AUDIT, RADIO) are enabled too
//   OBI_JS=<file.js>        evaluated in the page after load, before the wait (shot) or the
//                           recording (video)
//   OBI_INIT=<file.js>      injected into every document BEFORE its own scripts run (e.g. a
//                           WebXR emulator such as Meta's IWER, for the XR viewer)
//   OBI_ANGLE=<backend>     WebGL backend instead of the software default, e.g. `metal` on macOS —
//                           the 3D scene records at a usable frame rate only on a real GPU
//   OBI_CLICK=<selector>    a real (trusted) mouse click on that element after OBI_JS — for
//                           buttons that need user activation, like entering an XR session
import { spawn } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.env.OBI_DEMO_BASE ?? 'http://127.0.0.1:8765/obi/';
const [mode, target, out, a4, a5, a6] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const size = (s) => (s ?? '1440x900').split('x').map(Number);
const [W, H] = size(mode === 'shot' ? a5 : a5);
const profile = mkdtempSync(join(tmpdir(), 'obi-shots-'));
const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(CHROME, [
  '--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  ...(process.env.OBI_ANGLE
    ? [`--use-angle=${process.env.OBI_ANGLE}`]
    : ['--use-angle=swiftshader', '--enable-unsafe-swiftshader']),
  '--ignore-gpu-blocklist',
  '--hide-scrollbars', '--force-device-scale-factor=2', `--window-size=${W},${H}`,
  '--no-first-run', '--no-default-browser-check', 'about:blank',
], { stdio: 'ignore', detached: true });

async function wsUrl() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((t) => t.type === 'page');
      if (page) return page.webSocketDebuggerUrl;
    } catch { /* not up yet */ }
    await sleep(200);
  }
  throw new Error('chrome did not start');
}

let id = 0;
const pending = new Map();
const listeners = [];
const ws = new WebSocket(await wsUrl());
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  } else if (msg.method) listeners.forEach((fn) => fn(msg));
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const n = ++id;
  pending.set(n, { resolve, reject });
  ws.send(JSON.stringify({ id: n, method, params }));
});

try {
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 2, mobile: false });
  const navigate = async (url) => {
    const loaded = new Promise((r) => listeners.push((m) => m.method === 'Page.loadEventFired' && r()));
    await send('Page.navigate', { url });
    await loaded;
  };
  if (process.env.OBI_INIT) {
    await send('Page.addScriptToEvaluateOnNewDocument', { source: readFileSync(process.env.OBI_INIT, 'utf8') });
  }
  if (process.env.OBI_LAYOUT) {
    await navigate(new URL('about-blank-for-storage', BASE).href); // same origin as the page
    const layout = JSON.parse(readFileSync(process.env.OBI_LAYOUT, 'utf8'));
    await send('Runtime.evaluate', {
      expression: `localStorage.setItem('obi-web-layout', ${JSON.stringify(JSON.stringify({ version: 1, ...layout }))});`
        + `localStorage.setItem('obi-web-gated', 'enabled');`,
    });
  }
  await navigate(new URL(target, BASE).href);
  if (process.env.OBI_JS) {
    await sleep(1500);
    await send('Runtime.evaluate', { expression: readFileSync(process.env.OBI_JS, 'utf8'), awaitPromise: true });
  }
  if (process.env.OBI_CLICK) {
    await sleep(1500);
    const { result } = await send('Runtime.evaluate', {
      expression: `JSON.stringify((() => { const r = document.querySelector(${JSON.stringify(process.env.OBI_CLICK)})?.getBoundingClientRect(); return r && { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })())`,
    });
    const at = JSON.parse(result.value ?? 'null');
    if (!at) throw new Error(`OBI_CLICK: no element matches ${process.env.OBI_CLICK}`);
    for (const type of ['mousePressed', 'mouseReleased']) {
      await send('Input.dispatchMouseEvent', { type, x: at.x, y: at.y, button: 'left', clickCount: 1 });
    }
  }
  const runScript = async (file) => {
    const { result, exceptionDetails } = await send('Runtime.evaluate', {
      expression: readFileSync(file, 'utf8'), awaitPromise: true, returnByValue: true,
    });
    if (exceptionDetails) throw new Error(`${file}: ${exceptionDetails.exception?.description ?? exceptionDetails.text}`);
    if (typeof result?.value === 'string') console.log(result.value);
  };
  if (mode === 'shot') {
    if (a6) {
      await sleep(2500);
      await runScript(a6);
    }
    await sleep(Number(a4 ?? 4000));
    const { data } = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(out, Buffer.from(data, 'base64'));
    console.log(`wrote ${out}`);
  } else if (mode === 'video') {
    mkdirSync(out, { recursive: true });
    const frames = [];
    listeners.push(async (m) => {
      if (m.method !== 'Page.screencastFrame') return;
      const n = frames.length;
      writeFileSync(join(out, `f${String(n).padStart(5, '0')}.jpg`), Buffer.from(m.params.data, 'base64'));
      frames.push(m.params.metadata.timestamp);
      await send('Page.screencastFrameAck', { sessionId: m.params.sessionId });
    });
    await sleep(2500);
    await send('Page.startScreencast', { format: 'jpeg', quality: 85, maxWidth: W * 2, maxHeight: H * 2, everyNthFrame: 1 });
    if (a6) await runScript(a6);
    await sleep(Number(a4 ?? 8) * 1000);
    await send('Page.stopScreencast');
    writeFileSync(join(out, 'frames.txt'), frames.join('\n'));
    console.log(`wrote ${frames.length} frames to ${out}`);
  }
} finally {
  ws.close();
  try { process.kill(-chrome.pid); } catch { /* gone */ }
  await sleep(800);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
