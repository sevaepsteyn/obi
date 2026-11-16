// obi AR voice module (feature 032). Self-contained ES module; consumes ONLY the documented
// same-origin contract (GET /voice.json, POST /api/turn — specs/032-ar-voice/contracts/
// ar-voice-api.md, turn protocol per 024's voice-turn-api.md). Capture is the proven 024
// pipeline (AudioWorklet float32 → 16 kHz mono PCM16 WAV — MediaRecorder can't make WAV);
// playback is Web Audio (DOM-independent: there is no visible page inside an immersive
// session, and a stoppable AudioBufferSourceNode gives clean push-to-talk interruption).
//
// The capture stream and AudioContext are acquired on the 2D setup page BEFORE the immersive
// session starts (spec FR-011): the mic permission prompt never has to render in-immersive,
// and the same unlocked AudioContext then records and speaks for the whole visit.

const RATE = 16000;
export const MIN_HOLD_SECONDS = 0.3; // shorter = an accidental tap, discarded locally
// 056 R4: press < this ⇒ tap-toggle; ≥ this ⇒ hold-PTT. Unified with MIN_HOLD_SECONDS (0.3 s):
// a sub-threshold press is a tap (keeps listening), so the old silent-discard-on-short-hold path
// is structurally gone.
export const HOLD_THRESHOLD_MS = MIN_HOLD_SECONDS * 1000;
const CONNECT_TIMEOUT_MS = 2500; // SC-004: "can't reach obi" must be knowable within 3 s

let audioContext = null;
let workletNode = null;
let mediaStream = null;
let chunks = [];
let recording = false;
// 056 R3 (CaptureArbiter): the mic is genuine global hardware — ONE AudioContext/worklet/stream
// for the whole page. With many chat windows the bare `recording` flag becomes an OWNER token:
// exactly one talk control may hold the recorder at a time (`recordOwner`); a second window that
// tries is told the mic is busy in «that window» (its ownerLabel), never silently blocked.
let recordOwner = null;
let playingSource = null;

const workletSource = `
  class Capture extends AudioWorkletProcessor {
    process(inputs) {
      const input = inputs[0];
      if (input.length) this.port.postMessage(input[0].slice(0));
      return true;
    }
  }
  registerProcessor("obi-capture", Capture);
`;

export function newConversationId() {
  return "ar-" + crypto.randomUUID(); // ar- prefix: headset turns are distinguishable in audit
}

export async function fetchVoiceStatus() {
  // The capability probe: {enabled, max_seconds}. Any failure reads as voice-off — the
  // viewer must keep working against a pre-032 server too (spec FR-015).
  try {
    const response = await fetch("/voice.json", { cache: "no-store" });
    if (!response.ok) return { enabled: false, maxSeconds: 90 };
    const status = await response.json();
    return { enabled: !!status.enabled, maxSeconds: status.max_seconds || 90 };
  } catch (err) {
    return { enabled: false, maxSeconds: 90 };
  }
}

export function captureReady() {
  return audioContext !== null;
}

export function captureDeviceLabel() {
  // Which input the browser actually handed us (post-grant, labels are readable). Diagnostic
  // surface: a virtual/asleep device here explains a pure-silence capture instantly.
  return mediaStream?.getAudioTracks?.()[0]?.label ?? "";
}

export async function micPermissionGranted() {
  // Whether the mic permission is ALREADY granted for this origin (→ ensureCapture will not
  // prompt). null = the browser won't say; fall back to the manual enable step.
  try {
    const status = await navigator.permissions.query({ name: "microphone" });
    return status.state === "granted" ? true : status.state === "denied" ? false : null;
  } catch (err) {
    return null;
  }
}

export async function resumeCapture() {
  // Call from a real user gesture (the Enter click): an AudioContext created outside a
  // gesture (the auto-arm path on reload) starts suspended under autoplay policy.
  if (audioContext && audioContext.state === "suspended") await audioContext.resume();
}

export async function ensureCapture() {
  // Run from a user gesture on the 2D page (the Enable-microphone step). Throws with a
  // readable message when the browser can't or the operator declines.
  if (audioContext) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error(
      "this browser cannot capture the microphone — if the address bar shows a " +
        "certificate warning, accept the certificate first (mic needs a secure context)"
    );
  }
  mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  const url = URL.createObjectURL(new Blob([workletSource], { type: "text/javascript" }));
  await audioContext.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  const source = audioContext.createMediaStreamSource(mediaStream);
  workletNode = new AudioWorkletNode(audioContext, "obi-capture");
  workletNode.port.onmessage = (event) => {
    if (recording) chunks.push(event.data);
  };
  source.connect(workletNode);
}

function downsampleTo16k(buffers, fromRate) {
  let total = 0;
  for (const b of buffers) total += b.length;
  const joined = new Float32Array(total);
  let offset = 0;
  for (const b of buffers) {
    joined.set(b, offset);
    offset += b.length;
  }
  if (fromRate === RATE) return joined;
  const outLength = Math.floor((joined.length * RATE) / fromRate);
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const pos = (i * fromRate) / RATE;
    const left = Math.floor(pos);
    const right = Math.min(left + 1, joined.length - 1);
    out[i] = joined[left] + (joined[right] - joined[left]) * (pos - left);
  }
  return out;
}

function encodeWav(samples) {
  // PCM16 mono @ 16 kHz — the contract format
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeString = (o, s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i));
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, RATE, true);
  view.setUint32(28, RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

// --- the capture arbiter (056 R3, data-model CaptureArbiter) ---------------------------------
//
// `token` is the requesting talk control's opaque identity (it carries `ownerLabel()` so a
// losing window can name the winner). Acquire is atomic — single-threaded JS makes the guard a
// plain check. Only the owning token may stop/cancel; the chunk buffer belongs to the owner.

function acquireRecording(token) {
  // → true if `token` now owns the recorder; false if another token already does (busy-elsewhere).
  if (recordOwner && recordOwner !== token) return false;
  if (!audioContext) throw new Error("capture not staged — enable the microphone first");
  stopPlayback(); // starting to talk always silences obi (FR-020 second clause)
  if (audioContext.state === "suspended") audioContext.resume();
  recordOwner = token;
  chunks = [];
  recording = true;
  return true;
}

function stopRecordingOwned(token) {
  // → {blob, seconds} | null (nothing captured / below the accidental-tap floor). Releases owner.
  if (recordOwner !== token) return null;
  recordOwner = null;
  if (!recording) return null;
  recording = false;
  const captured = chunks;
  chunks = [];
  if (!captured.length) return null;
  const samples = downsampleTo16k(captured, audioContext.sampleRate);
  const seconds = samples.length / RATE;
  if (seconds < MIN_HOLD_SECONDS) return null;
  return { blob: encodeWav(samples), seconds };
}

function cancelRecordingOwned(token) {
  if (recordOwner !== token) return;
  recordOwner = null;
  recording = false;
  chunks = [];
}

// The default capture surface a talk control drives (injectable so createTalkControl runs DOM-free
// in the node state-machine harness — 054 pattern, research R10). Production controls all share
// this one module arbiter, which is what makes cross-window exclusion real.
const moduleCapture = {
  ready: () => captureReady(),
  acquire: (token) => acquireRecording(token),
  stop: (token) => stopRecordingOwned(token),
  discard: (token) => cancelRecordingOwned(token),
  owner: () => recordOwner,
};

export async function postTurn(conversationId, wavBlob, onEvent, context = null) {
  // One spoken POST /api/turn, streaming NDJSON events to onEvent. 056: an optional `context`
  // part rides the turn (the right-click screen context the operator attached and can see).
  const form = new FormData();
  form.append("conversation", conversationId);
  form.append("audio", wavBlob, "question.wav");
  if (context) form.append("context", context);
  return streamTurn(form, onEvent);
}

export async function postTextTurn(conversationId, text, onEvent, context = null) {
  // One TYPED POST /api/turn (033): a `text` part instead of `audio` — same event stream,
  // same conversation, same engine. Typed mode needs no microphone at all. 056: same optional
  // `context` part as the spoken path (a seeded chat can be asked by typing or by mic).
  const form = new FormData();
  form.append("conversation", conversationId);
  form.append("text", text);
  if (context) form.append("context", context);
  return streamTurn(form, onEvent);
}

async function streamTurn(form, onEvent) {
  // The abort budget covers only the un-answered wait (connect + headers); once obi is
  // streaming, thinking takes as long as it takes. Throws {unreachable: true} on
  // transport failure/timeout.
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), CONNECT_TIMEOUT_MS);
  let response;
  try {
    response = await fetch("/api/turn", { method: "POST", body: form, signal: abort.signal });
  } catch (err) {
    const failure = new Error("can't reach obi — is `obi web serve` running?");
    failure.unreachable = true;
    throw failure;
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) {
    let message = response.status + " error";
    try {
      message = (await response.json()).error;
    } catch (err) {
      /* non-JSON error body */
    }
    // 404 here = a server without voice mounted (the probe said otherwise, or it restarted).
    if (response.status === 404) message = "voice is not enabled on the server";
    onEvent({ event: "error", reason: "turn_failed", message });
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  while (true) {
    const { done, value } = await reader.read();
    if (value) buffered += decoder.decode(value, { stream: true });
    const lines = buffered.split("\n");
    buffered = done ? "" : lines.pop();
    for (const line of lines) {
      if (line.trim()) onEvent(JSON.parse(line));
    }
    if (done) break;
  }
}

export function stopPlayback() {
  if (playingSource) {
    try {
      playingSource.stop();
    } catch (err) {
      /* already ended */
    }
    playingSource = null;
  }
}

export async function playAnswer(b64) {
  // → resolves when playback ends (or immediately when there is nothing to play).
  if (!b64 || !audioContext) return;
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  let decoded;
  try {
    decoded = await audioContext.decodeAudioData(bytes.buffer);
  } catch (err) {
    return; // undecodable answer audio: the text is on the card (US2 fallback)
  }
  stopPlayback();
  const source = audioContext.createBufferSource();
  source.buffer = decoded;
  source.connect(audioContext.destination);
  playingSource = source;
  return new Promise((resolve) => {
    source.onended = () => {
      if (playingSource === source) playingSource = null;
      resolve();
    };
    source.start();
  });
}

// --- the talk-control state machine (data-model.md: TalkControlState) ----------------------
//
// createTalkControl wires the mechanism; the caller renders. onState(state, detail) fires on
// every transition (056 widened set): idle · listening · submitting · thinking · speaking ·
// answered · error · unavailable · busy-elsewhere · hint. detail carries {mode, transcript,
// note, text, errorKind, message, contextReset, capped, owner, report} as applicable (report:
// the 089 per-turn tool/token/cost object off the answer event, or null).
//
// Two capture idioms share one recorder:
//   • holdStart/holdEnd — raw push-to-talk (the XR squeeze/pinch, 032 semantics, untouched).
//   • press/release — the 2D tap-or-hold machine (R4): press < 300 ms toggles (tap again to
//     send, Esc to discard), press ≥ 300 ms is push-to-talk (submit on release). No gesture
//     ever ends with the window silently idle (SC-003) — every path emits a visible state.
//
// The voice policy (R5) is a client parameter, not a server request: a mic-asked turn plays
// its answer audio; a typed turn is text-only (playback gated on `micAsked`, robust to any
// server vintage — a stale server that still synthesizes for typed turns is gated here).

export function createTalkControl({
  maxSeconds,
  onState,
  ownerLabel = () => "obi",
  capture = moduleCapture,
  postAudio = postTurn,
  postText = postTextTurn,
}) {
  const conversationId = newConversationId();
  const token = { ownerLabel }; // this control's arbiter identity (carries the busy-window label)
  let inFlight = false;
  let recordingHere = false; // this control currently holds the recorder
  let gesture = "idle"; // 'idle' | 'pressing' | 'toggle' — the 2D tap-or-hold machine
  let heldPastThreshold = false; // set by the mode timer: the press outlived the tap window
  let modeTimer = null;
  let capTimer = null;
  let capped = false;
  let pendingContext = null; // 056 US2: attached to the NEXT submitted turn, then consumed

  function emit(state, detail) {
    onState(state, detail || {});
  }

  function takeContext() {
    const context = pendingContext;
    pendingContext = null;
    return context;
  }

  function begin() {
    // Shared capture start behind both idioms. → true if recording started here (else the
    // right unavailable/busy/thinking state was already emitted).
    if (!capture.ready()) {
      emit("unavailable", {
        message: "voice is off — enable the microphone on the setup page, then re-enter",
      });
      return false;
    }
    if (inFlight) {
      // Mirror the server's per-conversation lock locally instead of racing it.
      emit("thinking", { note: "still working on the last question" });
      return false;
    }
    if (recordingHere) return true; // idempotent (a repeated squeezestart)
    let acquired;
    try {
      acquired = capture.acquire(token);
    } catch (err) {
      emit("unavailable", { message: err.message });
      return false;
    }
    if (!acquired) {
      // The mic is busy in another window (FR-018): name it, never start recording here.
      const other = capture.owner();
      const label = other && other.ownerLabel ? other.ownerLabel() : "another window";
      emit("busy-elsewhere", { owner: label });
      return false;
    }
    recordingHere = true;
    capped = false;
    // Stop-and-submit at the server's cap rather than getting 413'd after the fact (FR-016).
    capTimer = setTimeout(() => {
      capped = true;
      endAndSubmit(true);
    }, maxSeconds * 1000);
    return true;
  }

  function dropCapture() {
    recordingHere = false;
    gesture = "idle";
    clearTimeout(modeTimer);
    clearTimeout(capTimer);
    capture.discard(token);
  }

  async function endAndSubmit(micAsked) {
    if (!recordingHere) return;
    recordingHere = false;
    gesture = "idle";
    clearTimeout(modeTimer);
    clearTimeout(capTimer);
    const captured = capture.stop(token);
    if (!captured) {
      // A gesture too short to contain speech: a visible hint, never today's silent discard
      // (FR-015). The recorder is already released.
      emit("hint", { message: "too short — hold a moment longer, or tap the mic and speak" });
      return;
    }
    inFlight = true;
    emit("submitting", { capped });
    await runTurn(
      (onEvent) => postAudio(conversationId, captured.blob, onEvent, takeContext()),
      micAsked,
    );
  }

  // --- raw push-to-talk (the XR squeeze/pinch — 032 semantics, unchanged) --------------------
  function holdStart() {
    if (!begin()) return;
    emit("listening", { mode: "hold" });
  }
  function holdEnd() {
    return endAndSubmit(true);
  }

  // --- the 2D tap-or-hold machine (R4) -------------------------------------------------------
  function press() {
    if (gesture === "toggle") {
      // Second tap of a toggle: send what has been listening.
      endAndSubmit(true);
      return;
    }
    if (!begin()) return;
    gesture = "pressing";
    heldPastThreshold = false;
    // While the tap-vs-hold is being decided, render the hold expectation ("release to send").
    emit("listening", { mode: "hold" });
    modeTimer = setTimeout(() => {
      heldPastThreshold = true;
    }, HOLD_THRESHOLD_MS);
  }
  function release() {
    if (gesture !== "pressing") return; // a toggle's 2nd tap already submitted on press; ignore
    clearTimeout(modeTimer);
    if (heldPastThreshold) {
      endAndSubmit(true); // push-to-talk: held past the threshold, submit on release
    } else {
      gesture = "toggle"; // tap: keep listening; the next tap (or Esc) resolves it
      emit("listening", { mode: "toggle" });
    }
  }

  // Esc / the visible cancel affordance in toggle mode: discard WITHOUT submitting, visibly
  // returning to idle (FR-017).
  function escapeCancel() {
    if (!recordingHere) return;
    dropCapture();
    emit("hint", { message: "discarded — tap the mic to talk" });
  }

  async function submitText(text) {
    // A typed question through the SAME turn machinery (033): same conversation, same
    // states, same in-flight lock. Needs no microphone — typed mode works ungranted. 056 R5:
    // micAsked=false ⇒ the answer is rendered as text only, never played.
    const question = (text || "").trim();
    if (!question) return;
    if (recordingHere) return; // mid-record: never interleave with a capture
    if (inFlight) {
      emit("thinking", { note: "still working on the last question" });
      return;
    }
    inFlight = true;
    stopPlayback();
    emit("submitting", { typed: true });
    await runTurn(
      (onEvent) => postText(conversationId, question, onEvent, takeContext()),
      false,
    );
  }

  async function runTurn(post, micAsked) {
    let sawTerminal = false;
    try {
      await post(async (event) => {
        switch (event.event) {
          case "transcript":
            emit("thinking", { transcript: event.text });
            break;
          case "progress":
            emit("thinking", { note: "consulting " + event.tool + "…" });
            break;
          case "answer":
            sawTerminal = true;
            // 089: the per-turn report (tools + tokens + cost) rides the speaking detail;
            // consumers that don't render it (xr) simply ignore the extra field.
            emit("speaking", {
              text: event.text,
              contextReset: !!event.context_reset,
              report: event.report || null,
            });
            // 056 R5: play answer audio ONLY for mic-asked turns — the policy holds against any
            // server vintage (gated on the input mode, not on the presence of audio_wav_b64).
            if (micAsked) await playAnswer(event.audio_wav_b64);
            emit("answered", { text: event.text });
            break;
          case "gated": {
            sawTerminal = true;
            // The server's measurements + the device we actually captured from: a level of
            // 0.0000 with a real duration = the OS/browser handed us a silent stream (macOS
            // app-level mic permission or a dead default input), not a too-quiet operator.
            const heard = `heard ${event.seconds ?? "?"}s, level ${event.level ?? "?"}`;
            const device = captureDeviceLabel();
            const via = device ? ` via "${device}"` : "";
            emit("error", {
              errorKind: "gated",
              message:
                event.reason === "silent"
                  ? `the microphone delivered pure silence (${heard}${via}) — macOS: allow this ` +
                    "browser under System Settings → Privacy & Security → Microphone, and check " +
                    "the input device + level under Sound settings"
                  : `didn't catch any speech (${heard}${via}) — try again, closer to the mic`,
            });
            break;
          }
          case "error":
            sawTerminal = true;
            emit("error", {
              errorKind: event.reason || "turn_failed",
              message:
                event.reason === "busy"
                  ? "still working on the last question"
                  : event.message,
            });
            break;
          default:
            break; // unknown events are ignored (contract: additive evolution)
        }
      });
      if (!sawTerminal) {
        emit("error", { errorKind: "turn_failed", message: "obi's answer never arrived" });
      }
    } catch (err) {
      emit("error", {
        errorKind: err.unreachable ? "unreachable" : "turn_failed",
        message: err.message,
      });
    } finally {
      inFlight = false;
    }
  }

  function cancel() {
    // Interrupted (session end, input lost, window closed) mid-capture: drop it, never submit.
    if (recordingHere) {
      dropCapture();
      emit("idle", {});
    }
    stopPlayback();
  }

  // 056 US2: seed / clear the context attachment consumed by the next submit (chip lifecycle).
  function attachContext(text) {
    pendingContext = text || null;
  }
  function clearContext() {
    pendingContext = null;
  }

  return {
    holdStart,
    holdEnd,
    press,
    release,
    escapeCancel,
    submitText,
    attachContext,
    clearContext,
    cancel,
    conversationId,
    isRecording: () => recordingHere,
  };
}
