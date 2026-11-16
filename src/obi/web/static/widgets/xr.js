// XR widget (feature 037) — the immersive session's setup + launch pad.
//
// This absorbs the 033 landing's 3D setup screen verbatim in FLOW (research R11): the same
// site/eye/mode choices, the same per-mode support gating, and every 032 field lesson around
// microphone staging — the grant happens HERE, before the immersive session (permission
// prompts inside a session are unproven on the headset browser); the capture stream +
// AudioContext stay alive across startAR(); the Enter click is the user gesture that
// resumes a silently re-armed AudioContext. The immersive scene itself (viewer.js) is
// untouched; its renderer canvas attaches to document.body and removes itself on end, so
// the workspace underneath survives the whole trip (spec US2: windows intact).
//
// The old setup page's debug top-down map is gone: MAP is that map now, openable beside
// this widget. The old ?version=3d route redirects to this widget's ?w=xr page.

import { ensureCapture, fetchVoiceStatus, micPermissionGranted, resumeCapture } from '../voice.js';
import { loadBackdropOpaque, saveBackdropOpaque } from '../bands.js';

const XR_HELP =
  'WebXR is unavailable on this browser. The two known causes:\n' +
  '1. This is not the Quest built-in browser (open the URL there).\n' +
  '2. The HTTPS certificate warning was not accepted (reload and accept it) — ' +
  'WebXR only runs on accepted-HTTPS origins.';

// 119 (contract C1/C7): a refused session is now fatal to entry, because there is only ONE
// session type to try. requestSession CONSUMES transient user activation, so we cannot retry
// inside the same click — the operator has to press Enter again for a fresh gesture. Say so.
const XR_RETRY =
  'The headset refused to start the session. Press Enter to try again.';

export async function mount(host, ctx) {
  let alive = true; // session callbacks may outlive the widget (window closed mid-session)

  // 119 note on the two `.note` divs below: the backdrop hint sits above the mic/voice status
  // line, and both want the shared `.xr .note` styling. querySelector('.note') returns whichever
  // comes FIRST in the markup, so the status line carries its own `status-note` class and is
  // addressed by that — otherwise voice status would silently overwrite the backdrop hint.
  host.innerHTML = `
    <div class="xr">
      <div class="setup">
        <label for="xr-site">I am standing at site</label>
        <select id="xr-site"></select>
        <label for="xr-eye">Eye height above local ground (meters)</label>
        <input id="xr-eye" type="number" step="0.1" min="0" value="1.7">
        <label for="xr-mode">Start with</label>
        <select id="xr-mode">
          <option value="vr" selected>VR — opaque background (reality hidden)</option>
          <option value="ar">AR — passthrough (links over reality)</option>
        </select>
        <div class="note backdrop-note">
          Switchable in the headset at any time: gear &rarr; backdrop.
        </div>
        <label class="voice-label" hidden>Voice (talk to obi in the headset)</label>
        <button class="mic" type="button" hidden>Enable microphone</button>
        <button class="enter" type="button">Enter VR</button>
        <div class="note status-note"></div>
      </div>
      <div class="message" hidden></div>
    </div>`;
  const setup = host.querySelector('.setup');
  const sitePicker = host.querySelector('#xr-site');
  const eyeInput = host.querySelector('#xr-eye');
  const modePicker = host.querySelector('#xr-mode');
  const backdropNote = host.querySelector('.backdrop-note');
  const voiceLabel = host.querySelector('.voice-label');
  const micButton = host.querySelector('.mic');
  const enterButton = host.querySelector('.enter');
  const note = host.querySelector('.status-note');
  const message = host.querySelector('.message');

  function showMessage(text, isError) {
    message.hidden = false;
    message.textContent = text;
    message.classList.toggle('error', Boolean(isError));
  }

  function hideMessage() {
    message.hidden = true;
  }

  // --- geometry ------------------------------------------------------------------------------
  let geometry = null;
  try {
    const response = await fetch('links.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    geometry = await response.json();
  } catch (err) {
    setup.hidden = true;
    showMessage(
      `Could not load ${new URL('links.json', location.href)} — ${err}.\n` +
        'No export yet? Run: obi web export (then reopen this widget).',
      true,
    );
    return { unmount() { alive = false; } };
  }

  const siteNames = geometry.sites.map((s) => s.name);
  for (const name of siteNames) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    sitePicker.appendChild(option);
  }
  ctx.setBadge(`${geometry.links.length} links / ${geometry.endpoints.length} radios`);
  // Export freshness stays visible at the entry point (operator decision, 2026-07-06 VR
  // pass: the in-scene freshness card was removed — this line is where the timestamp lives).
  const meta = document.createElement('div');
  meta.className = 'note';
  meta.textContent =
    `exported ${geometry.generated_at} · ${geometry.controller} · ` +
    'in the headset the scene re-reads the export every 5 min';
  setup.appendChild(meta);

  // Prior choices prefill (wrong-site recovery, spec US2 AS-4) — the SAME sessionStorage
  // keys every viewer generation used, so an upgraded device keeps its setup.
  const previous = sessionStorage.getItem('obi-ar-site');
  if (previous && siteNames.includes(previous)) sitePicker.value = previous;
  const previousEye = sessionStorage.getItem('obi-ar-eye');
  if (previousEye) eyeInput.value = previousEye;
  // 119 (contract C6): the backdrop is a normal viewer preference now, persisted alongside the
  // rest of them through bands.js — not the odd-one-out per-tab AR/VR key it used to be, which
  // reset whenever the headset browser tab closed. ONE owner: this picker and the in-session
  // panel row go through the same two helpers, so they cannot disagree. Storage itself stays
  // in bands.js — widgets never reach for it directly (test_web_workspace's owners rule).
  modePicker.value = loadBackdropOpaque() ? 'vr' : 'ar';

  // --- support probe (119 contract C1/C7) -------------------------------------------------------
  // Resolved ONCE at mount, never inside the Enter handler: requestSession consumes transient
  // user activation, so AR support cannot be discovered by trying it and falling back. The
  // probe itself needs no gesture — shell.js runs the same one at workspace boot.
  //
  // The gate is on immersive support AT ALL, not on the picked backdrop: the picker chooses
  // what the session STARTS with, not which session type is requested, so refusing entry
  // because one mode is unsupported would be gating on a question we never ask.
  let canAR = false;
  async function updateModeSupport() {
    canAR = Boolean(
      navigator.xr && (await navigator.xr.isSessionSupported('immersive-ar').catch(() => false)),
    );
    const canVR = Boolean(
      navigator.xr && (await navigator.xr.isSessionSupported('immersive-vr').catch(() => false)),
    );
    enterButton.textContent = modePicker.value === 'ar' ? 'Enter AR' : 'Enter VR';
    const supported = canAR || canVR;
    enterButton.disabled = !supported;
    if (!supported) {
      showMessage(
        navigator.xr ? XR_HELP : 'immersive mode is unavailable on this device — open the ' +
          'workspace on the headset browser to enter the 3D view.\n\n' + XR_HELP,
        true,
      );
      return;
    }
    hideMessage();
    // Without passthrough there is only one backdrop to have. Say so rather than letting the
    // picker promise a mode the device cannot deliver (contract C3's discipline, on the 2D side).
    backdropNote.textContent = canAR
      ? 'Switchable in the headset at any time: gear → backdrop.'
      : 'This device has no passthrough — the session runs on an opaque background.';
  }
  // Only the button label depends on the picker now, so a change needs no re-probe.
  modePicker.addEventListener('change', () => {
    enterButton.textContent = modePicker.value === 'ar' ? 'Enter AR' : 'Enter VR';
  });
  await updateModeSupport();

  // --- microphone staging (032 FR-011, ported unchanged in behavior) ---------------------------
  const voiceStatus = await fetchVoiceStatus();
  voiceLabel.hidden = false;
  if (voiceStatus.enabled) {
    function markMicReady() {
      micButton.hidden = false;
      micButton.textContent = 'Microphone ready';
      micButton.classList.add('granted');
      micButton.disabled = true;
      note.textContent =
        'Microphone ready — in the headset, hold grip (or the talk button) to talk to obi.';
    }
    // The permission grant is one-time per headset, but the capture stream is per page —
    // re-arm it silently on every (re)load once the grant exists (032 field lesson).
    if (await micPermissionGranted()) {
      try {
        await ensureCapture();
        markMicReady();
      } catch (err) {
        /* fall through to the manual step */
      }
    }
    if (micButton.disabled !== true) {
      micButton.hidden = false;
      note.textContent =
        'One-time per headset: enable the microphone BEFORE entering, then hold grip ' +
        '(or the in-scene talk button) to ask obi a question by voice.';
      micButton.addEventListener('click', async () => {
        try {
          await ensureCapture();
          markMicReady();
        } catch (err) {
          note.textContent =
            'Microphone unavailable: ' + err.message + ' — you can still enter; voice will be off.';
        }
      });
    }
  } else {
    note.textContent =
      'Voice is off on the server — it mounts by default with `obi web serve`; a missing ' +
      'STT/TTS dependency or `[web] voice = false` disables it.';
  }

  // --- enter ------------------------------------------------------------------------------------
  enterButton.addEventListener('click', async () => {
    // A silently auto-armed AudioContext starts suspended (autoplay policy) — this click is
    // the user gesture that unlocks it for the whole immersive session.
    await resumeCapture();
    const site = sitePicker.value;
    const eyeHeight = parseFloat(eyeInput.value) || 1.7;
    // 119: the picker sets the STARTING backdrop, not the session type — and it writes the
    // same one preference the in-session row writes, so the two can never disagree (C6).
    // A device with no passthrough only has one backdrop, so don't record a wish for the other.
    const startOpaque = canAR ? modePicker.value !== 'ar' : true;
    sessionStorage.setItem('obi-ar-site', site);
    sessionStorage.setItem('obi-ar-eye', String(eyeHeight));
    saveBackdropOpaque(startOpaque);
    setup.hidden = true;
    ctx.setTitle('in session');
    showMessage(
      `${startOpaque ? 'VR' : 'AR'} session running — look around. Hands: pinch-drag moves the ` +
        'scene, two-hand pinch zooms/rotates, a quick pinch cycles the view mode. ' +
        'Controllers: trigger-drag moves, sticks move/zoom/rotate, A/X cycles the view, ' +
        'B/Y cycles terrain. The top row: settings gear, fleet list, push-to-talk, help. ' +
        'Switch between passthrough and the opaque background any time from gear → backdrop. ' +
        'Exit the session to return to the workspace.',
    );
    try {
      const { startAR } = await import('../viewer.js');
      await startAR(geometry, site, eyeHeight, () => {
        // Session ended -> back to the setup view, prior choices prefilled; the workspace
        // around this window was never unmounted (spec US2 AS-2). 119: re-read the backdrop —
        // the operator may have flipped it in-session, and the picker must agree (C6).
        if (!alive) return;
        modePicker.value = loadBackdropOpaque() ? 'vr' : 'ar';
        enterButton.textContent = modePicker.value === 'ar' ? 'Enter AR' : 'Enter VR';
        setup.hidden = false;
        hideMessage();
        ctx.setTitle(null);
      }, { canAR, startOpaque }, { enabled: voiceStatus.enabled, maxSeconds: voiceStatus.maxSeconds });
    } catch (err) {
      // 119 (C1): NO retry here. requestSession consumed this click's user activation, so a
      // second attempt inside this handler would fail with a security error whatever the real
      // cause was. Hand the setup form back and let the operator press Enter for a fresh one.
      if (!alive) return;
      setup.hidden = false;
      ctx.setTitle(null);
      showMessage(`${XR_RETRY}\n\n${XR_HELP}\n\n(${err})`, true);
    }
  });

  return {
    unmount() {
      alive = false;
    },
  };
}
