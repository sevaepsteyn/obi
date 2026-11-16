// 076 screenshot harness — the per-widget mount page's logic. Resolves ?widget=<id> to a real
// registry def (catalog id or minted id, via defForLayoutId — the same resolver the shell uses to
// rebuild a saved layout, so every id form the shell can open is coverable here), then mounts the
// REAL widget module against fixture data with a stub ctx. No stubs of the code under test: the
// whole point (the 075 lesson) is to render the actual widget in a real browser.

import { defForLayoutId } from '/registry.js';

const host = document.getElementById('shot-host');
const strip = document.getElementById('shot-strip');
const errorBox = document.getElementById('shot-error');

function fail(message) {
  errorBox.textContent = message;
  errorBox.style.display = 'block';
  document.documentElement.dataset.shot = 'error';
}

// The strip mirrors the workspace title-bar (mnemonic · name — suffix · badge) so a capture
// reflects the ctx.setName/setTitle/setBadge calls a widget makes on mount.
const view = { name: '', suffix: '', badge: '' };
function paintStrip(mnemonic) {
  const bits = [mnemonic];
  if (view.name) bits.push(view.name);
  const line = bits.join(' · ') + (view.suffix ? ` — ${view.suffix}` : '');
  strip.textContent = view.badge ? `${line}    [${view.badge}]` : line;
}

async function boot() {
  const id = new URLSearchParams(location.search).get('widget');
  if (!id) {
    fail('no ?widget=<id> given');
    return;
  }
  const def = defForLayoutId(id);
  if (!def) {
    fail(`unknown widget id "${id}"`);
    return;
  }
  host.classList.add(`widget-${def.id}`);
  paintStrip(def.mnemonic);

  // The stub ctx: window-mode (so pop-out affordances render as they would in the workspace), the
  // def's own params, title/name/badge painted into the strip, and inert open*/requestClose. Per
  // the D6 lesson, openSnapshot is genuinely ABSENT (not a no-op) — panels branch on ctx.mode.
  const ctx = {
    mode: 'window',
    id: def.id,
    params: def.params ?? null,
    setTitle: (suffix) => { view.suffix = suffix ?? ''; paintStrip(def.mnemonic); },
    setName: (name) => { view.name = name ?? ''; paintStrip(def.mnemonic); },
    setBadge: (text) => { view.badge = text ?? ''; paintStrip(def.mnemonic); },
    openRouter: () => {},
    openConfig: () => {},
    openOspf: () => {},
    openOspfLint: () => {},
    openLink: () => {},
    requestClose: () => {},
  };

  try {
    const module = await import(def.module);
    const handle = await module.mount(host, ctx);
    // Settle: let mount's first render + any rAF-scheduled canvas draw complete before Chrome's
    // virtual-time budget snapshots the page. onResize nudges host-observing canvases to size.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    handle?.onResize?.();
    document.documentElement.dataset.shot = 'ready';
  } catch (err) {
    fail(`the ${def.mnemonic} widget failed to mount:\n${err && err.stack ? err.stack : err}`);
  }
}

boot();
