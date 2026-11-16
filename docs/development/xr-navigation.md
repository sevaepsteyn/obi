# XR viewer navigation — the model and its field lessons

*Normative for the 3D viewer's navigation (`src/obi/web/static/viewer.js` + `nav.js`).
Features 022 → 029 → 080. Read this before touching any gesture, stick, or camera-relative
code in the viewer — every rule here was paid for with an in-headset round that failed.
Current contract: `specs/080-xr-nav/contracts/xr-nav.md` (supersedes the gamepad table of
`specs/029-ar-hands-live/contracts/ar-viewer-nav.md`; the hand-gesture sections there
remain in force).*

## The model in one paragraph

All navigation is a transform of the one `world` group: position = pan, `rotation.y` =
yaw, uniform scale = zoom. Every input — a hand grab or a stick deflection — is a
**gesture**: it begins at a discrete edge (select event, radial-deadzone exit), latches
whatever gaze-derived state it needs ONCE at that edge (anchor point, amplification),
applies deltas while it lasts, and releases cleanly (select end, deadzone re-entry with
hysteresis, tracking loss). Zoom and yaw always go through `applyAboutPoint` — the one
primitive that holds a scene-space point visually fixed — and never write `world.rotation`
or `world.scale` directly. Pure stick math lives in `nav.js` (DOM-free, three-free),
executed for real by `tests/web_harness/nav_headless.mjs`.

## Field lessons (transferable — each generalizes beyond the bug that taught it)

1. **Anchor to content, not to the viewer** (2026-07-06, feature 029). At km scale a
   hand-sized pan moves mountains by centimeters, and a uniform scale about a point at
   the viewer is angularly invariant (2× bigger at 2× the distance renders identically).
   Transforms must anchor to the gaze-ray point nearest the CONTENT, and pans must be
   amplified by anchor-distance / hand-reach.

2. **Latch the anchor once per gesture** (029 for hands; 080 for sticks). An anchor
   re-derived per frame wanders as the transform it feeds changes the answer — "a moving
   anchor feels like swimming", and at close range it snaps between candidates and the
   scene chases it (the 080 "pushed out while zooming" complaint). Any gaze-derived state
   a gesture needs is captured at the gesture's start edge and held to its end.

3. **A yaw sign is tuned to its pivot** (080 round 1). The 022 stick-yaw sign was chosen
   for rotation about the distant map origin; the moment the pivot moved to the
   looked-at content, the same sign read BACKWARDS in-headset. Changing a rotation's
   pivot silently flips the expected input sign — retest direction whenever the pivot
   moves.

4. **Movement must be head-relative, never reference-space-relative** (080). Applying
   stick axes to the session's fixed X/Z axes means "forward" points wherever the
   operator faced at session start — turn your body and the controls rotate out from
   under you. Derive movement bases from the CURRENT camera pose every frame, projected
   to the horizon, with a sticky previous-basis fallback when the projection degenerates
   (near-vertical view) so there is never a NaN or a direction flip.

5. **Flight along the gaze reads as zoom, not travel** (080 round 1 — reversed a
   clarify-time operator decision). At map scale, a forward component down the view
   pitch is perceptually indistinguishable from scaling. Stick movement is strictly
   horizontal; altitude is zoom's job. (Lesson under the lesson: feel decisions made at
   a desk, however carefully asked, are provisional until the headset pass.)

6. **A gaze anchor that only knows one content type fails where the operator looks at
   another** (080 round 1). The anchor walked LINK segments only; looking at the
   mountain FACE, the nearest segment lies kilometers past it, so zoom latched beyond
   the ground and rushed the scene past the operator — while link-dense sites (Showers,
   BRC) felt fine and hid the bug. The anchor distance is now capped by the first
   terrain intersection along the gaze ("never anchor beyond the ground you look at"),
   with the raycast cached ≤150 ms for per-frame callers (a 161×161 site grid is ~51k
   triangles) and fresh at every gesture latch. Visibility is deliberately ignored: in
   AR passthrough the real mountain is present even with the terrain layer off.

7. **Rates are per second, deadzones are radial** (080). Per-frame rate constants change
   feel with the headset's 72/90/120 Hz mode; every rate is `_PER_S` × frame-dt (dt
   clamped to 50 ms so a stall never becomes one giant step). Per-axis deadzone gates
   step from zero to threshold-rate and snap diagonals to axes; the deadzone is radial
   with a smooth `(m − dz)/(1 − dz)` ramp and an engage/release hysteresis gap
   (0.15/0.12) so a threshold-hovering stick can't re-latch a fresh anchor every few
   frames.

8. **Executable tests for the math, tripwires for the wiring** (080). All three shipped
   nav bugs passed every text tripwire — string assertions cannot catch a wrong sign, a
   wandering anchor, or a per-frame rate. Pure math goes in a DOM-free module run by a
   node harness; tripwires only pin that viewer.js is wired to it (and that dead
   patterns stay dead: `world.rotation.y -=`, reference-space pan writes,
   `world.position.y` writes on the stick path).

## Tuning knobs

`PAN_RATE_PER_S` / `YAW_RATE_PER_S` / `ZOOM_RATE_PER_S` and `TERRAIN_HIT_MAX_AGE_MS` at
the top of `viewer.js`; `STICK_ENGAGE` / `STICK_RELEASE` / `DISTANCE_TAU_S` / `DT_MAX_S`
in `nav.js`. All rates were converted from the pre-080 per-frame values at a 90 Hz
baseline; retune in field rounds, never by reintroducing per-frame constants.
