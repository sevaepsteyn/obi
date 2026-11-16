// Shared net-tool widget (feature 048) — a form over the /api/tool bridge (PING + TRACE).
//
// PING and TRACE are the same shape: pick a source RouterOS device (from the saved discovery
// roster, /api/inventory.json), type a destination, Run. They differ only in the tool name, the
// extra ping "count" field, and the button label — so both widgets are three lines around this
// one helper. mountNetTool builds the form, POSTs {tool, args} to /api/tool (the web tool bridge),
// and shows the tool's own rendered text. No streaming: a run returns when the tool completes
// (bounded by [routeros.live]); a busy state covers the wait. The result pane is textContent only
// (data is never markup — the kit's discipline), and a tool-level error (unreachable device, 100%
// loss, missing arg) is shown as its own message, never a thrown widget.

import { createCopyButton } from './kit.js';

const INVENTORY_URL = '/api/inventory.json';
const TOOL_URL = '/api/tool';

export async function mountNetTool(host, ctx, { tool, verb, withCount = false }) {
  host.innerHTML = `
    <div class="nettool">
      <!-- 076: the control row is the shared kit toolbar layout; nettool-form is kept for its
           form-specific separator (border-bottom) + the .nettool-form submit hook. -->
      <form class="nettool-form kit-toolbar">
        <label class="nettool-field">
          <span class="nettool-label">from</span>
          <select class="nettool-device" aria-label="source device"></select>
        </label>
        <label class="nettool-field nettool-dest-field">
          <span class="nettool-label">to</span>
          <input class="nettool-dest" type="text" autocomplete="off" spellcheck="false"
                 placeholder="address or host" aria-label="destination">
        </label>
        ${withCount ? `<label class="nettool-field">
          <span class="nettool-label">count</span>
          <input class="nettool-count" type="number" min="1" max="20" value="4"
                 aria-label="probe count"></label>` : ''}
        <button type="submit" class="nettool-run kit-btn">${verb}</button><!-- 076: shared button base -->
      </form>
      <div class="pane-tools"></div>
      <pre class="nettool-out" aria-live="polite"></pre>
    </div>`;

  const form = host.querySelector('.nettool-form');
  const deviceSel = host.querySelector('.nettool-device');
  const destInput = host.querySelector('.nettool-dest');
  const countInput = host.querySelector('.nettool-count'); // null when !withCount
  const runButton = host.querySelector('.nettool-run');
  const out = host.querySelector('.nettool-out');
  // Copy the result text (049): reads the pane live, so a re-run always copies the latest output.
  host.querySelector('.pane-tools').appendChild(
    createCopyButton(() => out.textContent, { title: 'copy the result' }));

  // Source dropdown from the saved roster. The option value is a device identity (the tool
  // resolves identity OR address), so an inventory-fetch failure is non-fatal — the operator can
  // still type a device below via the free-text destination + a manually-known source, and the
  // selector explains itself. Built with DOM nodes (never innerHTML) so device names are inert.
  await populateDevices(deviceSel);

  function setBusy(busy) {
    runButton.disabled = busy;
    runButton.textContent = busy ? '…' : verb;
    ctx?.setBadge?.(busy ? 'running…' : '');
  }

  async function run() {
    const device = deviceSel.value.trim();
    const destination = destInput.value.trim();
    if (!destination) {
      showError(out, 'enter a destination (an address or host)');
      destInput.focus();
      return;
    }
    out.classList.remove('error');
    out.textContent = `${verb} ${destination}${device ? ` from ${device}` : ''}…`;
    ctx?.setTitle?.(destination);
    setBusy(true);
    const args = { device, destination };
    if (withCount && countInput) {
      const n = Number.parseInt(countInput.value, 10);
      if (Number.isFinite(n)) args.count = n;
    }
    try {
      const resp = await fetch(TOOL_URL, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ tool, args }),
      });
      const data = await resp.json().catch(() => ({}));
      const text = data?.text ?? data?.error ?? `request failed (${resp.status})`;
      out.textContent = text;
      const failed = !resp.ok || data?.ok === false || String(text).startsWith('error');
      out.classList.toggle('error', failed);
    } catch (err) {
      showError(out, `could not reach obi: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    run();
  });

  return { unmount() {} };
}

async function populateDevices(select) {
  const placeholder = (text) => {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = text;
    select.replaceChildren(opt);
  };
  try {
    const resp = await fetch(INVENTORY_URL, { cache: 'no-store' });
    const payload = await resp.json();
    const entries = payload?.inventory?.entries ?? [];
    const devices = entries
      .filter((e) => e && e.present_in_latest !== false)
      .map((e) => e.identity || e.address)
      .filter(Boolean);
    if (!devices.length) {
      placeholder('(no devices in inventory)');
      return;
    }
    select.replaceChildren(
      ...devices.map((name) => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        return opt;
      }),
    );
  } catch {
    placeholder('(inventory unavailable)');
  }
}

function showError(out, message) {
  out.textContent = message;
  out.classList.add('error');
}
