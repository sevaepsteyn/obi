// obi AR link viewer scene (features 022 + 025 + 029 + 031) — three.js WebXR, passthrough
// AR or opaque VR (mode picked on the setup page; VR is the default).
//
// Terrain (031, contracts/ar-viewer-terrain.md): terrain.json is fetched ONCE per session
// (after the XR session starts — never polled, and never inside the requestSession
// user-activation window). Ground profiles join links by id and rebuild with every live
// refresh; the wireframe + peak labels live in one `terrain` group (world child, so 029
// navigation applies untouched) toggled by the B/Y face button. The only math added is
// the two contract-blessed affine maps: profile sample i of n sits at the horizontal lerp
// of the endpoints at height samples_amsl[i] − site.ground_elevation_m, and grid post
// (i, j) at its row-major ENU cell — no geodesy, no physics, verdicts consumed verbatim.
//
// This file contains NO geodesy or physics (contracts/ar-viewer.md, ar-viewer-fresnel.md,
// ar-viewer-nav.md): endpoint positions come verbatim from the export's per-site ENU tables
// and Fresnel radii verbatim from the export's fresnel_radius_m; the only coordinate handling
// is the fixed transposition   scene.x = east, scene.y = up, scene.z = -north
// (three.js is right-handed with -Z forward ⇒ north maps to -Z), validated against
// tests/fixtures/ar/worked-example.json.
//
// Navigation (029, sticks reworked by 080) lives entirely on the `world` group: position =
// pan, rotation.y = yaw, uniform scale = zoom. One grab-gesture engine serves hands and
// controllers alike, driven by the WebXR select events (a hand pinch and a controller
// trigger fire the same primary action): one grab pans, two grabs zoom + yaw, a quick tap
// toggles the Fresnel zones — the same action as the A/X face button (025 FR-008), which
// stays. Thumbsticks follow the same latch discipline as grabs (080, contracts/xr-nav.md):
// a deflection out of the radial deadzone is ONE gesture — left stick moves in the
// HORIZONTAL plane relative to the operator's current facing (altitude never changes;
// approach the ground with zoom), right stick zooms + yaws about the gaze anchor latched
// when the deflection began (capped by the terrain hit — never anchored beyond the ground
// being looked at). Pure stick math lives in nav.js; rates are per second (scaled by the
// frame delta), so feel is identical across the 72/90/120 Hz refresh modes.
//
// Field lesson (2026-07-06 headset pass): at km scale, hand-sized transforms are invisible —
// a 1:1 pan moves mountains by centimeters, and a uniform scale about a point at the viewer
// is angularly invariant (a 2x bigger link 2x farther away renders identically). Gestures
// therefore anchor to the CONTENT: zoom/yaw happen about the gaze-ray point nearest a link
// (what you look at magnifies in place) and pans are amplified by anchor-distance/hand-reach
// (drag the map under your gaze, phone-style), which converges back to ~1:1 once the scene
// is at tabletop scale.

import * as THREE from 'three';
import * as nav from './nav.js';
import { buildCityGeometry, fenceRing, makeHeightSampler, maskWireIndex } from './brc.js';
import { createTalkControl } from './voice.js';
import {
  backdropAvailable, bandOf, bandsPresent, canonicalLinkName, capacityLine, CLEARANCE_COLORS,
  coreScale, formatRate, HEALTH_COLORS_3D as HEALTH_COLORS, linkKey, linkListPage, linkRadius3d,
  loadBackdropOpaque, loadHiddenBands, loadViewerPref,
  MAINTENANCE_COLOR_3D as MAINTENANCE_COLOR, modeColor3d, MODE_FRESNEL,
  MODE_HEALTH, MODE_SIGNAL, MODE_UTILIZATION, saveBackdropOpaque, saveHiddenBands, saveViewerPref,
  SIGNAL_RAMP, terrainStyleDefaultFor, terrainStyleKeyFor,
  USAGE_RAMP, usageFraction, usagePercent, VIEW_MODES,
} from './bands.js';

// 063: the palette (0x ints for three.js) is derived in bands.js from the one string palette both
// surfaces share, and aliased to the local names here so every usage site below stays unchanged.

// Band visibility persists per surface (044 FR-005) — this key never crosses to viewer2d.js.
const BANDS_KEY = 'obi.viewer.bands';

// 078 XR link views (contracts/view-modes.md): the color view mode + the two scene toggles now
// persist per XR surface via the shared bands.js JSON pref helpers. First-run defaults match
// today's session behavior — health mode, zones off, terrain on (FR-017).
const MODE_KEY = 'obi.viewer.mode';
const TERRAIN_KEY = 'obi.viewer.terrain';
// (obi.viewer.zones is retired — Fresnel zones are the fourth VIEW mode now, carried by MODE_KEY;
// a stale stored zones pref is simply never read again.)

// Utilization inner core (078 US3, R10): a white-hot "fill level" inside the capacity pipe,
// leaning toward the active mode's link color as the pipe saturates.
const CORE_COLOR = 0xf8fafc;

// 080: stick rates are per SECOND, applied × the frame delta, so identical input over
// identical wall time moves the scene identically at 72/90/120 Hz. Values are the ×90
// conversion of the pre-080 per-frame rates (the familiar Quest-3 feel is the baseline);
// deadzone/hysteresis thresholds live in nav.js beside the remap math.
const YAW_RATE_PER_S = 1.8;   // radians per second at full stick deflection
const PAN_RATE_PER_S = 0.54;  // fraction of the smoothed gaze distance per second
const ZOOM_RATE_PER_S = 1.8;  // ln(scale) per second at full stick deflection

// Gaze-content anchoring (field lesson above): gestures act on the link nearest the gaze
// ray. GRAB_REACH_M is the nominal head-to-hand distance — the pan amplification is
// anchor-distance / reach, so dragging a link 5 km away slides the world by kilometers
// while a tabletop-scale scene pans near 1:1.
const GRAB_REACH_M = 0.5;
const ANCHOR_MIN_M = 1;       // never anchor inside the head
const ANCHOR_MAX_M = 20000;   // beyond the farthest link, amplification stops growing
const ANCHOR_CONE_RAD = 0.35; // links within ~20 deg of gaze are "looked at"
const TERRAIN_HIT_MAX_AGE_MS = 150; // flight-speed staleness budget for the terrain raycast

// Grab gestures (029, contracts/ar-viewer-nav.md): a select that ends fast and near where
// it started is a tap (zone toggle), anything longer/farther was a drag. Drags apply live —
// a tap's sub-3 cm accidental pan is imperceptible at terrain scale (research R4).
const TAP_MS = 250;           // max press duration for a tap
const TAP_MOVE_M = 0.03;      // max ray-origin travel for a tap
const SCALE_MIN = 0.001;      // 20 km of terrain shrinks to a 20 m tabletop, never to zero
const SCALE_MAX = 20;
const PAN_MAX_M = 50000;      // the scene is always recoverable (029 FR-007)

// Fresnel zones (025, contracts/ar-viewer-fresnel.md): neutral color; opacities tuned so
// stacked zones never occlude passthrough; depthWrite off + DoubleSide render correctly
// from inside the ellipsoid. 0.6 is the standard RF clearance criterion — presentation
// emphasis of the exported radius, not physics.
const ZONE_COLOR = 0xe2e8f0;
const ZONE_OPACITY_FULL = 0.06;
const ZONE_OPACITY_CORE = 0.14;
const ZONE_CORE_FACTOR = 0.6;
const ZONE_BUTTON = 4;        // xr-standard: A/X primary face button — cycles the view mode

// Terrain layer (031, contracts/ar-viewer-terrain.md): the wireframe is scenery under the
// links — neutral, low-opacity, depthWrite off (the 025 passthrough discipline); ground
// profiles are terrain facts, not link state, so they get an earth tone the health colors
// never use.
const TERRAIN_COLOR = 0x94a3b8;
const TERRAIN_OPACITY = 0.15;
// The shaded-relief palette (round 8, third tuning): the fill is a mid slate — the Lambert
// lighting multiplies it (~1.5× lit faces, ~0.5× shadow), so a mid base keeps both readable —
// and the contour grid is ONE STEP darker (≈2.2:1 luminance vs the fill), which reads as
// engraved lines. Round 7's near-black read as holes in the surface; round 5's light gray as a
// glow. Both lifted per the 2026-07-17 field pass.
const TERRAIN_SHADED_FILL = 0x76848e;
const TERRAIN_CONTOUR_COLOR = 0x3f4a54;
const TERRAIN_CONTOUR_OPACITY = 0.75; // round 9: 0.6 read too quiet in-headset
const PROFILE_COLOR = 0xb08d57;
const TERRAIN_BUTTON = 5;     // xr-standard: B/Y secondary — cycles terrain off/wire/shaded

// BRC city grid (098, contracts/brc-artifacts.md): the surveyed city as ground scenery.
// Neutral playa-dust tones no link mode or terrain style uses — streets and plazas share
// one family and the fence perimeter reads slightly warmer; view modes never repaint
// these (FR-009). depthWrite off (the 025 passthrough discipline) and a small drape lift
// (brc.js LIFT_M) keep the lines legible over the shaded terrain, which sits
// polygon-offset back at renderOrder −1. Colors + opacities are field-pass tuning knobs.
// (gate_road was a fourth family through field round 1 — dropped by operator decision.)
const BRC_KEY = 'obi.viewer.brc';
const BRC_COLORS = { streets: 0x9c927f, plazas: 0x9c927f, trash_fence: 0xa98292 };
const BRC_OPACITY = { streets: 0.45, plazas: 0.55, trash_fence: 0.8 };

function enuToScene([east, north, up]) {
  return new THREE.Vector3(east, up, -north);
}

// --- navigation helpers (029) — pure, transform-only, no geometry knowledge ---------------

function clampScale(scale) {
  return Math.min(SCALE_MAX, Math.max(SCALE_MIN, scale));
}

function clampPan(position) {
  const length = position.length();
  if (length > PAN_MAX_M) position.multiplyScalar(PAN_MAX_M / length);
  return position;
}

// Scale the world by `scaleFactor` and yaw it by `yawDelta` while keeping the scene-space
// point `point` visually fixed (the map-pinch-zoom feel, generalized to 3D): a world-content
// point at scene position q maps to  point + f·R(yawDelta)·(q − point).
function applyAboutPoint(world, point, scaleFactor, yawDelta) {
  const current = world.scale.x;
  const target = clampScale(current * scaleFactor);
  const applied = target / current; // the factor that survives the clamp
  world.scale.setScalar(target);
  world.rotation.y += yawDelta;
  const offset = world.position.clone().sub(point);
  offset.multiplyScalar(applied);
  offset.applyAxisAngle(UP_AXIS, yawDelta);
  world.position.copy(point).add(offset);
  clampPan(world.position);
}

const UP_AXIS = new THREE.Vector3(0, 1, 0);

// 080 field round 1: zooming "into the mountain" pushed the operator out — the anchor
// only knew LINK segments, and at the mountain the nearest segment lies kilometers past
// the terrain face the operator is actually looking at, so the latched pivot sat beyond
// the ground and the scene rushed past. The gaze anchor now respects the first terrain
// hit along the gaze: never anchor beyond the ground you look at. One site grid is ~51k
// triangles (161×161 posts), so the raycast is cached briefly — per-frame callers (the
// flight-speed EMA) accept a slightly stale hit via maxAgeMs, gesture-start latches
// demand a fresh one (maxAgeMs 0). Visibility is deliberately ignored: in AR passthrough
// the REAL mountain is present even with the terrain layer toggled off.
const terrainRaycaster = new THREE.Raycaster();
const terrainHit = { at: -Infinity, value: null };
function terrainGazeDistance(world, origin, direction, maxAgeMs) {
  const now = performance.now();
  if (now - terrainHit.at <= maxAgeMs) return terrainHit.value;
  let nearest = null;
  const terrain = world.userData.terrain;
  if (terrain) {
    terrainRaycaster.set(origin, direction);
    terrainRaycaster.far = ANCHOR_MAX_M;
    for (const child of terrain.children) {
      // One raycast per site grid: the shaded mesh (the contour wireframe shares its
      // displaced geometry; peak labels are sprites, which must never be raycast — they
      // require a camera the raycaster does not carry here).
      if (child.userData?.terrainStyle !== 'shaded') continue;
      const hits = terrainRaycaster.intersectObject(child, false);
      if (hits.length > 0 && (nearest === null || hits[0].distance < nearest)) {
        nearest = hits[0].distance;
      }
    }
  }
  terrainHit.at = now;
  terrainHit.value = nearest;
  return nearest;
}

// Where is the operator looking, in content terms? Walk the cached link segments (kept in
// world-local space by populateWorld), find the one nearest the gaze ray — preferring links
// inside the gaze cone, falling back to the nearest anywhere — and anchor on the gaze ray at
// that content's distance, capped by the terrain hit (above). With no links at all, anchor a
// comfortable 3 m out.
function gazeAnchor(camera, world, terrainMaxAgeMs = 0) {
  const origin = camera.getWorldPosition(new THREE.Vector3());
  const direction = camera.getWorldDirection(new THREE.Vector3());
  const ray = new THREE.Ray(origin, direction);
  const onRay = new THREE.Vector3();
  const onSegment = new THREE.Vector3();
  let inCone = null;
  let nearest = null;
  world.updateWorldMatrix(true, false);
  for (const segment of world.userData.segments ?? []) {
    ray.distanceSqToSegment(
      world.localToWorld(segment.a.clone()), world.localToWorld(segment.b.clone()),
      onRay, onSegment
    );
    const toContent = onSegment.sub(origin);
    const distance = toContent.length();
    if (nearest === null || distance < nearest) nearest = distance;
    if (toContent.angleTo(direction) < ANCHOR_CONE_RAD
        && (inCone === null || distance < inCone)) {
      inCone = distance;
    }
  }
  let distance = Math.min(ANCHOR_MAX_M, Math.max(ANCHOR_MIN_M, inCone ?? nearest ?? 3));
  const ground = terrainGazeDistance(world, origin, direction, terrainMaxAgeMs);
  if (ground !== null && ground < distance) distance = Math.max(ANCHOR_MIN_M, ground);
  return { point: ray.at(distance, new THREE.Vector3()), distance };
}

// Quest-browser texture gotcha (2026-07-17 field pass): re-uploading a canvas that was RESIZED
// after a previous upload does not reallocate the existing texture's GPU storage — the Y-flipped
// rows land offset inside the old-size storage, so the sprite shows doubled/shifted content
// while canvas-space hit regions silently diverge from the displayed pixels (panel taps fired
// rows below the aim dot even though the dot was truthful). A canvas resize after upload MUST
// swap in a fresh texture object; every resizing sprite in this file goes through this helper.
function resizeSpriteTexture(sprite, canvas, height, width = canvas.width) {
  if (canvas.height === height && canvas.width === width) return sprite.material.map;
  canvas.width = width;   // resizing either dimension clears the canvas + resets ctx state
  canvas.height = height;
  sprite.material.map.dispose();
  sprite.material.map = new THREE.CanvasTexture(canvas);
  sprite.material.needsUpdate = true;
  return sprite.material.map;
}

// A text sprite on a translucent dark card, camera-facing, drawn over everything
// (depthTest off) so labels stay legible through terrain and zones. Multi-line since 044:
// '\n' stacks lines (line 1 = the link name at full size, later lines — the capacity
// readout — slightly smaller and muted); a single-line label renders byte-identical to
// the 025 card.
function makeUpdatableLabel(position, minWidth) {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 96;
  const ctx = canvas.getContext('2d');
  let texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.copy(position);
  // Readable from the viewer position: scale with distance (never smaller than minWidth m).
  const width = Math.max(minWidth, position.length() * 0.06);
  sprite.scale.set(width, width * (96 / 512), 1);
  return {
    sprite,
    set(text) {
      const lines = String(text).split('\n');
      // 094 round 5: the card grows to fit its longest line — the 025-era fixed 512 px
      // clipped long UISP pair names at both edges. The sprite widens WITH the canvas at
      // the same px-per-meter, so the glyphs keep their size and the whole name shows.
      // Capped at 4× to bound a pathological name (which then ellipsizes via fitText).
      const measured = lines.map((line, i) => {
        ctx.font = i === 0 ? '40px system-ui, sans-serif' : '34px system-ui, sans-serif';
        return ctx.measureText(line).width;
      });
      const contentW = Math.min(2048, Math.max(512, Math.ceil(Math.max(...measured)) + 48));
      const height = 32 + lines.length * 64; // 96 for one line — the 025 card exactly
      texture = resizeSpriteTexture(sprite, canvas, height, contentW);
      sprite.scale.set(width * (canvas.width / 512), width * (height / 512), 1);
      ctx.fillStyle = 'rgba(10, 14, 20, 0.7)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      lines.forEach((line, i) => {
        ctx.font = i === 0 ? '40px system-ui, sans-serif' : '34px system-ui, sans-serif';
        ctx.fillStyle = i === 0 ? '#ffffff' : '#cbd5e1';
        ctx.fillText(fitText(ctx, line, canvas.width - 32), canvas.width / 2, 48 + i * 64);
      });
      texture.needsUpdate = true;
    },
  };
}

function makeLabel(text, position, minWidth = 8) {
  const label = makeUpdatableLabel(position, minWidth);
  label.set(text);
  return label.sprite;
}

// --- in-headset voice UI (032, row moved to the top by 094) ---------------------------------
// Head-locked (camera children — the world group is rebuilt by live refreshes, the camera is
// not), at the TOP of the view so the scene below stays unobstructed (094 US3 — the network is
// below eye level from every mast, so sky/ceiling space is where nothing lives; the 029
// center-view freshness card was removed for obstructing for the same reason).
// Repaints happen only on state transitions, never per frame.

// 094 field round 2: the row's proven 032-era depth (0.75 m) read too close to the eyes —
// pushed back, exactly the 078 panel lesson. The layout numbers below stay in their original
// 0.75 m-tuned form; HUD_AT re-projects positions, sprite sizes, AND hit/hover slacks so the
// row keeps its angular size and feel at the new depth. One constant tunes the depth.
const VOICE_UI_DISTANCE = 1.2; // meters in front of the eyes
const HUD_AT = VOICE_UI_DISTANCE / 0.75; // angular-preserving re-projection of the tuned layout
// The icon-row height (094): ONE constant for gear, links, mic, and help — positive = the top
// of the view (≈24° above center at the cluster depth, comfortably inside the Quest 3's
// upward FOV); the mode chip docks 0.055 outboard (above). Even 0.17 x-spacing keeps every pair
// of hover targets farther apart than the sum of their hover radii (mic 0.10 + links 0.06; gear
// 0.08 + links 0.06). Field-tunable — a one-line edit moves the whole row.
const HUD_ROW_Y = 0.34;

function makeVoiceButton() {
  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(0, HUD_ROW_Y * HUD_AT, -VOICE_UI_DISTANCE); // center of the icon row
  sprite.scale.set(0.16 * HUD_AT, 0.08 * HUD_AT, 1);
  sprite.renderOrder = 10;
  // mode: 'idle' | 'hover' (an aim ray is on the button) | 'active' (recording)
  const MODES = {
    idle: { fill: 'rgba(26, 39, 51, 0.85)', edge: '#2d3a45' },
    hover: { fill: 'rgba(37, 99, 235, 0.85)', edge: '#6cb6ff' },
    active: { fill: 'rgba(122, 31, 31, 0.9)', edge: '#cc3333' },
  };
  function drawMicGlyph() {
    // A vector microphone (operator ask, 033): an icon reads instantly and needs no emoji
    // font in the headset browser. Capsule + cradle arc + stem + base, centered.
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    ctx.strokeStyle = '#e6e8ea';
    ctx.fillStyle = '#e6e8ea';
    ctx.lineWidth = 7;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.roundRect(cx - 14, cy - 38, 28, 50, 14); // the capsule body
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, cy - 5, 26, 0, Math.PI, false); // the cradle
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx, cy + 21); // stem
    ctx.lineTo(cx, cy + 34);
    ctx.moveTo(cx - 16, cy + 34); // base
    ctx.lineTo(cx + 16, cy + 34);
    ctx.stroke();
  }

  // paint(null, mode) renders the mic icon (the idle affordance — operator ask: an icon,
  // not the word "talk"); paint(text, mode) renders a status text like "listening…".
  function paint(text, mode) {
    const style = MODES[mode] ?? MODES.idle;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = style.fill;
    ctx.strokeStyle = style.edge;
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.roundRect(4, 4, canvas.width - 8, canvas.height - 8, 60);
    ctx.fill();
    ctx.stroke();
    if (text === null) {
      drawMicGlyph();
    } else {
      ctx.font = '600 40px system-ui, sans-serif';
      ctx.fillStyle = '#e6e8ea';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, canvas.width / 2, canvas.height / 2);
    }
    texture.needsUpdate = true;
  }
  paint(null, 'idle');
  return { sprite, paint };
}

function makeVoiceCard() {
  const canvas = document.createElement('canvas');
  canvas.width = 1024;
  canvas.height = 512;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  // 094 round 2: the voice card is a centered surface like the panels/cards — same dock, same
  // depth, one surface at a time. 0.75 m-tuned 0.44×0.22, re-projected to the panel plane.
  sprite.position.set(0, 0, -PANEL_DISTANCE);
  sprite.scale.set(0.88, 0.44, 1);
  sprite.renderOrder = 10;
  sprite.visible = false;

  function wrap(text, maxWidth) {
    const lines = [];
    for (const paragraph of String(text).split('\n')) {
      let line = '';
      for (const word of paragraph.split(/\s+/)) {
        const candidate = line ? line + ' ' + word : word;
        if (ctx.measureText(candidate).width > maxWidth && line) {
          lines.push(line);
          line = word;
        } else {
          line = candidate;
        }
      }
      lines.push(line);
    }
    return lines;
  }

  let lastTitle = '';
  let lastBody = '';
  let closeLit = false; // the ✕ lights up while a ray is on a dismissible card

  function paint() {
    ctx.fillStyle = 'rgba(10, 14, 20, 0.78)';
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.beginPath();
    ctx.roundRect(0, 0, canvas.width, canvas.height, 28);
    ctx.fill();
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.font = '600 34px system-ui, sans-serif';
    ctx.fillStyle = '#9fb0c0';
    ctx.fillText(lastTitle, 36, 30);
    // Close affordance (field feedback 2026-07-07): tap anywhere on a settled card to
    // dismiss it — the ✕ makes that discoverable, and lights up when a tap would land.
    if (closeLit) {
      ctx.fillStyle = '#6cb6ff';
      ctx.beginPath();
      ctx.arc(canvas.width - 46, 48, 34, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#0b0f13';
    }
    ctx.textAlign = 'right';
    ctx.fillText('✕', canvas.width - 32, 30);
    ctx.textAlign = 'left';
    ctx.font = '36px system-ui, sans-serif';
    ctx.fillStyle = '#ffffff';
    const lines = wrap(lastBody ?? '', canvas.width - 72);
    const maxLines = 9;
    if (lines.length > maxLines) {
      lines.length = maxLines;
      lines[maxLines - 1] += ' …';
    }
    lines.forEach((line, i) => ctx.fillText(line, 36, 96 + i * 46));
    texture.needsUpdate = true;
  }

  function set(title, body) {
    lastTitle = title;
    lastBody = body;
    closeLit = false;
    paint();
    sprite.visible = true;
  }

  function highlightClose(on) {
    if (!sprite.visible || on === closeLit) return; // repaint only on change
    closeLit = on;
    paint();
  }

  function hide() {
    sprite.visible = false;
    closeLit = false;
  }

  return { sprite, set, hide, highlightClose };
}

// Small always-on-top marker: fingertips and aim points (field feedback 2026-07-07 — in VR
// mode the opaque background hides the operator's real hands, making the head-locked talk
// button unaimable without visual feedback).
function makePointerDot(color, radius) {
  const dot = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 10, 8),
    new THREE.MeshBasicMaterial({ color, depthTest: false, transparent: true, opacity: 0.9 })
  );
  dot.renderOrder = 11;
  dot.visible = false;
  return dot;
}

// The aim beam: a thin translucent line along the target ray, ending at the cursor dot
// (operator feedback: a bare dot doesn't read as "where am I pointing").
function makePointerBeam() {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
  const beam = new THREE.Line(
    geometry,
    new THREE.LineBasicMaterial({
      color: 0xffffff, transparent: true, opacity: 0.35, depthTest: false,
    })
  );
  beam.renderOrder = 11;
  beam.visible = false;
  beam.frustumCulled = false; // endpoints move every frame; stale bounds must not cull it
  return beam;
}

// One shared unit sphere, scaled per zone (contracts/ar-viewer-fresnel.md): scaling it to
// (D/2, r, r) and aiming +X along the chord yields the first-Fresnel ellipsoid of
// revolution with foci at the endpoints (semi-major D/2 vs the exact sqrt((D/2)²+r²)
// differs by millimeters at network scale — recorded, not computed).
const ZONE_UNIT_SPHERE = new THREE.SphereGeometry(1, 24, 16);
const ZONE_X_AXIS = new THREE.Vector3(1, 0, 0);

function makeZonePair(pointA, pointB, radius) {
  const chord = pointB.clone().sub(pointA);
  const length = chord.length();
  const midpoint = pointA.clone().add(pointB).multiplyScalar(0.5);
  const orientation = new THREE.Quaternion().setFromUnitVectors(
    ZONE_X_AXIS, chord.normalize()
  );
  const surfaces = [
    { scale: [length / 2, radius, radius], opacity: ZONE_OPACITY_FULL },
    // hair-shorter major axis avoids z-fighting with the outer surface at the apexes
    {
      scale: [(length / 2) * 0.999, radius * ZONE_CORE_FACTOR, radius * ZONE_CORE_FACTOR],
      opacity: ZONE_OPACITY_CORE,
    },
  ];
  return surfaces.map(({ scale, opacity }) => {
    const mesh = new THREE.Mesh(
      ZONE_UNIT_SPHERE,
      new THREE.MeshBasicMaterial({
        color: ZONE_COLOR,
        transparent: true,
        opacity,
        side: THREE.DoubleSide,   // legible from inside the zone (FR-007)
        depthWrite: false,        // passthrough never fully occluded, stacking-safe
      })
    );
    mesh.scale.set(...scale);
    mesh.position.copy(midpoint);
    mesh.quaternion.copy(orientation);
    return mesh;
  });
}

// Defensive read (FR-010): pre-025 exports have no fresnel_radius_m at all; missing, null,
// zero, or junk all mean "no zone" and the 022 rendering is preserved byte-for-byte.
function zoneRadius(link) {
  const radius = link.fresnel_radius_m;
  return typeof radius === 'number' && radius > 0 ? radius : null;
}

// Verdict label suffix (031 FR-011): only non-clear verdicts earn label space — a clear
// link keeps its 025 label byte-for-byte. Same defensive-read discipline as zoneRadius.
function verdictSuffix(link) {
  const verdict = link.clearance_verdict;
  if (verdict !== 'marginal' && verdict !== 'obstructed') return '';
  const mark = verdict === 'marginal' ? '⚠' : '✕';
  const margin = typeof link.clearance_margin_m === 'number'
    ? ` ${link.clearance_margin_m.toFixed(1)} m` : '';
  const at = typeof link.clearance_at_fraction === 'number'
    ? ` @ ${Math.round(link.clearance_at_fraction * 100)}%` : '';
  return ` · ${mark}${margin}${at}`;
}

// The terrain wireframe + peak labels (031 FR-012), built once from the fetched artifact
// into the permanent `terrain` group. Anything malformed or partial renders nothing —
// silently, per the contract (missing terrain is the 029 experience, never an error).
// Flip every style-tagged terrain mesh to the requested style ('wire' | 'shaded'). The wireframe
// stays visible OVER the shaded relief (2026-07-17 field pass, round 4: the lit surface alone
// blended together) — brighter there so the contours read against the solid fill.
function applyTerrainStyle(terrain, style) {
  if (!terrain) return;
  terrain.traverse((child) => {
    const tag = child.userData?.terrainStyle;
    if (tag === 'shaded') child.visible = style === 'shaded';
    if (tag === 'wire') {
      child.visible = true;
      child.material.opacity = style === 'shaded' ? TERRAIN_CONTOUR_OPACITY : TERRAIN_OPACITY;
      child.material.color.setHex(style === 'shaded' ? TERRAIN_CONTOUR_COLOR : TERRAIN_COLOR);
    }
  });
}

function buildTerrain(terrain, site, style) {
  if (!site || !Number.isFinite(site.half_extent_m) || !Number.isInteger(site.posts)) return;
  const posts = site.posts;
  const heights = site.rel_heights_dm;
  if (posts < 2 || !Array.isArray(heights) || heights.length !== posts * posts) return;
  const half = site.half_extent_m;
  const geometry = new THREE.PlaneGeometry(2 * half, 2 * half, posts - 1, posts - 1);
  geometry.rotateX(-Math.PI / 2); // plane XY → scene XZ; vertex row 0 lands on the NORTH edge
  const position = geometry.attributes.position;
  for (let row = 0; row < posts; row++) {
    // Export rows run south → north (data-model §SiteTerrain) — mirror into vertex rows.
    const exported = posts - 1 - row;
    for (let col = 0; col < posts; col++) {
      position.setY(row * posts + col, heights[exported * posts + col] / 10);
    }
  }
  // Two styles over ONE displaced geometry (2026-07-17 field pass): the 031 wireframe
  // silhouette (passthrough-friendly, the AR default) and a lit shaded relief (the VR default).
  // The wireframe ALSO overlays the shaded relief as its contour lines (round 4); the solid is
  // polygon-offset back so the lines never z-fight it. Peak labels are style-independent.
  const wire = new THREE.Mesh(
    geometry,
    new THREE.MeshBasicMaterial({
      color: style === 'shaded' ? TERRAIN_CONTOUR_COLOR : TERRAIN_COLOR,
      wireframe: true,          // a silhouette, never a solid fill (FR-012)
      transparent: true,
      opacity: style === 'shaded' ? TERRAIN_CONTOUR_OPACITY : TERRAIN_OPACITY,
      depthWrite: false,        // passthrough stays visible through the terrain
    })
  );
  wire.userData.terrainStyle = 'wire';
  geometry.computeVertexNormals(); // the shaded relief needs them; harmless for the wireframe
  const shaded = new THREE.Mesh(
    geometry,
    new THREE.MeshLambertMaterial({
      color: TERRAIN_SHADED_FILL,
      side: THREE.DoubleSide,
      polygonOffset: true,      // sit behind the contour wireframe
      // Round 9 field fix: factor 1/units 1 still z-fought the contour lines on DISTANT tiles
      // (glancing angles + km-scale depth precision) — visible as flicker. The factor term
      // scales with the polygon's depth slope, which is exactly the far-glancing case.
      polygonOffsetFactor: 3,
      polygonOffsetUnits: 6,
    })
  );
  shaded.userData.terrainStyle = 'shaded';
  shaded.visible = style === 'shaded';
  shaded.renderOrder = -1;      // solid first, contour lines over it
  terrain.add(shaded, wire);
  for (const peak of site.peaks ?? []) {
    if (!peak || typeof peak.name !== 'string' || !Array.isArray(peak.enu)) continue;
    terrain.add(makeLabel(peak.name, enuToScene(peak.enu), 12));
  }
}

// Apply the persisted band-visibility set (044 US1): each band group (links and zones
// alike) flips as a unit, and the gaze-anchor segment table is refiltered so invisible
// content never magnetizes the 029 grab gestures.
function applyBandVisibility(world, hiddenBands) {
  for (const [band, group] of Object.entries(world.userData.bandGroups ?? {})) {
    group.visible = !hiddenBands.has(band);
  }
  for (const [band, group] of Object.entries(world.userData.zoneBandGroups ?? {})) {
    group.visible = !hiddenBands.has(band);
  }
  world.userData.segments = (world.userData.allSegments ?? [])
    .filter((segment) => !hiddenBands.has(segment.band));
}

// Repaint every link's colorable (body cylinder + endpoint markers + inner core) from the active
// color view mode (078 US1, contracts/view-modes.md). ONE authority — bands.modeColor3d — decides
// the link color and opacity in every mode (maintenance > down > metric ramp > no-data gray@0.45);
// endpoint markers keep their per-end override (maintenance orange, a dark end's red) or follow the
// link color when active; the utilization inner core (US3) is tinted white-hot toward that color at
// high load. Called in place by setMode (no world rebuild — SC-005) and at the end of populateWorld.
function applyMode(world, viewMode) {
  for (const { link, body, markers, core, profile } of world.userData.colorables ?? []) {
    const { color, opacity } = modeColor3d(link, viewMode);
    // Ground profiles are clearance context (031): they show exactly while the fresnel view
    // is active, alongside the zone ellipsoids — in every other mode they were terrain noise
    // under the links.
    if (profile) profile.visible = viewMode === MODE_FRESNEL;
    body.material.color.setHex(color);
    body.material.opacity = opacity;
    body.material.transparent = opacity < 1;
    for (const { mesh, override } of markers) {
      if (override !== null) {
        mesh.material.color.setHex(override);
        mesh.material.opacity = 1;
        mesh.material.transparent = false;
      } else {
        mesh.material.color.setHex(color);
        mesh.material.opacity = opacity;
        mesh.material.transparent = opacity < 1;
      }
    }
    tintCore(core, color); // 078 US3: white-hot core, tinted toward the mode color at high load
  }
}

// The utilization inner core's tint (078 US3, R10): white-hot at idle, leaning up to ~55% toward
// the active mode's link color as the pipe fills — so a saturated core reads hot in every mode.
function tintCore(core, modeColor) {
  if (!core) return;
  const fraction = core.userData.fraction ?? 0;
  core.material.color.setHex(CORE_COLOR).lerp(new THREE.Color(modeColor), 0.55 * fraction);
}

// Build the per-link scene objects (lines, markers, zones, labels) into `world` and its
// zones group. Shared by the initial build and every applied live refresh (029 FR-011).
// Every per-link object parents to its band's group (044 US1), so a band toggle hides
// line + markers + label + profile in one visibility flip; zones get parallel per-band
// subgroups under the zones group, so the A/X zones toggle and the band toggles compose
// (three.js visibility is hierarchical).
function populateWorld(world, geometry, siteName, hiddenBands, viewMode) {
  const site = geometry.sites.find((s) => s.name === siteName);
  if (!site) throw new Error(`site ${siteName} not in the export`);
  const endpointStatus = new Map(geometry.endpoints.map((e) => [e.name, e.status]));
  const endpointMaintenance = new Map(
    geometry.endpoints.map((e) => [e.name, e.maintenance === true])
  );
  const zones = world.userData.zones;
  const bandGroups = {};
  const zoneBandGroups = {};
  const allSegments = []; // world-local; gazeAnchor maps through the transform
  // 078: the per-link objects whose material is repainted when the color view mode changes
  // (setMode recolors these in place — no world rebuild, SC-005). One authority: modeColor3d.
  const colorables = [];
  world.userData.bandGroups = bandGroups;
  world.userData.zoneBandGroups = zoneBandGroups;
  world.userData.allSegments = allSegments;
  world.userData.colorables = colorables;

  for (const link of geometry.links) {
    const a = site.enu[link.a];
    const b = site.enu[link.b];
    if (!a || !b) continue; // export invariant guarantees presence; belt-and-suspenders
    const pointA = enuToScene(a);
    const pointB = enuToScene(b);
    const band = bandOf(link);
    let group = bandGroups[band];
    if (!group) {
      group = new THREE.Group();
      bandGroups[band] = group;
      world.add(group);
    }
    allSegments.push({ a: pointA.clone(), b: pointB.clone(), band });

    // The link body is a thin cylinder, not a THREE.Line (044 US2): WebGL ignores line
    // width, so capacity-scaled thickness needs real geometry. Radius comes from the
    // shared bands.js scale; the gaze-anchor still uses the segment table above. The color
    // is set by applyMode (below) from the active view mode — a placeholder here.
    const chord = pointB.clone().sub(pointA);
    const chordLength = chord.length();
    // Mid-span BEFORE the quaternion line below: setFromUnitVectors normalizes chord IN PLACE,
    // and computing a position from the mutated (unit) chord was the round-6 field bug — the
    // utilization core sat 0.5 m from endpoint A and poked out past the cylinder end.
    const mid = pointA.clone().addScaledVector(chord, 0.5);
    const lineRadius = linkRadius3d(link);
    const body = new THREE.Mesh(
      new THREE.CylinderGeometry(lineRadius, lineRadius, chordLength, 10, 1, true),
      new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true })
    );
    body.position.copy(mid);
    body.quaternion.setFromUnitVectors(UP_AXIS, chord.normalize());
    group.add(body);

    // Small markers at each radio so a link end at a nearby mast is findable. A device in
    // maintenance keeps its orange dot even when its radio is dark (044 US3); an inactive
    // radio (its end down) stays red; an active end follows the link's mode color. The
    // per-marker `override` (a fixed color, or null = "track the link's mode color") lets
    // applyMode recolor active-end markers when the view mode changes.
    const markers = [];
    for (const [point, name] of [[pointA, link.a], [pointB, link.b]]) {
      const override = endpointMaintenance.get(name)
        ? MAINTENANCE_COLOR
        : endpointStatus.get(name) === 'active' ? null : HEALTH_COLORS.down;
      // Radius floor rides the link thickness too (round-6 field finding): the distance-scaled
      // term collapses to 0.3 m AT the anchor site (points near the ENU origin), which is
      // subpixel beside a 1-2 m-thick capacity cylinder — the anchor site's own masts showed no
      // spheres while every remote site's did.
      const marker = new THREE.Mesh(
        new THREE.SphereGeometry(
          Math.max(0.3, lineRadius * 1.6, point.length() * 0.004), 12, 8
        ),
        new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true })
      );
      marker.position.copy(point);
      group.add(marker);
      markers.push({ mesh: marker, override });
    }

    // Utilization inner core (078 US3, R10): a mid-span cylinder whose radius+length follow the
    // worst-direction usage fraction — a "fill level" inside the capacity pipe, readable in every
    // mode. Present only when usage data exists AND the link is not down (a dead link never grows a
    // fill; R4); absent (no mesh) otherwise — absence, never an empty pipe (FR-016). Parented to
    // the band group so a band toggle hides the core with its link.
    let core = null;
    const scale = link.health === 'down' ? null : coreScale(usageFraction(link));
    if (scale) {
      const coreRadius = lineRadius * scale.radiusFactor;
      core = new THREE.Mesh(
        new THREE.CylinderGeometry(coreRadius, coreRadius, chordLength * scale.lengthFactor, 10),
        new THREE.MeshBasicMaterial({ color: CORE_COLOR })
      );
      core.position.copy(mid);
      core.quaternion.copy(body.quaternion);
      core.userData.fraction = scale.lengthFactor; // how full — drives applyMode's tint
      group.add(core);
    }

    const radius = zoneRadius(link);
    if (radius !== null) {
      let zoneGroup = zoneBandGroups[band];
      if (!zoneGroup) {
        zoneGroup = new THREE.Group();
        zoneBandGroups[band] = zoneGroup;
        zones.add(zoneGroup);
      }
      for (const mesh of makeZonePair(pointA, pointB, radius)) zoneGroup.add(mesh);
    }

    // Ground profile under the link (031 FR-011): exported AMSL samples at uniform
    // fractions, placed by the contract-blessed affine map (horizontal lerp between the
    // endpoints; height = AMSL − this site's ground). Joined by link id from the one-shot
    // terrain fetch; absent profile ⇒ the 029 rendering, byte-for-byte. Visible only in
    // the fresnel view (applyMode owns the flip, like the zone ellipsoids).
    let profile = null;
    const samples = (world.userData.profiles ?? {})[link.id]?.samples_amsl;
    if (Array.isArray(samples) && samples.length >= 2) {
      const points = samples.map((amsl, i) => {
        const t = i / (samples.length - 1);
        return new THREE.Vector3(
          pointA.x + (pointB.x - pointA.x) * t,
          amsl - site.ground_elevation_m,
          pointA.z + (pointB.z - pointA.z) * t
        );
      });
      // Round 8 field fix: the profile is the PRECISE DEM line; the shaded relief is the coarse
      // 161-post grid, which z-fights and swallows the profile wherever the two interleave.
      // depthTest off keeps the true ground line always readable over the approximate surface
      // (renderOrder above terrain, below the HUD sprites at 10).
      profile = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(points),
        new THREE.LineBasicMaterial({
          color: PROFILE_COLOR, transparent: true, opacity: 0.85, depthTest: false,
        })
      );
      profile.renderOrder = 5;
      group.add(profile);
    }
    colorables.push({ link, body, markers, core, profile });

    // Label text states true meters from the ENU data — zoom changes apparent size only
    // (029 FR-007). Non-clear clearance verdicts append their margin + location (031);
    // capacity/usage joins as a second line when the export carries it (044 FR-008).
    const km = (link.length_m / 1000).toFixed(1);
    const capacity = capacityLine(link);
    // The XR label is this surface's detail card: a maintenance link states the flag AND
    // its underlying health (044 US3 — orange must never hide what the radios are doing).
    const maintenance = link.maintenance === true
      ? `\nmaintenance (health: ${link.health})` : '';
    const label = (radius !== null
      ? `${link.name} · ${km} km · F1 ${radius.toFixed(1)} m`
      : `${link.name} · ${km} km`) + verdictSuffix(link)
      + (capacity ? `\n${capacity}` : '') + maintenance;
    const midpoint = pointA.clone().add(pointB).multiplyScalar(0.5);
    group.add(makeLabel(label, midpoint));
  }

  applyBandVisibility(world, hiddenBands);
  applyMode(world, viewMode); // 078: paint every colorable from the active view mode
}

// Free the GPU resources of one replaced scene object (029 FR-015). The shared zone unit
// sphere is everyone's geometry — it must survive every rebuild.
function disposeObject(object) {
  if (object.geometry && object.geometry !== ZONE_UNIT_SPHERE) object.geometry.dispose();
  if (object.material) {
    if (object.material.map) object.material.map.dispose();
    object.material.dispose();
  }
}

// Band groups (044) made scene children nested: dispose the whole subtree, not just the
// direct child (traverse visits the object itself first).
function disposeTree(object) {
  object.traverse(disposeObject);
}

function clearGroup(group) {
  for (const child of [...group.children]) {
    group.remove(child);
    disposeTree(child);
  }
}

// In-place data refresh (029 FR-011/012/015): replace the world's CHILDREN, never the world
// group, its zones group, or its terrain group — the navigation transform (pan/yaw/zoom)
// and both toggle states live on those and must survive untouched. Terrain is static per
// session (031 FR-013): its group is never rebuilt here. Band visibility (044) is re-applied
// from the persisted set inside populateWorld, so a refresh never resets the toggles.
function applyGeometry(world, geometry, siteName, hiddenBands, viewMode) {
  if (!geometry.sites.some((s) => s.name === siteName)) {
    throw new Error(`site ${siteName} not in the refreshed export`); // scene left untouched
  }
  const zones = world.userData.zones;
  const terrain = world.userData.terrain;
  const brc = world.userData.brc;
  clearGroup(zones);
  for (const child of [...world.children]) {
    if (child === zones || child === terrain || child === brc) continue;
    world.remove(child);
    disposeTree(child);
  }
  populateWorld(world, geometry, siteName, hiddenBands, viewMode);
}

function buildWorld(geometry, siteName, eyeHeight, hiddenBands, viewMode) {
  const world = new THREE.Group();
  // Origin = the site at ground level; the camera sits at (0, 0, 0) in 'local' space, so the
  // world drops by the operator's eye height above that ground (quickstart §4).
  world.position.y = -eyeHeight;

  // All Fresnel zones live in one group so a single button flips them; they start hidden
  // (FR-008: align first, inspect second) and pan/yaw/zoom with the world. The group object
  // itself is permanent — live refreshes replace its children, never the group, so the
  // toggle state survives (029 FR-012).
  const zones = new THREE.Group();
  zones.visible = false;
  world.add(zones);
  world.userData.zones = zones;

  // The terrain layer (031): one permanent world-child group for the wireframe + peak
  // labels — 029 navigation applies untouched. VISIBLE by default (peaks are the yaw-
  // alignment aid; spec FR-013), B/Y toggles it; populated by the one-shot terrain fetch
  // and never rebuilt by live refreshes.
  const terrain = new THREE.Group();
  world.add(terrain);
  world.userData.terrain = terrain;
  world.userData.profiles = {};

  // The BRC city-grid layer (098): one permanent world-child group — populated once by
  // the one-shot brc.json intake, hidden/shown by the CITY GRID panel row, never rebuilt
  // by live refreshes (the zones/terrain permanence rule above).
  const brc = new THREE.Group();
  world.add(brc);
  world.userData.brc = brc;

  populateWorld(world, geometry, siteName, hiddenBands, viewMode);
  return world;
}

// 078 US4/R7: the 044 floating band-chip column has been removed — band visibility is now a
// section of the settings panel below (see bandRow), the single control surface.

// --- in-headset settings + links (078, amended by 094 contracts/hud-links.md) ---------------
// Two persistent head-locked buttons on the icon row open two canvas-sprite panels from the ONE
// factory below: the gear opens SETTINGS (color view selector, scene toggles, band toggles — the
// control surface that subsumed the 044 floating chip column), the links button opens the LINKS
// panel (the fleet list, promoted out of settings by 094). At most one of the two panels is open.
// All are camera children, so they survive the world's live refreshes exactly like the voice UI;
// they repaint only on state change. Interaction routes through the existing selectstart chain
// (ahead of the grab engine) and the existing pointer-dot hover feedback (updateVoicePointers),
// so panel taps never pan the world (FR-020) and the head-locked controls are aimable in opaque
// VR.

// The small gear button: ALWAYS the gear glyph — settings is settings (2026-07-17 field pass;
// the active mode is shown by the separate mode chip above it, FR-007).
function makeGearButton() {
  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(-0.34 * HUD_AT, HUD_ROW_Y * HUD_AT, -VOICE_UI_DISTANCE); // left row end
  sprite.scale.set(0.16 * HUD_AT, 0.08 * HUD_AT, 1);
  sprite.renderOrder = 10;

  function drawGear() {
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.fillStyle = '#e6e8ea';
    const teeth = 8;
    for (let i = 0; i < teeth; i++) {
      ctx.save();
      ctx.rotate((i / teeth) * Math.PI * 2);
      ctx.fillRect(-7, -38, 14, 14); // a tooth just outside the hub, at the top
      ctx.restore();
    }
    ctx.beginPath();
    ctx.arc(0, 0, 27, 0, Math.PI * 2); // the hub
    ctx.fill();
    ctx.fillStyle = 'rgba(26, 39, 51, 0.95)';
    ctx.beginPath();
    ctx.arc(0, 0, 11, 0, Math.PI * 2); // the center hole
    ctx.fill();
    ctx.restore();
  }

  function paint(hover) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = hover ? 'rgba(37, 99, 235, 0.85)' : 'rgba(26, 39, 51, 0.85)';
    ctx.strokeStyle = hover ? '#6cb6ff' : '#2d3a45';
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.roundRect(4, 4, canvas.width - 8, canvas.height - 8, 60);
    ctx.fill();
    ctx.stroke();
    drawGear();
    texture.needsUpdate = true;
  }
  paint(false);
  return { sprite, paint };
}

// The mode chip (2026-07-17 field pass): a small non-interactive readout above the gear showing
// "mode: <name>" at all times — the gear stays a pure settings glyph, and the active view is
// readable with the panel closed (FR-007).
function makeModeChip() {
  const canvas = document.createElement('canvas');
  canvas.width = 320;
  canvas.height = 64;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(-0.34 * HUD_AT, (HUD_ROW_Y + 0.055) * HUD_AT, -VOICE_UI_DISTANCE); // outboard of the gear
  sprite.scale.set(0.16 * HUD_AT, 0.032 * HUD_AT, 1);
  sprite.renderOrder = 10;
  const LABELS = {
    [MODE_HEALTH]: 'health',
    [MODE_SIGNAL]: 'signal',
    [MODE_UTILIZATION]: 'utilization',
    [MODE_FRESNEL]: 'fresnel',
  };
  function paint(activeMode) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const dim = activeMode === MODE_HEALTH; // the default mode reads quieter
    ctx.fillStyle = 'rgba(10, 14, 20, 0.7)';
    ctx.beginPath();
    ctx.roundRect(2, 2, canvas.width - 4, canvas.height - 4, 30);
    ctx.fill();
    ctx.font = '600 32px system-ui, sans-serif';
    ctx.fillStyle = dim ? '#8b98a5' : '#6cb6ff';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(`mode: ${LABELS[activeMode] ?? activeMode}`, canvas.width / 2, canvas.height / 2 + 2);
    texture.needsUpdate = true;
  }
  paint(MODE_HEALTH);
  return { sprite, paint };
}

// The help button + card (2026-07-17 field pass): a persistent "?" right of the mic on the icon
// row; tapping it opens a head-locked cheat sheet of the buttons and gestures, tap anywhere
// on the card (or the button again) to close. Static content, painted once.
function makeHelpButton() {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(0.17 * HUD_AT, HUD_ROW_Y * HUD_AT, -VOICE_UI_DISTANCE); // right of the mic
  sprite.scale.set(0.08 * HUD_AT, 0.08 * HUD_AT, 1);
  sprite.renderOrder = 10;
  function paint(hover) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = hover ? 'rgba(37, 99, 235, 0.85)' : 'rgba(26, 39, 51, 0.85)';
    ctx.strokeStyle = hover ? '#6cb6ff' : '#2d3a45';
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.arc(64, 64, 56, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.font = '700 64px system-ui, sans-serif';
    ctx.fillStyle = '#e6e8ea';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('?', 64, 68);
    texture.needsUpdate = true;
  }
  paint(false);
  return { sprite, paint };
}

// The links button (094): a persistent help-sized square on the icon row that toggles the LINKS
// panel — the fleet list, promoted out of the settings panel. Drawn glyph (two nodes joined by a
// link line, the scene's own visual grammar), no emoji font — the 033 mic-glyph lesson.
function makeLinksButton() {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(-0.17 * HUD_AT, HUD_ROW_Y * HUD_AT, -VOICE_UI_DISTANCE); // gear-mic gap
  sprite.scale.set(0.08 * HUD_AT, 0.08 * HUD_AT, 1);
  sprite.renderOrder = 10;
  function paint(hover) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = hover ? 'rgba(37, 99, 235, 0.85)' : 'rgba(26, 39, 51, 0.85)';
    ctx.strokeStyle = hover ? '#6cb6ff' : '#2d3a45';
    ctx.lineWidth = 6;
    ctx.beginPath();
    ctx.arc(64, 64, 56, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    // Two endpoint nodes and the link between them, lower-left → upper-right.
    ctx.strokeStyle = '#e6e8ea';
    ctx.fillStyle = '#e6e8ea';
    ctx.lineWidth = 8;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(44, 84);
    ctx.lineTo(84, 44);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(40, 88, 13, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(88, 40, 13, 0, Math.PI * 2);
    ctx.fill();
    texture.needsUpdate = true;
  }
  paint(false);
  return { sprite, paint };
}

const HELP_W = 640;
const HELP_WIDTH_M = 0.8;
// [section header | null, left column, right column] rows; drawn once at construction.
const HELP_ROWS = [
  ['gestures — hands or controllers', null, null],
  [null, 'pinch / trigger + drag', 'pan the world'],
  [null, 'two-hand pinch + spread', 'zoom'],
  [null, 'two-hand twist', 'rotate'],
  [null, 'quick tap on the world', 'cycle view mode'],
  ['controller buttons', null, null],
  [null, 'A / X', 'cycle view mode'],
  [null, 'B / Y', 'terrain: off / wireframe / shaded'],
  [null, 'grip (squeeze) — hold', 'push-to-talk'],
  [null, 'left stick', 'pan'],
  [null, 'right stick', 'turn + zoom'],
  ['head-locked controls', null, null],
  [null, 'mic button — hold', 'push-to-talk'],
  // 119: the backdrop row is unconditional here — makeHelpCard() takes no arguments and draws
  // once at construction, so a conditional row would mean threading availability through for
  // no benefit. On a device with no passthrough the panel row is simply absent.
  [null, 'gear', 'settings: views, terrain, bands'],
  [null, 'gear → backdrop', 'AR passthrough / opaque VR'],
  [null, 'links button', 'fleet list'],
  [null, 'row on the links panel', 'open the link card'],
  [null, '✕ on a panel / tap a card', 'close it'],
];

function makeHelpCard() {
  const canvas = document.createElement('canvas');
  canvas.width = HELP_W;
  const HEADER_H = 64;
  const ROW_H = 44;
  const PAD = 28;
  canvas.height = PAD * 2 + 56 +
    HELP_ROWS.reduce((sum, [header]) => sum + (header ? HEADER_H : ROW_H), 0);
  const ctx = canvas.getContext('2d');
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(0, 0, -PANEL_DISTANCE); // round 2: every surface docks centered
  sprite.scale.set(HELP_WIDTH_M, HELP_WIDTH_M * (canvas.height / HELP_W), 1);
  sprite.renderOrder = 11; // over the panel/card if they overlap at the edges
  sprite.visible = false;

  ctx.fillStyle = 'rgba(10, 14, 20, 0.92)';
  ctx.beginPath();
  ctx.roundRect(0, 0, HELP_W, canvas.height, 24);
  ctx.fill();
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  ctx.font = '600 36px system-ui, sans-serif';
  ctx.fillStyle = '#e6edf3';
  ctx.fillText('controls', PAD, PAD + 20);
  ctx.textAlign = 'right';
  ctx.fillStyle = '#9fb0c0';
  ctx.fillText('✕', HELP_W - PAD, PAD + 20);
  let y = PAD + 56;
  for (const [header, left, right] of HELP_ROWS) {
    if (header) {
      ctx.textAlign = 'left';
      ctx.font = '600 22px system-ui, sans-serif';
      ctx.fillStyle = '#7c8b9a';
      ctx.fillText(header.toUpperCase(), PAD, y + HEADER_H - 18);
      y += HEADER_H;
    } else {
      ctx.font = '600 24px system-ui, sans-serif';
      ctx.fillStyle = '#dbe4ee';
      ctx.textAlign = 'left';
      ctx.fillText(left, PAD + 8, y + ROW_H / 2);
      ctx.font = '24px system-ui, sans-serif';
      ctx.fillStyle = '#9fb0c0';
      ctx.textAlign = 'right';
      ctx.fillText(right, HELP_W - PAD, y + ROW_H / 2);
      y += ROW_H;
    }
  }
  texture.needsUpdate = true;

  return {
    sprite,
    show() { sprite.visible = true; },
    hide() { sprite.visible = false; },
    isVisible: () => sprite.visible,
  };
}

// The settings panel sprite. `render(state)` repaints from the caller-supplied state and rebuilds
// the canvas-pixel hot-region table; `hitTest(u, v)` maps a sprite-UV hit to a row action. The
// panel owns its layout only — all state (mode, toggles, bands, links) lives in the viewer.
// 2026-07-17 field pass (twice-tuned): at the voice cluster's 0.75 m the tall panel felt "too
// close to the eyes", and 1.15 m still did — the panel, inspect card, and help card sit at
// 1.5 m, sized up to keep their angular size, while the proven mic/gear cluster keeps its 032
// depth.
const PANEL_DISTANCE = 1.5; // meters in front of the eyes (settings panel + inspect/help cards)
const PANEL_WIDTH_M = 0.73;
const PANEL_W = 512;       // the factory's canvas px width (the settings panel's size)
const LINKS_PANEL_W = 768; // round 3: the links panel is 1.5× wider so fleet names fit un-cut
// The fresnel view's legend stops (categorical clearance verdicts, drawn as a swatch strip).
const CLEARANCE_STOPS = [
  { t: 0, hex: CLEARANCE_COLORS.clear },
  { t: 0.5, hex: CLEARANCE_COLORS.marginal },
  { t: 1, hex: CLEARANCE_COLORS.obstructed },
];
const PANEL_PAD = 18;
const PANEL_TITLE_H = 56; // the round-2 title bar: panel name + ✕ (help-card idiom)
const PANEL_HEADER_H = 46;
const PANEL_MODE_ROW_H = 74;
const PANEL_ROW_H = 52; // toggle / band / link-list / pager rows

// A name too long for a list row is ellipsized to a pixel budget so the band tag stays readable.
function fitText(ctx, text, maxPx) {
  let s = String(text ?? '');
  if (ctx.measureText(s).width <= maxPx) return s;
  while (s.length > 1 && ctx.measureText(`${s}…`).width > maxPx) s = s.slice(0, -1);
  return `${s}…`;
}

// widthPx (round 3) widens an instance's canvas AND its sprite together at the settings
// panel's px-per-meter density — a wider panel gains room (the links panel's name column),
// never smaller text.
function makeSettingsPanel(widthPx = PANEL_W) {
  const canvas = document.createElement('canvas');
  canvas.width = widthPx;
  canvas.height = 256; // resized per render to fit the content exactly
  const ctx = canvas.getContext('2d');
  let texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  const widthM = PANEL_WIDTH_M * (widthPx / PANEL_W); // constant density: px and meters scale together
  sprite.position.set(0, 0, -PANEL_DISTANCE); // round 2: every surface docks centered
  sprite.scale.set(widthM, widthM, 1); // y set per render from the aspect
  sprite.renderOrder = 10;
  sprite.visible = false;

  let regions = []; // [{x0,x1,y0,y1,action}] in canvas px, rebuilt every render

  const W = canvas.width;
  const contentX0 = PANEL_PAD;
  const contentX1 = W - PANEL_PAD;

  function headerRow(text) {
    return {
      height: PANEL_HEADER_H,
      draw(y) {
        ctx.font = '600 24px system-ui, sans-serif';
        ctx.fillStyle = '#7c8b9a';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(text.toUpperCase(), contentX0 + 6, y + PANEL_HEADER_H / 2 + 4);
      },
    };
  }

  // The title bar (094 round 2): every panel is a titled window with a ✕ — the help/inspect
  // card idiom — so all four head-locked surfaces read and close the same way. Tapping the
  // ✕ region dispatches 'close'; the rest of the bar is inert (swallowed like any panel tap).
  function titleRow(text) {
    return {
      height: PANEL_TITLE_H,
      draw(y) {
        ctx.font = '600 30px system-ui, sans-serif';
        ctx.fillStyle = '#e6edf3';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, contentX0 + 6, y + PANEL_TITLE_H / 2 + 2);
        ctx.font = '600 34px system-ui, sans-serif';
        ctx.fillStyle = '#9fb0c0';
        ctx.textAlign = 'right';
        ctx.fillText('✕', contentX1, y + PANEL_TITLE_H / 2 + 2);
      },
      hits(y) {
        return [{
          x0: contentX1 - 64, x1: W, y0: y, y1: y + PANEL_TITLE_H,
          action: { kind: 'close' },
        }];
      },
    };
  }

  function drawLegend(x, y, w, stops, lowLabel, highLabel) {
    const grad = ctx.createLinearGradient(x, 0, x + w, 0);
    for (const stop of stops) grad.addColorStop(stop.t, stop.hex);
    ctx.fillStyle = grad;
    ctx.fillRect(x, y, w, 12);
    ctx.font = '18px system-ui, sans-serif';
    ctx.fillStyle = '#9fb0c0';
    ctx.textBaseline = 'top';
    ctx.textAlign = 'left';
    ctx.fillText(lowLabel, x, y + 16);
    ctx.textAlign = 'right';
    ctx.fillText(highLabel, x + w, y + 16);
  }

  function modeRow(mode, label, legend, active) {
    return {
      height: PANEL_MODE_ROW_H,
      action: { kind: 'mode', mode },
      x0: contentX0,
      x1: contentX1,
      draw(y) {
        ctx.fillStyle = active ? 'rgba(37, 99, 235, 0.30)' : 'rgba(26, 39, 51, 0.55)';
        ctx.strokeStyle = active ? '#6cb6ff' : '#2d3a45';
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.roundRect(contentX0, y + 6, contentX1 - contentX0, PANEL_MODE_ROW_H - 12, 14);
        ctx.fill();
        ctx.stroke();
        ctx.font = '600 30px system-ui, sans-serif';
        ctx.fillStyle = active ? '#e6edf3' : '#c3ccd6';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(active ? `● ${label}` : label, contentX0 + 20, y + 30);
        if (legend) {
          drawLegend(contentX0 + 22, y + PANEL_MODE_ROW_H - 34, contentX1 - contentX0 - 44,
            legend.stops, legend.low, legend.high);
        }
      },
    };
  }

  // A link-list row: the mode-color dot (no-data links dimmed by the mode opacity), the link name
  // (ellipsized), and its band tag. Tapping the row opens the inspect card (078 US2).
  function linkRow(link, viewMode) {
    const key = linkKey(link.a, link.b);
    const { color, opacity } = modeColor3d(link, viewMode);
    return {
      height: PANEL_ROW_H,
      action: { kind: 'link', key },
      x0: contentX0,
      x1: contentX1,
      draw(y) {
        const midY = y + PANEL_ROW_H / 2;
        ctx.globalAlpha = opacity;
        ctx.fillStyle = `#${color.toString(16).padStart(6, '0')}`;
        ctx.beginPath();
        ctx.arc(contentX0 + 20, midY, 11, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.font = '20px system-ui, sans-serif';
        ctx.fillStyle = '#7c8b9a';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        const band = bandOf(link);
        ctx.fillText(band, contentX1 - 12, midY);
        const bandW = ctx.measureText(band).width;
        ctx.font = '600 26px system-ui, sans-serif';
        ctx.fillStyle = '#dbe4ee';
        ctx.textAlign = 'left';
        const nameX = contentX0 + 44;
        ctx.fillText(fitText(ctx, link.name, contentX1 - 24 - bandW - nameX), nameX, midY);
      },
    };
  }

  // The pager row: ▲ (left half) previous page, ▼ (right half) next page, with the page counter.
  function pagerRow(page, pageCount) {
    return {
      height: PANEL_ROW_H,
      draw(y) {
        const midY = y + PANEL_ROW_H / 2;
        ctx.font = '600 26px system-ui, sans-serif';
        ctx.fillStyle = '#9fb0c0';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const span = contentX1 - contentX0;
        ctx.fillText('▲ prev', contentX0 + span * 0.2, midY);
        ctx.fillText(`${page + 1} / ${pageCount}`, (contentX0 + contentX1) / 2, midY);
        ctx.fillText('next ▼', contentX0 + span * 0.8, midY);
      },
      hits(y) {
        const mid = (contentX0 + contentX1) / 2;
        return [
          { x0: contentX0, x1: mid, y0: y, y1: y + PANEL_ROW_H, action: { kind: 'pager', dir: -1 } },
          { x0: mid, x1: contentX1, y0: y, y1: y + PANEL_ROW_H, action: { kind: 'pager', dir: 1 } },
        ];
      },
    };
  }

  // A pill toggle at the right of a row (on = blue-lit, off = muted). Shared by the scene toggles
  // and (as hidden/shown) the band rows.
  function drawPill(x, midY, label, on) {
    const pillW = 96;
    const pillH = 34;
    const px = x - pillW;
    ctx.fillStyle = on ? 'rgba(37, 99, 235, 0.5)' : 'rgba(60, 72, 84, 0.5)';
    ctx.strokeStyle = on ? '#6cb6ff' : '#3c4854';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect(px, midY - pillH / 2, pillW, pillH, 17);
    ctx.fill();
    ctx.stroke();
    ctx.font = '20px system-ui, sans-serif';
    ctx.fillStyle = on ? '#e6edf3' : '#9fb0c0';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, px + pillW / 2, midY);
  }

  // A scene-toggle row (zones / terrain), on/off pill on the right (078 US4).
  function toggleRow(kind, label, on, pills = ['on', 'off']) {
    return {
      height: PANEL_ROW_H,
      action: { kind },
      x0: contentX0,
      x1: contentX1,
      draw(y) {
        const midY = y + PANEL_ROW_H / 2;
        ctx.font = '600 26px system-ui, sans-serif';
        ctx.fillStyle = on ? '#dbe4ee' : '#9fb0c0';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(label, contentX0 + 16, midY);
        drawPill(contentX1 - 6, midY, on ? pills[0] : pills[1], on);
      },
    };
  }

  // A band-toggle row (078 US4): the band label — struck through + dimmed when hidden, mirroring
  // the retired 044 chips — with a hidden/shown pill. Subsumes the whole floating chip column.
  function bandRow(band, hidden) {
    return {
      height: PANEL_ROW_H,
      action: { kind: 'band', band },
      x0: contentX0,
      x1: contentX1,
      draw(y) {
        const midY = y + PANEL_ROW_H / 2;
        ctx.font = '600 26px system-ui, sans-serif';
        ctx.fillStyle = hidden ? '#77828c' : '#dbe4ee';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(band, contentX0 + 16, midY);
        if (hidden) {
          const w = ctx.measureText(band).width;
          ctx.strokeStyle = '#77828c';
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.moveTo(contentX0 + 12, midY);
          ctx.lineTo(contentX0 + 20 + w, midY);
          ctx.stroke();
        }
        drawPill(contentX1 - 6, midY, hidden ? 'hidden' : 'shown', !hidden);
      },
    };
  }

  // Section builders, top→bottom (094 contracts/hud-links.md): mode selector → scene toggles →
  // band toggles → link list. EVERY section renders only when its state is supplied — the one
  // factory builds both panels: settings passes `modeRows`+scene+bands, the links panel passes
  // `links`+`listPage` (`mode` rides along in both for the row-dot colors).
  function buildRows(state) {
    const rows = [];
    if (state.title) rows.push(titleRow(state.title));
    if (state.modeRows) {
      rows.push(headerRow('view'));
      rows.push(modeRow(MODE_HEALTH, 'health', null, state.mode === MODE_HEALTH));
      rows.push(modeRow(MODE_SIGNAL, 'signal',
        { stops: SIGNAL_RAMP, low: 'strong −45 dBm', high: 'weak −70 dBm' },
        state.mode === MODE_SIGNAL));
      rows.push(modeRow(MODE_UTILIZATION, 'utilization',
        { stops: USAGE_RAMP, low: 'idle 0%', high: 'full 100%' },
        state.mode === MODE_UTILIZATION));
      rows.push(modeRow(MODE_FRESNEL, 'fresnel zones',
        { stops: CLEARANCE_STOPS, low: 'clear', high: 'obstructed' },
        state.mode === MODE_FRESNEL));
    }
    if (state.terrain !== undefined) {
      rows.push(headerRow('scene'));
      // One tri-state row (round 5): tapping cycles off → wireframe → shaded, same as B/Y.
      const terrainPill = !state.terrain ? 'off'
        : (state.terrainStyle === 'shaded' ? 'shaded' : 'wireframe');
      rows.push(toggleRow('terrainCycle', 'terrain', state.terrain,
        [terrainPill, terrainPill]));
      // 098: the CITY GRID row exists only when the fetched city has an anchor for this
      // site (contract C5) — never a dead toggle promising nothing (FR-007).
      if (state.brcAvailable) rows.push(toggleRow('brc', 'city grid', state.brc));
      // 119 (contract C4): the BACKDROP row, on the same availability discipline — present
      // only where the session actually composites alpha-blend. `on` means OPAQUE so the pill
      // tints in the "normal state" reading the other rows use.
      if (state.backdropAvailable) {
        rows.push(toggleRow('backdrop', 'backdrop', state.opaque, ['opaque', 'passthrough']));
      }
    }
    if (state.bands && state.bands.length) {
      rows.push(headerRow('bands'));
      for (const { band, hidden } of state.bands) rows.push(bandRow(band, hidden));
    }
    if (state.links) {
      // The link count lives in the title bar now — no duplicate section header (round 2).
      const paged = linkListPage(state.links, state.listPage ?? 0);
      for (const link of paged.items) rows.push(linkRow(link, state.mode));
      if (paged.pageCount > 1) rows.push(pagerRow(paged.page, paged.pageCount));
    }
    return rows;
  }

  function render(state) {
    const rows = buildRows(state);
    const totalH = rows.reduce((sum, r) => sum + r.height, 0) + 2 * PANEL_PAD;
    texture = resizeSpriteTexture(sprite, canvas, totalH);
    ctx.clearRect(0, 0, W, canvas.height);
    ctx.fillStyle = 'rgba(10, 14, 20, 0.88)';
    ctx.beginPath();
    ctx.roundRect(0, 0, W, canvas.height, 24);
    ctx.fill();
    regions = [];
    let y = PANEL_PAD;
    for (const row of rows) {
      row.draw(y);
      if (row.hits) {
        regions.push(...row.hits(y));
      } else if (row.action) {
        regions.push({
          x0: row.x0 ?? 0, x1: row.x1 ?? W, y0: y, y1: y + row.height, action: row.action,
        });
      }
      y += row.height;
    }
    sprite.scale.y = widthM * (canvas.height / W);
    texture.needsUpdate = true;
  }

  function hitTest(u, v) {
    const px = u * W;
    const py = v * canvas.height;
    for (const r of regions) {
      if (px >= r.x0 && px <= r.x1 && py >= r.y0 && py <= r.y1) return r.action;
    }
    return null;
  }

  function setOpen(open, state) {
    sprite.visible = open;
    if (open) render(state);
  }

  return { sprite, render, hitTest, setOpen, isOpen: () => sprite.visible };
}

// The head-locked inspect card (078 US2, contracts/panel-card.md): the 2D LINK window's facts —
// field-for-field the same values widgets/link.js shows (SC-007), formatted with the same bands.js
// formatters — plus its elevation profile chart ported to canvas. Docked right-of-center; opened
// from a panel link-list row; the ✕ (tap anywhere) dismisses, voice-card idiom. A refresh that
// dropped the inspected link repaints with the "no longer in the current snapshot" banner instead
// of showing stale values (US2 #5).
const CARD_WIDTH_M = 0.86;
const CARD_PAD = 24;
const CARD_TITLE_H = 66;
const CARD_FACT_H = 46;
const CARD_CHART_H = 300;
const CARD_W = 640;

// The card's fact rows — every row always present, a missing value shown as — (never 0, never an
// omitted row, FR-014). References the exact links.json fields widgets/link.js reads, so the
// grep-parity tripwire (SC-007) holds by construction.
function cardFacts(link, endpointSite) {
  const dash = '—';
  const freq = link.frequency_ghz != null
    ? `${link.frequency_ghz} GHz` + (
      link.frequency_provenance && link.frequency_provenance !== 'controller'
        ? ` (${link.frequency_provenance})` : '')
    : dash;
  const cap = (link.capacity_down_bps != null || link.capacity_up_bps != null)
    ? `${formatRate(link.capacity_down_bps) ?? dash} ↓ / ${formatRate(link.capacity_up_bps) ?? dash} ↑`
    : dash;
  const percent = usagePercent(link);
  const usage = (link.usage_down_bps != null || link.usage_up_bps != null)
    ? `${formatRate(link.usage_down_bps) ?? dash} ↓ / ${formatRate(link.usage_up_bps) ?? dash} ↑`
      + (percent ? ` (${percent})` : '')
    : dash;
  const clearance = link.clearance_verdict != null
    ? `${link.clearance_verdict} · worst ${link.clearance_margin_m != null
      ? link.clearance_margin_m.toFixed(1) : dash} m at `
      + `${Math.round((link.clearance_at_fraction ?? 0) * 100)}%`
    : dash;
  const facts = [
    ['A', `${link.a} (${endpointSite.get(link.a) ?? '?'})`],
    ['B', `${link.b} (${endpointSite.get(link.b) ?? '?'})`],
    ['band', link.band || dash],
    ['frequency', freq],
    ['length', link.length_m != null ? `${(link.length_m / 1000).toFixed(2)} km` : dash],
    ['signal', link.signal_dbm != null ? `${link.signal_dbm} dBm` : dash],
    ['capacity', cap],
    ['usage', usage],
    ['clearance', clearance],
  ];
  if (link.maintenance === true) facts.push(['maintenance', `flagged · health ${link.health}`]);
  return facts;
}

// The elevation profile, ported from widgets/link.js's canvas chart into a card sub-rectangle:
// terrain silhouette, the first-Fresnel envelope + line of sight, and the precomputed worst-
// clearance marker. Every number is an export field — no geodesy. Axis labels are ≥28 px so the
// chart reads at headset distance. Absent/short profile ⇒ draws nothing (FR-013).
function drawCardProfile(ctx, rect, link, profile, endpointAmsl) {
  const samples = profile?.samples_amsl;
  if (!Array.isArray(samples) || samples.length < 2) return;
  const heightA = endpointAmsl.get(link.a);
  const heightB = endpointAmsl.get(link.b);
  const radius = link.fresnel_radius_m ?? 0;
  const los = (t) => (heightA != null && heightB != null ? heightA + (heightB - heightA) * t : null);
  const envelope = (t) => radius * 2 * Math.sqrt(t * (1 - t));

  let lo = Math.min(...samples);
  let hi = Math.max(...samples);
  if (heightA != null && heightB != null) {
    lo = Math.min(lo, heightA - radius, heightB - radius);
    hi = Math.max(hi, heightA + radius, heightB + radius);
  }
  const pad = Math.max(2, (hi - lo) * 0.12);
  lo -= pad;
  hi += pad;
  const left = 64;
  const right = 12;
  const top = 12;
  const bottom = 40;
  const plotW = rect.w - left - right;
  const plotH = rect.h - top - bottom;
  const sx = (t) => rect.x + left + t * plotW;
  const sy = (amsl) => rect.y + top + (1 - (amsl - lo) / (hi - lo)) * plotH;

  // terrain silhouette
  ctx.beginPath();
  ctx.moveTo(sx(0), sy(samples[0]));
  for (let i = 1; i < samples.length; i++) ctx.lineTo(sx(i / (samples.length - 1)), sy(samples[i]));
  ctx.lineTo(sx(1), rect.y + rect.h - bottom);
  ctx.lineTo(sx(0), rect.y + rect.h - bottom);
  ctx.closePath();
  ctx.fillStyle = 'rgba(148, 121, 91, 0.45)';
  ctx.fill();

  if (heightA != null && heightB != null) {
    ctx.beginPath();
    const steps = 48;
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const y = sy(los(t) - envelope(t));
      if (i === 0) ctx.moveTo(sx(t), y);
      else ctx.lineTo(sx(t), y);
    }
    for (let i = steps; i >= 0; i--) {
      const t = i / steps;
      ctx.lineTo(sx(t), sy(los(t) + envelope(t)));
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(96, 165, 250, 0.18)';
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(sx(0), sy(los(0)));
    ctx.lineTo(sx(1), sy(los(1)));
    ctx.strokeStyle = '#60a5fa';
    ctx.lineWidth = 3;
    ctx.stroke();
  }

  if (link.clearance_at_fraction != null && heightA != null && heightB != null) {
    const t = link.clearance_at_fraction;
    const y = sy(los(t) - envelope(t));
    ctx.beginPath();
    ctx.arc(sx(t), y, 8, 0, Math.PI * 2);
    const flagged = link.clearance_verdict === 'marginal' || link.clearance_verdict === 'obstructed';
    ctx.fillStyle = flagged ? '#ef4444' : '#22c55e';
    ctx.fill();
    if (link.clearance_margin_m != null) {
      ctx.font = '28px system-ui, sans-serif';
      ctx.fillStyle = '#e6edf3';
      ctx.textAlign = t > 0.5 ? 'right' : 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(`${link.clearance_margin_m.toFixed(1)} m`, sx(t) + (t > 0.5 ? -12 : 12), y - 12);
    }
  }

  // axes: elevation ticks + span label, ≥28 px for headset legibility
  ctx.font = '28px system-ui, sans-serif';
  ctx.fillStyle = '#9fb0c0';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  ctx.fillText(`${Math.round(hi)} m`, rect.x + 4, rect.y + top);
  ctx.textBaseline = 'bottom';
  ctx.fillText(`${Math.round(lo)} m`, rect.x + 4, rect.y + rect.h - bottom + 30);
  if (link.length_m != null) {
    ctx.textAlign = 'center';
    ctx.fillText(`${(link.length_m / 1000).toFixed(2)} km`, rect.x + left + plotW / 2,
      rect.y + rect.h - 4);
  }
}

function makeInspectCard() {
  const canvas = document.createElement('canvas');
  canvas.width = CARD_W;
  canvas.height = 256; // resized per render
  const ctx = canvas.getContext('2d');
  let texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true })
  );
  sprite.position.set(0, 0, -PANEL_DISTANCE); // round 2: every surface docks centered
  sprite.scale.set(CARD_WIDTH_M, CARD_WIDTH_M, 1);
  sprite.renderOrder = 10;
  sprite.visible = false;

  let current = null; // { kind: 'link', ... } | { kind: 'gone', name }
  let closeLit = false;

  function frame(height) {
    texture = resizeSpriteTexture(sprite, canvas, height);
    ctx.clearRect(0, 0, CARD_W, height);
    ctx.fillStyle = 'rgba(10, 14, 20, 0.90)';
    ctx.beginPath();
    ctx.roundRect(0, 0, CARD_W, height, 24);
    ctx.fill();
    // title + ✕ dismiss (lights up while a ray is on the card)
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.font = '600 34px system-ui, sans-serif';
    ctx.fillStyle = '#e6edf3';
    ctx.fillText(fitText(ctx, current?.title ?? '', CARD_W - 120), CARD_PAD, CARD_PAD);
    if (closeLit) {
      ctx.fillStyle = '#6cb6ff';
      ctx.beginPath();
      ctx.arc(CARD_W - 44, CARD_PAD + 18, 30, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#0b0f13';
    } else {
      ctx.fillStyle = '#9fb0c0';
    }
    ctx.textAlign = 'right';
    ctx.fillText('✕', CARD_W - 28, CARD_PAD);
    sprite.scale.y = CARD_WIDTH_M * (height / CARD_W);
    texture.needsUpdate = true;
  }

  function paint() {
    if (!current) return;
    if (current.kind === 'gone') {
      const height = CARD_PAD * 2 + CARD_TITLE_H + CARD_FACT_H * 2;
      frame(height);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.font = '28px system-ui, sans-serif';
      ctx.fillStyle = '#f0a35a';
      ctx.fillText('this link is no longer in the current snapshot',
        CARD_PAD, CARD_PAD + CARD_TITLE_H + CARD_FACT_H);
      texture.needsUpdate = true;
      return;
    }
    const { link, endpointSite, endpointAmsl, profile } = current;
    const facts = cardFacts(link, endpointSite);
    const hasChart = Array.isArray(profile?.samples_amsl) && profile.samples_amsl.length >= 2;
    const chartH = hasChart ? CARD_CHART_H : 0;
    const height = CARD_PAD * 2 + CARD_TITLE_H + facts.length * CARD_FACT_H + chartH;
    frame(height);
    let y = CARD_PAD + CARD_TITLE_H;
    const valueX = CARD_PAD + 160;
    for (const [label, value] of facts) {
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      ctx.font = '24px system-ui, sans-serif';
      ctx.fillStyle = '#7c8b9a';
      ctx.fillText(label, CARD_PAD, y + CARD_FACT_H / 2);
      ctx.font = '600 26px system-ui, sans-serif';
      ctx.fillStyle = '#dbe4ee';
      ctx.fillText(fitText(ctx, value, CARD_W - CARD_PAD - valueX), valueX, y + CARD_FACT_H / 2);
      y += CARD_FACT_H;
    }
    if (hasChart) {
      drawCardProfile(ctx, { x: CARD_PAD, y: y + 6, w: CARD_W - 2 * CARD_PAD, h: chartH - 12 },
        link, profile, endpointAmsl);
    }
    texture.needsUpdate = true;
  }

  function render(data) {
    current = { kind: 'link', title: data.link.name, ...data };
    closeLit = false;
    paint();
    sprite.visible = true;
  }

  function renderGone(name) {
    current = { kind: 'gone', title: name ?? current?.title ?? 'link' };
    closeLit = false;
    paint();
    sprite.visible = true;
  }

  function highlightClose(on) {
    if (!sprite.visible || on === closeLit) return;
    closeLit = on;
    paint();
  }

  function hide() {
    sprite.visible = false;
    closeLit = false;
    current = null;
  }

  return { sprite, render, renderGone, hide, highlightClose, isVisible: () => sprite.visible };
}

// 119 (contracts/xr-backdrop.md C2): the opaque backdrop MUST be a TEXTURE, not a Color.
//
// `scene.background = new THREE.Color(...)` renders NOTHING in an alpha-blend XR session.
// three.js r160's WebGLBackground.render turns a Color background into a clear color, then
// unconditionally overrides that clear from the XR blend mode, then clears:
//
//     null === x ? g(o, h) : x && x.isColor && (g(x, 1), v = !0);
//     const y = t.xr.getEnvironmentBlendMode();
//     "additive" === y ? ...setClear(0,0,0,1,a) : "alpha-blend" === y && ...setClear(0,0,0,0,a);
//
// A Color background has no geometry — it IS the clear color — so under our always-AR session
// it would be erased every frame and "opaque" would look identical to passthrough. A TEXTURE
// background takes a different branch: three.js draws a real full-screen PlaneGeometry(2,2)
// mesh (BackgroundMaterial, depthTest/depthWrite off) unshifted to the front of the render
// list, after the clear, so the override cannot touch it. Its shader writes the texture's
// alpha and the material is not `transparent`, so blending is off and dest alpha is 1 —
// passthrough fully occluded. SRGBColorSpace is required or the shade drifts from 0x0b0f13.
//
// Module-level so re-entering a session does not reallocate it.
const OPAQUE_BACKDROP = new THREE.DataTexture(new Uint8Array([0x0b, 0x0f, 0x13, 255]), 1, 1);
OPAQUE_BACKDROP.colorSpace = THREE.SRGBColorSpace;
OPAQUE_BACKDROP.needsUpdate = true;

// 119: ONE session type. `canAR` is resolved by the XR widget at mount and passed in — never
// probed here. requestSession CONSUMES transient user activation, so a try-AR-catch-retry-VR
// fallback inside the entry gesture's handler would reject the second call with a security
// error: AR support cannot be discovered by failing at it (contract C1, research R3).
export async function startAR(
  geometry, siteName, eyeHeight, onEnd,
  { canAR = false, startOpaque = true } = {},
  voice = { enabled: false, maxSeconds: 90 }
) {
  const scene = new THREE.Scene();
  // 119: the backdrop is now RUNTIME state, not a session property (031's mode picker is
  // retired). Passthrough = no background at all; opaque = the texture above painted over it.
  // Forced opaque below if the live session turns out not to composite alpha-blend.
  let opaque = startOpaque;
  scene.background = opaque ? OPAQUE_BACKDROP : null;
  const camera = new THREE.PerspectiveCamera(70, 1, 0.05, 50000);
  // The camera joins the scene graph so head-locked children (the 032 voice UI) render;
  // live refreshes rebuild the world's children only, so camera children always survive.
  scene.add(camera);
  const hiddenBands = loadHiddenBands(BANDS_KEY); // 044 US1: survives reload + refresh
  // 078: the color view mode persists across sessions (FR-017); an unknown/corrupt stored value
  // degrades to health (FR-018). Threaded into every world (re)build so the 5-min refresh keeps it.
  let viewMode = loadViewerPref(MODE_KEY, MODE_HEALTH);
  if (!VIEW_MODES.includes(viewMode)) viewMode = MODE_HEALTH;
  // 078 US4: terrain visibility persists per surface too (was session-only). First-run default
  // matches today's session behavior — terrain on (FR-017); storage-unavailable degrades to the
  // defaults (FR-018, via loadViewerPref's try/catch). Zone visibility is NOT a toggle anymore:
  // the ellipsoids show exactly while the fresnel VIEW is active (2026-07-17 field pass).
  let terrainOn = loadViewerPref(TERRAIN_KEY, true) !== false;
  // Terrain style (2026-07-17 field pass): wireframe or a lit shaded surface, per BACKDROP —
  // passthrough defaults to the see-through wireframe, opaque to the shaded relief. Each side
  // persists its own choice.
  //
  // 119: the key is DERIVED on every use, never captured. The backdrop now changes mid-session,
  // so a key captured at entry would write the wrong side after a flip (contract C6, research
  // R7). The 'ar'/'vr' suffixes are kept deliberately — renaming them would silently discard
  // every operator's stored choice for no functional gain.
  function loadTerrainStyleFor(isOpaque) {
    const fallback = terrainStyleDefaultFor(isOpaque);
    const stored = loadViewerPref(terrainStyleKeyFor(isOpaque), fallback);
    return stored === 'wire' || stored === 'shaded' ? stored : fallback;
  }
  let terrainStyle = loadTerrainStyleFor(opaque);
  // 098: the city-grid layer's visibility persists like the other scene toggles; default ON
  // (spec FR-006). brcDoc holds the fetched document once the one-shot intake lands —
  // availability is per-site: no anchor for this site ⇒ no geometry and no panel row.
  let brcOn = loadViewerPref(BRC_KEY, true) !== false;
  let brcDoc = null;
  const world = buildWorld(geometry, siteName, eyeHeight, hiddenBands, viewMode);
  world.userData.zones.visible = viewMode === MODE_FRESNEL; // restore the persisted view on entry
  world.userData.terrain.visible = terrainOn;
  world.userData.brc.visible = brcOn;
  scene.add(world);
  // Lights exist for the SHADED terrain style only — every other material in the scene is unlit
  // (MeshBasic/Sprite/Line), so they affect nothing else. A low raking sun with modest ambient
  // (round 4: near-vertical light + strong ambient flattened the relief into one blended mass).
  scene.add(new THREE.HemisphereLight(0xdfe8f0, 0x2a3138, 0.5));
  const sun = new THREE.DirectionalLight(0xffffff, 1.2);
  sun.position.set(1, 0.55, 0.35);
  scene.add(sun);

  // Field-debug handle (see memory: quest-usb-debugging): module closures are unreachable
  // from CDP, so expose the scene graph for USB inspection. Same-origin LAN page; no data
  // leaves the headset — this is the permanent version of the 029 rAF-hook acrobatics.
  window.__obiDebug = { scene, camera, world };

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.xr.enabled = true;
  renderer.xr.setReferenceSpaceType('local');
  // Fixed foveated rendering OFF (field lesson, 2026-07-06 VR pass): three.js defaults
  // XR foveation to maximum, which renders the periphery in coarser tiles. Invisible on
  // sparse content, but the full-view terrain wireframe (031) exposes the tile boundaries
  // as large view-anchored translucent rectangles. Our scene is lines and sprites — the
  // GPU headroom for full-resolution rendering is ample.
  renderer.xr.setFoveation(0);
  document.body.appendChild(renderer.domElement);

  // hand-tracking is OPTIONAL (029 FR-005): where absent, no hand input sources appear and
  // everything else — controllers included — behaves exactly as before.
  // 119 (contract C1): ONE session type. immersive-ar wherever the device supports it — its
  // alpha-blend composite is what lets an opaque backdrop hide reality, so a single AR session
  // can present BOTH looks, while a VR session can only ever present one. immersive-vr remains
  // as the fallback for runtimes without AR (desktop browsers, PC-tethered headsets), where the
  // backdrop is simply always opaque and the toggle is not offered.
  //
  // No isSessionSupported call here, and no retry on rejection — see the header comment on
  // startAR. A refusal propagates to the widget, which explains it and lets the operator press
  // Enter again for a fresh user activation.
  const session = await navigator.xr.requestSession(
    canAR ? 'immersive-ar' : 'immersive-vr',
    { optionalFeatures: ['local', 'hand-tracking'] }
  );
  session.addEventListener('end', () => {
    renderer.setAnimationLoop(null);
    renderer.dispose();
    renderer.domElement.remove();
    if (onEnd) onEnd();
  });
  await renderer.xr.setSession(session);

  // 119 (contract C3): ask the LIVE session what it actually composites, rather than assuming
  // from what we requested. Only 'alpha-blend' can show both backdrops, so only there does the
  // toggle exist. 'opaque' (the VR fallback, or a runtime that claimed AR but has no
  // passthrough) would render "passthrough" as black, not reality; 'additive' is a see-through
  // display where black IS transparent and an opaque backdrop is impossible. In both, force
  // opaque and omit the row — a control that cannot act must not exist (098's discipline).
  const canFlipBackdrop = backdropAvailable(renderer.xr.getEnvironmentBlendMode());
  // 119: expose the live session on the debug handle so a field pass can read
  // `window.__obiDebug.session.environmentBlendMode` over CDP (quickstart step 8). The handle
  // is built above, before the session exists, so it is attached here.
  window.__obiDebug.session = session;
  if (!canFlipBackdrop && !opaque) {
    opaque = true;
    scene.background = OPAQUE_BACKDROP;
    terrainStyle = loadTerrainStyleFor(opaque);
    applyTerrainStyle(world.userData.terrain, terrainStyle);
  }

  // 078 US4/R11 (amended by 094): the ONE shared path per control. A/X (button 4) AND the
  // quick-tap gesture step through ALL views in order via cycleMode — one gesture, one meaning
  // (the 078 fresnel peek is retired); B/Y (button 5) and the panel row share toggleTerrain —
  // so the scene, the persisted pref, and the panel rows stay consistent (FR-010).
  // setMode/refreshPanel are hoisted (defined below), so both are callable by the time any
  // toggle actually fires.
  function cycleMode() {
    setMode(VIEW_MODES[(VIEW_MODES.indexOf(viewMode) + 1) % VIEW_MODES.length]);
  }

  // B/Y cycles terrain off → wireframe → shaded → off (round 4). Re-entering from off always
  // restarts at wireframe; the panel rows still flip visibility and style independently.
  function cycleTerrain() {
    if (!terrainOn) {
      if (terrainStyle !== 'wire') toggleTerrainStyle();
      toggleTerrain();
    } else if (terrainStyle === 'wire') {
      toggleTerrainStyle();
    } else {
      toggleTerrain();
    }
  }

  // 119 (contract C5): the whole feature. Flip the backdrop in place — the session keeps
  // running, the world group is never rebuilt, nothing is refetched, and every piece of
  // session state (viewpoint, view mode, layers, open surfaces, an in-flight voice turn)
  // survives by construction because none of it is touched. Same shape as toggleBrc below.
  //
  // The terrain style follows the backdrop to the OTHER side's remembered choice, but is
  // deliberately NOT written back here: writing the value we just read would blur which key
  // owns it. Only an explicit style change (toggleTerrainStyle) persists.
  function toggleBackdrop() {
    if (!canFlipBackdrop) return;
    opaque = !opaque;
    scene.background = opaque ? OPAQUE_BACKDROP : null;
    saveBackdropOpaque(opaque);
    terrainStyle = loadTerrainStyleFor(opaque);
    applyTerrainStyle(world.userData.terrain, terrainStyle);
    refreshPanel();
  }

  function toggleTerrain() {
    const terrain = world.userData.terrain;
    if (!terrain) return;
    terrain.visible = !terrain.visible;
    terrainOn = terrain.visible;
    saveViewerPref(TERRAIN_KEY, terrainOn);
    refreshPanel();
  }

  function toggleTerrainStyle() {
    terrainStyle = terrainStyle === 'wire' ? 'shaded' : 'wire';
    // 119: derive the key at call time — the backdrop can have flipped since entry, and this
    // must persist against the side the operator is actually looking at (contract C6).
    saveViewerPref(terrainStyleKeyFor(opaque), terrainStyle);
    applyTerrainStyle(world.userData.terrain, terrainStyle);
    refreshPanel();
  }

  // The terrain WIRE mesh (031's silhouette / shaded-mode contour lines) — the only mesh
  // the fence mask below touches; the shaded ground fill is never holed.
  function findTerrainWire() {
    let wire = null;
    world.userData.terrain?.traverse((child) => {
      if (child.userData?.terrainStyle === 'wire') wire = child;
    });
    return wire;
  }

  // 098: the panel-only city-grid toggle (both face buttons are taken — research D8): flip
  // the permanent group, persist the choice, repaint — a flip, never a rebuild (SC-003).
  // Field round 1: the fence-holed wireframe follows the toggle, so layer-off restores the
  // terrain exactly as it was before 098.
  function toggleBrc() {
    const brcGroup = world.userData.brc;
    if (!brcGroup) return;
    brcGroup.visible = !brcGroup.visible;
    brcOn = brcGroup.visible;
    const wire = findTerrainWire();
    if (wire && wire.userData.fencedGeometry) {
      wire.geometry = brcOn ? wire.userData.fencedGeometry : wire.userData.fullGeometry;
    }
    saveViewerPref(BRC_KEY, brcOn);
    refreshPanel();
  }

  // --- in-headset voice (032, contracts/ar-voice-api.md; amended 094) ----------------------
  // Push-to-talk: hold grip (squeeze — the one unbound controller input) or pinch/trigger-
  // hold the head-locked talk button (the hands-mode affordance; hands never fire squeeze).
  // The mic was staged on the setup page (FR-011). 094 field round 1: the button renders
  // whenever voice is enabled — an unstaged capture (fresh origin, skipped enable step) no
  // longer leaves a HOLE in the icon row; a hold then explains how to enable instead of
  // recording (talk.holdStart's own unavailable path — the same thing squeeze does).
  const voiceHolds = new Set(); // input sources currently holding the talk BUTTON
  let squeezeSource = null;     // the controller currently holding grip, if any
  let talk = null;
  let talkButton = null;
  let voiceCard = null;
  let lastVoiceState = 'idle';
  let voiceHover = false;       // an aim ray is currently on the talk button
  const pointerDots = new Map(); // XRInputSource → {ray, index, thumb} marker meshes
  // Aim beams/dots (032) now serve the settings gear + panel too (078), so they exist regardless
  // of voice: without them the head-locked controls are unaimable in opaque VR.
  const pointers = new THREE.Group();
  scene.add(pointers);

  if (voice && voice.enabled) {
    voiceCard = makeVoiceCard();
    camera.add(voiceCard.sprite);
    talkButton = makeVoiceButton();
    camera.add(talkButton.sprite);

    const ERROR_TITLES = {
      unreachable: "⚠ can't reach obi",
      gated: "⚠ didn't catch that",
      busy: '⚠ busy',
      model_unavailable: '⚠ model unavailable',
      turn_failed: '⚠ turn failed',
    };
    let lastTranscript = '';
    let turnCount = 0;

    talk = createTalkControl({
      maxSeconds: voice.maxSeconds ?? 90,
      onState: (state, detail) => {
        lastVoiceState = state;
        // Round 2: the voice card is a centered surface like the rest — any visible state
        // takes the dock (closeSurfaces is hoisted; it runs long after startAR finished).
        if (state !== 'idle') closeSurfaces('voice');
        voiceHover = false; // state paints reset the button; the next frame re-applies hover
        switch (state) {
          case 'idle':
            voiceCard.hide();
            if (talkButton) talkButton.paint(null, 'idle');
            break;
          case 'unavailable':
            voiceCard.set('voice unavailable', detail.message ?? '');
            break;
          case 'listening':
            lastTranscript = '';
            voiceCard.set('● listening', 'release to ask');
            if (talkButton) talkButton.paint('listening…', 'active');
            break;
          case 'submitting':
            if (talkButton) talkButton.paint(null, 'idle');
            voiceCard.set(
              'sending…',
              detail.capped ? 'hit the recording cap — sent what was captured' : ''
            );
            break;
          case 'thinking':
            if (detail.transcript) lastTranscript = detail.transcript;
            voiceCard.set(
              lastTranscript ? 'you: ' + lastTranscript : 'thinking',
              detail.note ?? 'thinking…'
            );
            break;
          case 'speaking': {
            turnCount += 1;
            // First turn is always a "new" conversation — only later resets earn a notice
            // (FR-008; same rule as the 024 talk page).
            const reset = detail.contextReset && turnCount > 1
              ? '(fresh conversation — the previous one expired)\n' : '';
            voiceCard.set('obi', reset + (detail.text ?? ''));
            break;
          }
          case 'answered':
            voiceCard.set('obi', detail.text ?? ''); // persists until the next turn (FR-007)
            break;
          case 'error':
            voiceCard.set(
              ERROR_TITLES[detail.errorKind] ?? '⚠ error', detail.message ?? ''
            );
            if (talkButton) talkButton.paint(null, 'idle');
            break;
          default:
            break;
        }
      },
    });
    // Session over (or headset off) mid-turn: drop any capture, stop playback — the next
    // visit starts clean (spec edge case; analyze C1).
    session.addEventListener('end', () => talk.cancel());

    session.addEventListener('squeezestart', (event) => {
      if (squeezeSource) return; // one PTT hold at a time
      squeezeSource = event.inputSource;
      talk.holdStart();
    });
    session.addEventListener('squeezeend', (event) => {
      if (squeezeSource !== event.inputSource) return;
      squeezeSource = null;
      talk.holdEnd();
    });

    // USB field debugging (see memory: quest-usb-debugging): voice state is closure-held,
    // so surface it beside the scene handle.
    window.__obiDebug.voice = {
      card: voiceCard,
      button: talkButton,
      get state() { return lastVoiceState; },
    };
  }

  // Does this select's target ray point at (or pinch on) a head-locked voice sprite?
  function selectHits(event, sprite, slack) {
    if (!sprite || !event.frame) return false;
    const referenceSpace = renderer.xr.getReferenceSpace();
    if (!referenceSpace) return false;
    const pose = event.frame.getPose(event.inputSource.targetRaySpace, referenceSpace);
    if (!pose) return false;
    const p = pose.transform.position;
    const o = pose.transform.orientation;
    const origin = new THREE.Vector3(p.x, p.y, p.z);
    const direction = new THREE.Vector3(0, 0, -1)
      .applyQuaternion(new THREE.Quaternion(o.x, o.y, o.z, o.w));
    const target = sprite.getWorldPosition(new THREE.Vector3());
    return (
      new THREE.Ray(origin, direction).distanceToPoint(target) < slack ||
      origin.distanceTo(target) < slack * 1.25 // near-field pinch directly on the sprite
    );
  }

  // --- live refresh (029, contracts/ar-viewer-nav.md) -------------------------------------
  // Poll the same served export the page loaded, matching the `obi web export` cron cadence
  // (data freshness is the cron's job — serve stays a static file server, like the other
  // ingests). A new generated_at swaps the scene contents in place; anything else — HTTP
  // error, network down, malformed JSON, missing site — is discarded and the last good
  // scene stays (FR-014). The in-scene freshness readout that 029 floated 1.6 m ahead was
  // REMOVED by operator decision (2026-07-06 VR pass: a translucent box dead in the view);
  // the export timestamp still shows on the setup page before entering.
  const POLL_MS = 5 * 60 * 1000;
  const dataState = { generatedAt: geometry.generated_at ?? null, geometry };

  // --- band visibility (044 US1, contracts/viewer-bands.md) --------------------------------
  // 078 US4/R7: the floating chip column is retired — band visibility is now a section of the
  // settings panel below. The toggle state/key (`obi.viewer.bands`) and the `applyBandVisibility`
  // plumbing are unchanged; toggling a band re-applies visibility and repaints the panel.
  function toggleBand(band) {
    if (hiddenBands.has(band)) hiddenBands.delete(band);
    else hiddenBands.add(band);
    saveHiddenBands(BANDS_KEY, hiddenBands);
    applyBandVisibility(world, hiddenBands);
    refreshPanel(); // the band rows + link-list dots follow the change
  }
  window.__obiDebug.bands = { hiddenBands };

  // --- icon row: gear + chip + links + help, two panels (078, amended 094 hud-links.md) -----
  // Head-locked camera children (survive live refreshes like the voice/band UI). The gear
  // opens/closes the settings panel, the chip outboard of it reads out the active mode, the
  // links button opens/closes the links panel (at most one panel open — each toggle closes the
  // other), the ? opens the controls cheat sheet; a panel is repainted only when its state
  // changes. All routing goes through selectHits/UV mapping ahead of the grab engine (FR-020).
  const gearButton = makeGearButton();
  camera.add(gearButton.sprite);
  const modeChip = makeModeChip();
  camera.add(modeChip.sprite);
  modeChip.paint(viewMode); // reflect a restored non-health mode immediately
  const helpButton = makeHelpButton();
  camera.add(helpButton.sprite);
  const helpCard = makeHelpCard();
  camera.add(helpCard.sprite);
  const linksButton = makeLinksButton();
  camera.add(linksButton.sprite);
  const settingsPanel = makeSettingsPanel();
  camera.add(settingsPanel.sprite);
  const linksPanel = makeSettingsPanel(LINKS_PANEL_W); // same factory, links-only state, wider (round 3)
  camera.add(linksPanel.sprite);
  const inspectCard = makeInspectCard();
  camera.add(inspectCard.sprite);
  let panelOpen = false;
  let linksOpen = false;
  let gearHover = false;
  let helpHover = false;
  let linksHover = false;
  let cardHover = false;
  let listPage = 0;             // the links panel's list page (078 US2, moved by 094)
  let inspectKey = null;        // linkKey of the inspected link, or null (no card)

  // The links panel's list: the fleet with pair-canonical names (round 4 — UISP's own
  // endpoint order scattered one hop's siblings across the alphabet), sorted by that shown
  // name, hidden bands included (US2 #6). Read live from the polled export so a refresh's
  // add/remove flows through (list + open card re-resolve; the card looks the raw link up
  // by key, so it keeps UISP's own label).
  function sortedLinks() {
    return [...(dataState.geometry.links ?? [])]
      .map((link) => ({ ...link, name: canonicalLinkName(link.name) }))
      .sort((a, b) => String(a.name ?? '').localeCompare(String(b.name ?? '')));
  }

  // The state the settings panel paints from: the mode selector, the scene toggle, and the
  // per-band visibility (BAND_ORDER, hidden bands included so they stay reachable). The link
  // list is the links panel's (094 FR-001).
  function panelState() {
    return {
      title: 'settings',
      modeRows: true,
      mode: viewMode,
      terrain: terrainOn,
      terrainStyle,
      // 098: the city-grid row's state — availability is per-site, from the fetched doc.
      brc: brcOn,
      brcAvailable: !!(brcDoc && brcDoc.sites && brcDoc.sites[siteName]),
      // 119: the backdrop row's state. Deliberately NOT called `mode` — that name is taken
      // above by the colour view mode (health/signal/utilization/fresnel).
      opaque,
      backdropAvailable: canFlipBackdrop,
      bands: bandsPresent(dataState.geometry.links).map(
        (band) => ({ band, hidden: hiddenBands.has(band) })
      ),
    };
  }
  // The links panel's state: the name-sorted fleet + page; `mode` colors the row dots; the
  // fleet count rides in the title bar.
  function linksPanelState() {
    const links = sortedLinks();
    return { title: `links (${links.length})`, mode: viewMode, links, listPage };
  }
  function refreshPanel() {
    if (panelOpen) settingsPanel.render(panelState());
    if (linksOpen) linksPanel.render(linksPanelState());
  }

  // ONE centered surface at a time (094 round 2): the settings panel, the links panel, the
  // help sheet, the inspect card, and the voice card all share the center dock — whatever is
  // triggered closes the rest. `except` names the surface about to show (or stay); the
  // title-bar ✕ calls this with no exception.
  function closeSurfaces(except) {
    if (except !== 'settings' && panelOpen) {
      panelOpen = false;
      settingsPanel.setOpen(false);
    }
    if (except !== 'links' && linksOpen) {
      linksOpen = false;
      linksPanel.setOpen(false);
    }
    if (except !== 'help' && helpCard.isVisible()) helpCard.hide();
    if (except !== 'inspect' && inspectKey !== null) hideInspectCard();
    if (except !== 'voice' && voiceCard && voiceCard.sprite.visible) {
      voiceCard.hide();
      lastVoiceState = 'idle';
    }
  }
  function togglePanel() {
    const opening = !panelOpen;
    closeSurfaces('settings');
    panelOpen = opening;
    settingsPanel.setOpen(panelOpen, panelState());
  }
  function toggleLinksPanel() {
    const opening = !linksOpen;
    closeSurfaces('links');
    linksOpen = opening;
    linksPanel.setOpen(linksOpen, linksPanelState());
  }
  function setListPage(page) {
    listPage = linkListPage(sortedLinks(), page).page; // clamp into range
    refreshPanel();
  }

  // The inspect card (078 US2): open by linkKey, re-resolve against the live snapshot on every
  // render so the 5-min refresh keeps it fresh; a dropped link shows the "gone" banner (US2 #5).
  function renderInspectCard() {
    if (inspectKey === null) return;
    const geo = dataState.geometry;
    const link = (geo.links ?? []).find((l) => linkKey(l.a, l.b) === inspectKey) ?? null;
    if (!link) {
      inspectCard.renderGone(); // keeps the last-known title + shows the "gone" banner
      return;
    }
    const endpointSite = new Map((geo.endpoints ?? []).map((e) => [e.name, e.site]));
    const endpointAmsl = new Map((geo.endpoints ?? []).map((e) => [e.name, e.height_amsl_m]));
    const profile = world.userData.profiles?.[link.id] ?? null;
    inspectCard.render({ link, endpointSite, endpointAmsl, profile });
  }
  function openInspectCard(key) {
    closeSurfaces('inspect'); // round 2: the links panel gives way, the card takes the dock
    inspectKey = key;
    renderInspectCard();
  }
  function hideInspectCard() {
    inspectKey = null;
    inspectCard.hide();
  }
  // Does this select hit the open card? (whole-card tap dismisses, voice-card idiom.) The card
  // sits beyond arm's reach, so the test is the exact plane rect, not a center-radius slack.
  function cardHit(event) {
    if (!inspectCard.isVisible()) return false;
    const ray = eventRay(event);
    return !!(ray && hudPlaneHit(ray.origin, ray.direction, inspectCard.sprite));
  }

  // The one mode-change path: recolor every link IN PLACE (no world rebuild — SC-005), show the
  // zone ellipsoids exactly while the fresnel view is active, persist the choice (FR-017), and
  // repaint the mode chip + the open panel so all of them stay consistent.
  function setMode(newMode) {
    viewMode = VIEW_MODES.includes(newMode) ? newMode : MODE_HEALTH;
    saveViewerPref(MODE_KEY, viewMode);
    applyMode(world, viewMode);
    if (world.userData.zones) world.userData.zones.visible = viewMode === MODE_FRESNEL;
    modeChip.paint(viewMode);
    refreshPanel();
  }

  function dispatchPanelAction(action) {
    switch (action.kind) {
      case 'mode':
        setMode(action.mode);
        break;
      case 'terrainCycle':
        cycleTerrain();
        break;
      case 'brc':
        toggleBrc();
        break;
      case 'backdrop':
        toggleBackdrop();
        break;
      case 'band':
        toggleBand(action.band);
        break;
      case 'link':
        openInspectCard(action.key);
        break;
      case 'pager':
        setListPage(listPage + action.dir);
        break;
      case 'close':
        closeSurfaces(); // the title-bar ✕: no exception — the dock empties
        break;
      default:
        break;
    }
  }

  // Does this select hit the gear button? (small target — the same near-field test the mic uses.)
  function gearHit(event) {
    return selectHits(event, gearButton.sprite, 0.08 * HUD_AT);
  }

  function helpHit(event) {
    return selectHits(event, helpButton.sprite, 0.06 * HUD_AT);
  }

  function linksButtonHit(event) {
    return selectHits(event, linksButton.sprite, 0.06 * HUD_AT);
  }

  // Intersect a world-space ray with a head-locked sprite's plane. The sprite is a camera child,
  // so the ray is taken into camera-local space and intersected with the plane z = sprite.z; the
  // hit normalizes to [0,1]² sprite UV (canvas px = UV × canvas size) and also comes back as a
  // world-space point so the aim dot can sit EXACTLY where a tap would land (2026-07-17 field
  // fix: a fixed-length dot + controller-below-eye parallax made taps land rows below the dot).
  // `pad` widens the accepted rect in UV so the hover dot doesn't flicker right at the edge.
  function hudPlaneHit(originWorld, dirWorld, sprite, pad = 0) {
    camera.updateWorldMatrix(true, false);
    const originLocal = camera.worldToLocal(originWorld.clone());
    const dirLocal = dirWorld.clone()
      .applyQuaternion(camera.getWorldQuaternion(new THREE.Quaternion()).invert());
    if (Math.abs(dirLocal.z) < 1e-6) return null;
    const t = (sprite.position.z - originLocal.z) / dirLocal.z;
    if (t <= 0) return null; // plane is behind the ray origin
    const hitLocal = originLocal.addScaledVector(dirLocal, t);
    const u = (hitLocal.x - (sprite.position.x - sprite.scale.x / 2)) / sprite.scale.x;
    const v = ((sprite.position.y + sprite.scale.y / 2) - hitLocal.y) / sprite.scale.y;
    if (u < -pad || u > 1 + pad || v < -pad || v > 1 + pad) return null;
    return { u, v, world: camera.localToWorld(hitLocal.clone()) };
  }

  // A select event's target ray as world-space origin + direction (null when untracked).
  function eventRay(event) {
    if (!event.frame) return null;
    const referenceSpace = renderer.xr.getReferenceSpace();
    if (!referenceSpace) return null;
    const pose = event.frame.getPose(event.inputSource.targetRaySpace, referenceSpace);
    if (!pose) return null;
    const p = pose.transform.position;
    const o = pose.transform.orientation;
    return {
      origin: new THREE.Vector3(p.x, p.y, p.z),
      direction: new THREE.Vector3(0, 0, -1)
        .applyQuaternion(new THREE.Quaternion(o.x, o.y, o.z, o.w)),
    };
  }

  // Map a select's target ray onto an open panel's sprite UV. null = the ray missed.
  function panelHitUV(event, panel) {
    if (!panel.isOpen()) return null;
    const ray = eventRay(event);
    if (!ray) return null;
    const hit = hudPlaneHit(ray.origin, ray.direction, panel.sprite);
    return hit && { u: hit.u, v: hit.v };
  }

  // A select that lands anywhere on the open panel is consumed (returns a non-null object even for
  // a background tap) so it never reaches the grab engine; `.action` is the row action or null.
  function panelHit(event, panel) {
    const uv = panelHitUV(event, panel);
    if (!uv) return null;
    return { action: panel.hitTest(uv.u, uv.v) };
  }

  window.__obiDebug.settings = {
    panel: settingsPanel, button: gearButton, card: inspectCard,
    links: linksPanel, linksButton,
    get mode() { return viewMode; },
    get open() { return panelOpen; },
    get linksOpen() { return linksOpen; },
    get inspecting() { return inspectKey; },
  };

  async function pollOnce() {
    try {
      const response = await fetch('links.json', { cache: 'no-cache' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const fresh = await response.json();
      const valid = fresh && Array.isArray(fresh.sites) && Array.isArray(fresh.endpoints)
        && Array.isArray(fresh.links);
      if (valid && fresh.generated_at !== dataState.generatedAt) {
        // validates the site before touching the scene; band toggles + view mode survive (044/078)
        applyGeometry(world, fresh, siteName, hiddenBands, viewMode);
        dataState.generatedAt = fresh.generated_at;
        dataState.geometry = fresh;
        refreshPanel();        // the panel's bands + link list + dots follow the data (078)
        renderInspectCard();   // an open card re-resolves by linkKey (fresh values or "gone")
      }
    } catch {
      // keep the last good scene; the next tick tries again (FR-014)
    }
  }
  const pollId = setInterval(pollOnce, POLL_MS);
  session.addEventListener('end', () => clearInterval(pollId));

  // --- terrain intake (031, contracts/ar-viewer-terrain.md) --------------------------------
  // Exactly ONE terrain.json fetch per session — the artifact is static (the export
  // regenerates it, but grids/profiles only change when the network or the DEM does), so
  // it is deliberately NOT part of pollOnce. Runs after the session is established so the
  // requestSession user-activation window is never spent on network I/O. Any failure —
  // 404 (no export yet), network, malformed JSON — leaves the 029 scene untouched.
  (async () => {
    let terrainSite; // captured for the 098 city-grid drape below
    try {
      const response = await fetch('terrain.json', { cache: 'no-cache' });
      if (response.ok) {
        const doc = await response.json();
        if (doc && typeof doc === 'object') {
          world.userData.profiles =
            doc.profiles && typeof doc.profiles === 'object' ? doc.profiles : {};
          terrainSite = doc.sites?.[siteName];
          // 119: `terrainStyle` MUST stay the live binding here, never a value captured when
          // this fetch started — the operator can flip the backdrop while terrain.json is in
          // flight, and the terrain must arrive in the style belonging to the backdrop they
          // are actually looking at (spec edge case, contract C6).
          buildTerrain(world.userData.terrain, terrainSite, terrainStyle);
          // Rebuild the link objects so ground profiles + verdict labels join in; the terrain
          // group, all toggle states (zones, terrain, bands), the view mode, and the world
          // transform survive.
          applyGeometry(world, dataState.geometry, siteName, hiddenBands, viewMode);
          renderInspectCard(); // now that profiles are loaded, an open card gains its chart
        }
      }
    } catch {
      // terrain stays absent; the links render exactly as 029 (FR-014)
    }
    // 098 (C5/G1): the city-grid intake launches after the terrain fetch SETTLES — success
    // or failure — because the drape prefers the site's terrain grid but must never be
    // hostage to terrain availability.
    intakeBrc(terrainSite);
  })();

  // --- BRC city-grid intake (098, contracts/brc-artifacts.md C5) ---------------------------
  // Exactly ONE brc.json fetch per session — the artifact is static per season, so like
  // terrain.json it is deliberately NOT part of pollOnce. Any failure — 404 (no artifact),
  // malformed JSON, no anchor for this site — leaves the scene and panel untouched: the
  // terrain-absent experience (FR-007). One LineSegments per feature kind, all draped onto
  // the terrain heightfield (flat when absent) by the pure brc.js math.
  async function intakeBrc(terrainSite) {
    try {
      const response = await fetch('brc.json', { cache: 'no-cache' });
      if (!response.ok) return;
      const doc = await response.json();
      if (!doc || typeof doc !== 'object') return;
      const built = buildCityGeometry(doc, siteName, makeHeightSampler(terrainSite));
      if (!built || !Object.keys(built).length) return; // out of range / nothing usable
      const group = world.userData.brc;
      for (const [kind, positions] of Object.entries(built)) {
        const lineGeometry = new THREE.BufferGeometry();
        lineGeometry.setAttribute(
          'position', new THREE.Float32BufferAttribute(positions, 3)
        );
        const lines = new THREE.LineSegments(
          lineGeometry,
          new THREE.LineBasicMaterial({
            color: BRC_COLORS[kind] ?? BRC_COLORS.streets,
            transparent: true,
            opacity: BRC_OPACITY[kind] ?? 0.5,
            depthWrite: false,    // passthrough + shaded terrain stay visible through it
          })
        );
        lines.renderOrder = 0;    // over the shaded terrain (renderOrder −1, polygon-offset)
        group.add(lines);
      }
      // Field round 1 (2026-08-25): the terrain wireframe stops at the trash fence — inside
      // it the playa IS the city floor, and grid lines under the streets read as noise. The
      // wire mesh gets a second index with every fence-touching triangle dropped (shared
      // position attribute, so the displaced heights ride along); the shaded fill is never
      // holed — the ground itself stays. Applied only while the city layer is on.
      const ring = fenceRing(doc, siteName);
      const wire = findTerrainWire();
      if (ring && wire && wire.geometry.index) {
        const pos = wire.geometry.attributes.position;
        const masked = maskWireIndex(
          wire.geometry.index.array, (i) => [pos.getX(i), -pos.getZ(i)], ring
        );
        const holed = new THREE.BufferGeometry();
        holed.setAttribute('position', pos);
        holed.setIndex(masked);
        wire.userData.fullGeometry = wire.geometry;
        wire.userData.fencedGeometry = holed;
        if (brcOn) wire.geometry = holed;
      }
      group.visible = brcOn;
      brcDoc = doc;
      refreshPanel(); // an already-open settings panel gains the CITY GRID row immediately
    } catch {
      // the city stays absent; everything else renders exactly as before (FR-007)
    }
  }

  // --- grab-gesture engine (029, research R1/R3/R4) ---------------------------------------
  // Hands and controllers fire the same select events for pinch/trigger, so one engine
  // covers both. Poses are read per frame from targetRaySpace; a source that stops tracking
  // simply stops producing poses (its grab lies dormant) and is dropped for good when the
  // session removes it (FR-006).
  const grabs = new Map(); // XRInputSource → {startTime, position: Vector3|null, moved}

  session.addEventListener('selectstart', (event) => {
    // Voice routing first (032 FR-002/FR-003): a select STARTING on the talk button is a
    // push-to-talk hold and never reaches the grab engine; selects anywhere else never
    // touch the button. A tap on a settled voice card dismisses it (and grabs nothing).
    if (talk && talkButton && selectHits(event, talkButton.sprite, 0.1 * HUD_AT)) {
      voiceHolds.add(event.inputSource);
      talk.holdStart();
      return;
    }
    // Settings gear + panel next (078 US1): a select on the gear toggles the panel; a select
    // anywhere on the OPEN panel dispatches its row action (or is swallowed) and never reaches the
    // grab engine — the exact talk-button routing discipline, so a panel tap never pans (FR-020).
    if (gearHit(event)) {
      togglePanel();
      return;
    }
    if (helpHit(event)) {
      if (helpCard.isVisible()) {
        helpCard.hide();
      } else {
        closeSurfaces('help'); // round 2: one centered surface at a time
        helpCard.show();
      }
      return;
    }
    // The links button + links panel (094): the exact gear/panel routing discipline.
    if (linksButtonHit(event)) {
      toggleLinksPanel();
      return;
    }
    if (panelOpen) {
      const hit = panelHit(event, settingsPanel);
      if (hit) {
        if (hit.action) dispatchPanelAction(hit.action);
        return;
      }
    }
    if (linksOpen) {
      const hit = panelHit(event, linksPanel);
      if (hit) {
        if (hit.action) dispatchPanelAction(hit.action);
        return;
      }
    }
    // Inspect card dismiss (078 US2): a tap anywhere on the open card closes it (the ✕ affordance)
    // and never reaches the grab engine.
    if (cardHit(event)) {
      hideInspectCard();
      return;
    }
    // Help-card dismiss: a tap anywhere on the open cheat sheet closes it (card idiom).
    if (helpCard.isVisible()) {
      const ray = eventRay(event);
      if (ray && hudPlaneHit(ray.origin, ray.direction, helpCard.sprite)) {
        helpCard.hide();
        return;
      }
    }
    // The ask-obi card is ALWAYS closable while visible (2026-07-17 field pass, round 5: a turn
    // stuck in submitting/thinking left an undismissable card). Speech keeps playing; if an
    // in-flight answer lands later it re-opens the card with content — itself dismissible.
    if (voiceCard && voiceCard.sprite.visible) {
      const ray = eventRay(event);
      const onCard = (ray && hudPlaneHit(ray.origin, ray.direction, voiceCard.sprite) !== null)
        || selectHits(event, voiceCard.sprite, 0.36); // near-field pinch fallback
      if (onCard) {
        voiceCard.hide();
        lastVoiceState = 'idle';
        return;
      }
    }
    grabs.set(event.inputSource, {
      startTime: performance.now(),
      position: null, // filled by the first tracked frame
      moved: 0,
    });
  });
  session.addEventListener('selectend', (event) => {
    if (voiceHolds.delete(event.inputSource)) {
      if (talk) talk.holdEnd(); // release on the button = submit
      return;
    }
    const grab = grabs.get(event.inputSource);
    grabs.delete(event.inputSource);
    if (!grab) return;
    const duration = performance.now() - grab.startTime;
    if (duration < TAP_MS && grab.moved < TAP_MOVE_M) cycleMode(); // 094 FR-007: next view
  });
  session.addEventListener('inputsourceschange', (event) => {
    for (const source of event.removed) {
      grabs.delete(source); // FR-006: end cleanly
      stickGestures.delete(source); // 080 FR-010: a vanished controller drops its latch
      // A PTT source that vanished mid-hold cannot release: drop the capture, never submit.
      if (voiceHolds.delete(source) && talk) talk.cancel();
      if (source === squeezeSource) {
        squeezeSource = null;
        if (talk) talk.cancel();
      }
      const dots = pointerDots.get(source);
      if (dots) {
        pointerDots.delete(source);
        for (const dot of Object.values(dots)) {
          pointers.remove(dot);
          disposeObject(dot);
        }
      }
    }
  });

  // Fingertip + aim markers (032 field feedback): in VR mode the operator's real hands are
  // invisible, so show index/thumb tips (hands) and a small aim dot where each target ray
  // crosses the voice-UI shell — and highlight the talk button while an aim ray is on it.
  function updateVoicePointers(frame, referenceSpace) {
    let hovering = false;
    let cardHovering = false;
    let gearHovering = false; // the settings gear currently under an aim ray (078)
    let helpHovering = false; // the help ? button currently under an aim ray
    let linksHovering = false; // the links button currently under an aim ray (094)
    let inspectHovering = false; // the inspect card currently under an aim ray (078)
    const buttonTarget = talkButton
      ? talkButton.sprite.getWorldPosition(new THREE.Vector3()) : null;
    // The ✕ highlight must be truthful: light it only when a tap would actually dismiss —
    // which is now ANY visible state (round 5: the card is always closable).
    const cardDismissible = voiceCard && voiceCard.sprite.visible;
    const cardTarget = cardDismissible
      ? voiceCard.sprite.getWorldPosition(new THREE.Vector3()) : null;
    for (const source of session.inputSources) {
      let dots = pointerDots.get(source);
      if (!dots) {
        dots = {
          beam: makePointerBeam(),
          ray: makePointerDot(0xffffff, 0.006),
          index: makePointerDot(0x6cb6ff, 0.008),
          thumb: makePointerDot(0x6cb6ff, 0.008),
        };
        pointers.add(dots.beam, dots.ray, dots.index, dots.thumb);
        pointerDots.set(source, dots);
      }
      const pose = frame.getPose(source.targetRaySpace, referenceSpace);
      if (pose) {
        const p = pose.transform.position;
        const o = pose.transform.orientation;
        const origin = new THREE.Vector3(p.x, p.y, p.z);
        const direction = new THREE.Vector3(0, 0, -1)
          .applyQuaternion(new THREE.Quaternion(o.x, o.y, o.z, o.w));
        // The aim dot snaps to the panel/card plane when the ray is on it (2026-07-17 field
        // fix): the dot then marks the exact point a tap resolves to, instead of a fixed-length
        // point whose parallax against the plane landed taps rows below where the dot showed.
        let end = origin.clone().addScaledVector(direction, VOICE_UI_DISTANCE);
        // At most one of the two panels is open (094 FR-005) — snap to whichever it is.
        const openPanel = panelOpen ? settingsPanel : (linksOpen ? linksPanel : null);
        const panelSnap = openPanel
          ? hudPlaneHit(origin, direction, openPanel.sprite, 0.06) : null;
        const cardSnap = !panelSnap && inspectCard.isVisible()
          ? hudPlaneHit(origin, direction, inspectCard.sprite, 0.06) : null;
        const helpSnap = !panelSnap && !cardSnap && helpCard.isVisible()
          ? hudPlaneHit(origin, direction, helpCard.sprite, 0.06) : null;
        if (panelSnap) end = panelSnap.world;
        else if (cardSnap) end = cardSnap.world;
        else if (helpSnap) end = helpSnap.world;
        dots.ray.position.copy(end);
        dots.ray.visible = true;
        const positions = dots.beam.geometry.attributes.position;
        positions.setXYZ(0, origin.x, origin.y, origin.z);
        positions.setXYZ(1, end.x, end.y, end.z);
        positions.needsUpdate = true;
        dots.beam.visible = true;
        const aimRay = new THREE.Ray(origin, direction);
        const onButton = buttonTarget && (
          aimRay.distanceToPoint(buttonTarget) < 0.1 * HUD_AT ||
          origin.distanceTo(buttonTarget) < 0.125 * HUD_AT
        );
        if (onButton) hovering = true;
        // Settings gear + help hover (078): highlight when an aim ray is on them, like the mic.
        const gearTarget = gearButton.sprite.getWorldPosition(new THREE.Vector3());
        if (aimRay.distanceToPoint(gearTarget) < 0.08 * HUD_AT
          || origin.distanceTo(gearTarget) < 0.1 * HUD_AT) {
          gearHovering = true;
        }
        const helpTarget = helpButton.sprite.getWorldPosition(new THREE.Vector3());
        if (aimRay.distanceToPoint(helpTarget) < 0.06 * HUD_AT
          || origin.distanceTo(helpTarget) < 0.08 * HUD_AT) {
          helpHovering = true;
        }
        const linksTarget = linksButton.sprite.getWorldPosition(new THREE.Vector3());
        if (aimRay.distanceToPoint(linksTarget) < 0.06 * HUD_AT
          || origin.distanceTo(linksTarget) < 0.08 * HUD_AT) {
          linksHovering = true;
        }
        // Inspect card hover (078): light its ✕ when a ray is on the card plane, like the panel.
        if (cardSnap) inspectHovering = true;
        // Button wins for this ray (selectstart routes it to PTT before the card test).
        if (cardTarget && !onButton && (
          aimRay.distanceToPoint(cardTarget) < 0.36 ||
          origin.distanceTo(cardTarget) < 0.45
        )) {
          cardHovering = true;
        }
      } else {
        dots.ray.visible = false;
        dots.beam.visible = false;
      }
      for (const [key, jointName] of [['index', 'index-finger-tip'], ['thumb', 'thumb-tip']]) {
        let visible = false;
        if (source.hand && frame.getJointPose) {
          const joint = source.hand.get(jointName);
          const jointPose = joint ? frame.getJointPose(joint, referenceSpace) : null;
          if (jointPose) {
            const jp = jointPose.transform.position;
            dots[key].position.set(jp.x, jp.y, jp.z);
            visible = true;
          }
        }
        dots[key].visible = visible;
      }
    }
    // Repaint only on hover CHANGE, and never while recording (the active paint wins).
    if (talkButton && hovering !== voiceHover) {
      voiceHover = hovering;
      if (lastVoiceState !== 'listening') {
        talkButton.paint(null, hovering ? 'hover' : 'idle');
      }
    }
    if (voiceCard) voiceCard.highlightClose(cardDismissible && cardHovering);
    if (gearHovering !== gearHover) {
      gearHover = gearHovering;
      gearButton.paint(gearHover);
    }
    if (helpHovering !== helpHover) {
      helpHover = helpHovering;
      helpButton.paint(helpHover);
    }
    if (linksHovering !== linksHover) {
      linksHover = linksHovering;
      linksButton.paint(linksHover);
    }
    if (inspectHovering !== cardHover) {
      cardHover = inspectHovering;
      inspectCard.highlightClose(cardHover);
    }
  }

  // One anchor per gesture: computed from the gaze when the number of tracked grabs
  // changes, held fixed while the gesture lasts (a moving anchor feels like swimming).
  const gesture = { count: 0, anchor: new THREE.Vector3(), amp: 1 };

  // 080: stick deflections get the same latch discipline (contracts/xr-nav.md). One
  // entry per controller: `active` tracks the radial-deadzone state machine, `anchor`
  // is the zoom/yaw pivot latched at engage, `smoothedDistance` the flight-speed EMA,
  // `prevRight` the last valid horizontal strafe basis. Fully disjoint from the grab
  // engine's state above — simultaneous grab + stick input composes without fighting.
  const stickGestures = new Map(); // XRInputSource → {active, anchor, smoothedDistance, prevRight}
  let prevStickTime = null; // previous animation-loop timestamp (ms) — dt source

  function updateGrabs(frame, referenceSpace) {
    // Current poses for every grabbing source that is tracked this frame.
    const tracked = [];
    for (const [source, grab] of grabs) {
      const pose = frame.getPose(source.targetRaySpace, referenceSpace);
      if (!pose) continue; // tracking dropout: hold position, no runaway (FR-006)
      const { x, y, z } = pose.transform.position;
      tracked.push({ grab, position: new THREE.Vector3(x, y, z) });
    }

    if (tracked.length !== gesture.count) {
      gesture.count = tracked.length;
      if (tracked.length > 0) {
        const { point, distance } = gazeAnchor(camera, world);
        gesture.anchor.copy(point);
        gesture.amp = Math.max(1, distance / GRAB_REACH_M);
      }
    }

    if (tracked.length === 1) {
      const { grab, position } = tracked[0];
      if (grab.position) {
        // One grab = pan the content plane under the gaze (drag the map, phone-style).
        const delta = position.clone().sub(grab.position);
        grab.moved += delta.length(); // tap detection stays in physical hand meters
        world.position.add(delta.multiplyScalar(gesture.amp));
        clampPan(world.position);
      }
      grab.position = position;
    } else if (tracked.length >= 2) {
      const [first, second] = tracked; // oldest two grabs steer; extras just accumulate
      if (first.grab.position && second.grab.position) {
        const prevSpan = second.grab.position.clone().sub(first.grab.position);
        const currSpan = second.position.clone().sub(first.position);
        const prevMid = first.grab.position.clone().add(second.grab.position)
          .multiplyScalar(0.5);
        const currMid = first.position.clone().add(second.position).multiplyScalar(0.5);
        // Two grabs = zoom by the span ratio + yaw by the span's heading change, both about
        // the gaze anchor (the looked-at link magnifies/turns in place — scaling about the
        // hands themselves is angularly invisible at km scale), plus amplified midpoint pan.
        world.position.add(currMid.clone().sub(prevMid).multiplyScalar(gesture.amp));
        if (prevSpan.length() > 1e-4 && currSpan.length() > 1e-4) {
          const scaleFactor = currSpan.length() / prevSpan.length();
          let yawDelta = Math.atan2(currSpan.x, currSpan.z)
            - Math.atan2(prevSpan.x, prevSpan.z);
          if (yawDelta > Math.PI) yawDelta -= 2 * Math.PI;
          if (yawDelta < -Math.PI) yawDelta += 2 * Math.PI;
          applyAboutPoint(world, gesture.anchor, scaleFactor, yawDelta);
        }
        clampPan(world.position);
      }
      for (const { grab, position } of tracked) {
        if (grab.position) grab.moved += position.distanceTo(grab.position);
        grab.position = position;
      }
    }
  }

  // Fresnel zone toggle (025 FR-008) and terrain toggle (031 FR-013): edge-triggered per
  // input source so one press flips once, no repeat while held. Controllers without the
  // button simply never toggle that layer.
  const zoneButtonWasPressed = new WeakMap();
  const terrainButtonWasPressed = new WeakMap();

  renderer.setAnimationLoop((time, frame) => {
    const referenceSpace = renderer.xr.getReferenceSpace();
    if (frame && referenceSpace) updateGrabs(frame, referenceSpace);
    // Pointer feedback serves the settings gear + panel in every session, voice UI when enabled.
    if (frame && referenceSpace) updateVoicePointers(frame, referenceSpace);

    // Thumbsticks (080, contracts/xr-nav.md — replaces the 022/029 per-frame model whose
    // room-axis pan, origin-pivot yaw, and wandering zoom anchor were the 2026-07-17
    // field complaints): each radial-deadzone exit is ONE gesture with state latched at
    // engage, the grab engine's discipline. Left stick flies along the current gaze
    // (forward) and strafes horizontally (sideways) at a speed proportional to the
    // EMA-smoothed gaze-content distance — fast over kilometers, decelerating on
    // approach. Right stick zooms (Y) and yaws (X) through one applyAboutPoint call
    // about the latched anchor: the looked-at content scales and turns in place.
    // Trigger-drag/tap need no code here — controller triggers fire the same select
    // events the grab engine already handles. Hands also surface a vestigial gamepad on
    // Quest — skip it; hands are select-only. (xr-standard gamepad mapping: axes[2]/[3]
    // are the thumbstick; fall back to [0]/[1].)
    const dt = nav.frameDt(time, prevStickTime);
    prevStickTime = time;
    for (const source of session.inputSources) {
      const gamepad = source.gamepad;
      if (!gamepad || source.hand) continue;
      const axes = gamepad.axes;
      if (axes?.length) {
        const x = axes.length >= 4 ? axes[2] : axes[0];
        const y = axes.length >= 4 ? axes[3] : axes[1];
        let stickGesture = stickGestures.get(source);
        if (!stickGesture) {
          stickGesture = {
            active: false, anchor: null, smoothedDistance: null,
            prevForward: { x: 0, y: 0, z: -1 },
            prevRight: { x: 1, y: 0, z: 0 },
          };
          stickGestures.set(source, stickGesture);
        }
        const stick = nav.remapDeflection(x ?? 0, y ?? 0, stickGesture.active);
        stickGesture.active = stick.active;
        if (stick.justReleased) {
          stickGesture.anchor = null; // next engage latches fresh from the current gaze
          stickGesture.smoothedDistance = null;
        }
        if (source.handedness === 'left') {
          if (stick.active && stick.magnitude > 0) {
            // Push toward what you want to reach: the world slides past the operator,
            // strictly in the horizontal plane (field round 1 reversed the fly-along-gaze
            // design: a forward component down the view pitch read as zoom, not travel —
            // altitude never changes; approach the ground with zoom instead). Forward =
            // the view heading flattened to the horizon, sideways = the horizontal right.
            // Speed follows the gaze-content distance every frame (deceleration on
            // approach), EMA-smoothed so a nearest-content change never steps it; the
            // terrain part of that distance may lag ~150 ms (cached raycast) — invisible
            // under the smoothing.
            stickGesture.smoothedDistance = nav.smoothDistance(
              stickGesture.smoothedDistance,
              gazeAnchor(camera, world, TERRAIN_HIT_MAX_AGE_MS).distance,
              dt, nav.DISTANCE_TAU_S);
            const step = stickGesture.smoothedDistance * PAN_RATE_PER_S * dt;
            stickGesture.prevForward = nav.horizontalBasis(
              camera.getWorldDirection(new THREE.Vector3()), stickGesture.prevForward);
            stickGesture.prevRight = nav.horizontalBasis(
              new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0),
              stickGesture.prevRight);
            const advance = -stick.y * step; // stick up = move toward what you face
            const strafe = stick.x * step;   // stick right = strafe along the horizon
            world.position.x -= stickGesture.prevForward.x * advance
              + stickGesture.prevRight.x * strafe;
            world.position.z -= stickGesture.prevForward.z * advance
              + stickGesture.prevRight.z * strafe;
            clampPan(world.position);
          }
        } else {
          // Right stick — and unknown handedness, which keeps its 022 degraded role of
          // turn-only — steers about the anchor latched at engage; zoom input at a scale
          // clamp is absorbed by applyAboutPoint's survived-factor rule (no creep).
          if (stick.justEngaged) stickGesture.anchor = gazeAnchor(camera, world).point;
          if (stick.active && stickGesture.anchor && stick.magnitude > 0) {
            const zoom = source.handedness === 'right'
              ? Math.exp(-stick.y * ZOOM_RATE_PER_S * dt)
              : 1;
            // Field round 1: yaw sign flipped — the 022 direction was tuned for the sweep
            // about the distant map origin; about the looked-at content it read backwards.
            applyAboutPoint(world, stickGesture.anchor, zoom,
              stick.x * YAW_RATE_PER_S * dt);
          }
        }
      }
      const pressed = gamepad.buttons?.[ZONE_BUTTON]?.pressed ?? false;
      if (pressed && !zoneButtonWasPressed.get(source)) cycleMode();
      zoneButtonWasPressed.set(source, pressed);
      const terrainPressed = gamepad.buttons?.[TERRAIN_BUTTON]?.pressed ?? false;
      if (terrainPressed && !terrainButtonWasPressed.get(source)) cycleTerrain();
      terrainButtonWasPressed.set(source, terrainPressed);
    }
    renderer.render(scene, camera);
  });
  return session;
}
