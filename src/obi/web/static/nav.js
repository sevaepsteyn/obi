// nav.js — the pure math behind the XR viewer's thumbstick navigation (080).
//
// One nav model for sticks and hands: a stick deflection is a GESTURE, exactly like a
// 029 grab — it engages when the radial deflection leaves the deadzone, latches whatever
// gaze-derived state it needs once at engage, and releases when the deflection returns.
// The shipped stick code re-derived everything per frame (anchor, direction, speed) and
// all three 2026-07-17 field complaints traced to that; the grab engine's latch
// discipline is the fix, and this module is where its numbers live.
//
// Deliberately DOM-free and import-free (plain {x,y,z} objects, no three.js): the node
// harness (tests/web_harness/nav_headless.mjs) executes this file unstubbed. The shipped
// bugs passed every text tripwire — only running the math catches a remap or smoothing
// error. viewer.js stays the adapter that feeds poses in and applies transforms out.

// Radial deadzone with hysteresis: engage above 0.15, release below 0.12 — the gap keeps
// a threshold-hovering deflection from re-latching a fresh anchor several times a second.
export const STICK_ENGAGE = 0.15;
export const STICK_RELEASE = 0.12;
// Left-stick speed smoothing time constant: the gaze-anchor distance is re-sampled every
// frame (deceleration on approach — the flight-safety mechanism), EMA-smoothed so a
// nearest-link change never steps the speed.
export const DISTANCE_TAU_S = 0.25;
// Frame-delta ceiling: a pipeline stall or app-switch must not become one giant step.
export const DT_MAX_S = 0.05;

// One stick's deflection for one frame, as a gesture-state step. `wasActive` is the
// previous frame's `active`; the caller stores the returned `active` and reacts to the
// `justEngaged` / `justReleased` edges (latch / release its anchor state).
// While active, the output vector is the input direction scaled by a smooth ramp —
// zero at the engage threshold, 1 at full deflection — so motion never step-jumps at the
// deadzone edge. Inside the hysteresis band (release ≤ m < engage) the gesture stays
// active with zero output: the latch holds, nothing moves.
export function remapDeflection(x, y, wasActive) {
  const rawX = Number.isFinite(x) ? x : 0;
  const rawY = Number.isFinite(y) ? y : 0;
  const m = Math.hypot(rawX, rawY);
  const active = wasActive ? m >= STICK_RELEASE : m > STICK_ENGAGE;
  const ramp = active
    ? Math.min(1, Math.max(0, (m - STICK_ENGAGE) / (1 - STICK_ENGAGE)))
    : 0;
  const scale = m > 0 ? ramp / m : 0;
  return {
    active,
    justEngaged: active && !wasActive,
    justReleased: !active && wasActive,
    x: rawX * scale,
    y: rawY * scale,
    magnitude: ramp,
  };
}

// Horizontal movement basis from a camera world axis (the view direction for forward,
// world +X for strafe — field round 1 flattened BOTH: a forward component along the view
// pitch read as zoom/altitude change, so movement never leaves the horizontal plane):
// projected to the horizon and normalized. When the projection collapses (forward while
// looking straight up/down, +X under extreme roll) the previous frame's basis is kept
// (sticky: never NaN, never a direction flip mid-gesture).
export function horizontalBasis(axisWorld, prevBasis) {
  const hx = axisWorld?.x ?? 0;
  const hz = axisWorld?.z ?? 0;
  const length = Math.hypot(hx, hz);
  if (!(length >= 0.05)) return prevBasis;
  return { x: hx / length, y: 0, z: hz / length };
}

// dt-correct exponential moving average: alpha = 1 − e^(−dt/tau), so ten 10 ms steps
// land exactly where one 100 ms step does. `prev = null` seeds with the sample
// (gesture start).
export function smoothDistance(prev, sample, dt, tau) {
  if (prev === null || prev === undefined || !Number.isFinite(prev)) return sample;
  if (!(dt > 0) || !(tau > 0)) return prev;
  return prev + (sample - prev) * (1 - Math.exp(-dt / tau));
}

// Animation-loop timestamp pair → step seconds. First frame (no previous timestamp) and
// clock weirdness (backwards time) are 0 — no motion; stalls clamp to DT_MAX_S.
export function frameDt(nowMs, prevMs) {
  if (!Number.isFinite(nowMs) || !Number.isFinite(prevMs)) return 0;
  return Math.min(DT_MAX_S, Math.max(0, (nowMs - prevMs) / 1000));
}
