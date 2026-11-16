// Headless harness for the 080 stick-nav math in nav.js — runs the REAL pure functions in
// node and asserts the contract numbers (specs/080-xr-nav/contracts/xr-nav.md): the radial
// remap ramp (zero at the deadzone edge, 0.5 at m=0.575, 1 at full deflection, direction
// preserved on diagonals), the engage/release hysteresis state machine with its
// justEngaged/justReleased edges, horizontalBasis projection + sticky degenerate fallback,
// smoothDistance seeding + dt-correctness (one 100 ms step == ten 10 ms steps), and the
// frameDt clamp. The three shipped nav bugs all passed every text tripwire — executing the
// math is the layer that catches them. Prints one JSON summary line, exits non-zero on
// any failure.
//
// Usage: node nav_headless.mjs [static-dir]   (defaults to src/obi/web/static beside repo)
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const staticDir = resolve(process.argv[2] ?? resolve(HERE, '../../src/obi/web/static'));

const nav = await import(`${staticDir}/nav.js`);
const {
  STICK_ENGAGE, STICK_RELEASE, DISTANCE_TAU_S, DT_MAX_S,
  remapDeflection, horizontalBasis, smoothDistance, frameDt,
} = nav;

let checks = 0;
const failures = [];
function check(name, ok) {
  checks += 1;
  if (!ok) failures.push(name);
}
function close(a, b, eps = 1e-9) {
  return Math.abs(a - b) < eps;
}

// --- constants are the contract's -----------------------------------------------------------

check('engage threshold', STICK_ENGAGE === 0.15);
check('release threshold', STICK_RELEASE === 0.12);
check('tau', DISTANCE_TAU_S === 0.25);
check('dt ceiling', DT_MAX_S === 0.05);

// --- remap ramp values (contract: (m − 0.15)/(1 − 0.15), direction preserved) ---------------

{
  const atEdge = remapDeflection(0.15 + 1e-12, 0, false);
  check('ramp ~0 just past the edge', atEdge.active && close(atEdge.magnitude, 0, 1e-6));
  const mid = remapDeflection(0.575, 0, false);
  check('ramp 0.5 at m=0.575', close(mid.magnitude, 0.5));
  check('mid output x', close(mid.x, 0.5));
  check('mid output y', close(mid.y, 0));
  const full = remapDeflection(0, -1, false);
  check('ramp 1 at full deflection', close(full.magnitude, 1));
  check('full output preserves direction', close(full.x, 0) && close(full.y, -1));
  // diagonal: direction preserved, magnitude radial (m = 1 exactly at 45° with 0.7071..)
  const d = Math.SQRT1_2;
  const diag = remapDeflection(d, d, false);
  check('diagonal magnitude radial', close(diag.magnitude, (1 - 0.15) / (1 - 0.15) * 1, 1e-9)
    || close(diag.magnitude, (1 - 0.15) / 0.85, 1e-9)); // m=1 → ramp 1
  check('diagonal direction preserved',
    close(diag.x / diag.y, 1, 1e-9) && diag.x > 0);
  const below = remapDeflection(0.1, 0.05, false);
  check('inside deadzone: inactive, zero output',
    !below.active && below.x === 0 && below.y === 0 && below.magnitude === 0);
  const overdrive = remapDeflection(1.2, 0, false); // some sticks report past 1.0
  check('ramp clamps at 1', close(overdrive.magnitude, 1));
}

// --- hysteresis state machine (idle→0.16→active→0.13 holds→0.11→idle) -----------------------

{
  const engaged = remapDeflection(0.16, 0, false);
  check('engages above 0.15', engaged.active && engaged.justEngaged && !engaged.justReleased);
  const holding = remapDeflection(0.13, 0, true);
  check('0.13 while active stays active', holding.active && !holding.justEngaged);
  check('hysteresis band output is zero (latch holds, nothing moves)',
    close(holding.magnitude, 0) && close(holding.x, 0));
  const released = remapDeflection(0.11, 0, true);
  check('releases below 0.12', !released.active && released.justReleased);
  const idle = remapDeflection(0.13, 0, false);
  check('0.13 from idle does NOT engage', !idle.active && !idle.justEngaged);
  const reengage = remapDeflection(0.5, 0, false);
  check('re-engage after release is a fresh edge', reengage.justEngaged);
  check('NaN axes stay inactive', !remapDeflection(NaN, NaN, false).active);
}

// --- horizontalBasis: horizontal projection, sticky degenerate fallback -------------------------

{
  const prev = { x: 1, y: 0, z: 0 };
  const tilted = horizontalBasis({ x: 0.6, y: 0.5, z: 0.8 }, prev); // rolled/pitched camera +X
  check('projection drops the vertical component', tilted.y === 0);
  check('projection normalized', close(Math.hypot(tilted.x, tilted.z), 1));
  check('projection direction kept', close(tilted.x / tilted.z, 0.6 / 0.8));
  const degenerate = horizontalBasis({ x: 0.01, y: 0.999, z: 0.02 }, prev); // extreme roll
  check('degenerate keeps previous basis', degenerate === prev);
  const nanIn = horizontalBasis({ x: NaN, y: 0, z: NaN }, prev);
  check('NaN input keeps previous basis', nanIn === prev);
  const exact = horizontalBasis({ x: 0, y: 0, z: -1 }, prev);
  check('pure horizontal passes through', close(exact.z, -1) && close(exact.x, 0));
}

// --- smoothDistance: seeding + dt-correctness -----------------------------------------------

{
  check('null prev seeds with sample', smoothDistance(null, 42, 0.016, DISTANCE_TAU_S) === 42);
  check('undefined prev seeds too', smoothDistance(undefined, 7, 0.016, DISTANCE_TAU_S) === 7);
  const one = smoothDistance(0, 100, 0.1, DISTANCE_TAU_S);
  let ten = 0;
  for (let i = 0; i < 10; i += 1) ten = smoothDistance(ten, 100, 0.01, DISTANCE_TAU_S);
  check('one 100ms step == ten 10ms steps', close(one, ten, 1e-9));
  check('EMA converges toward the sample', one > 0 && one < 100);
  check('dt=0 is a no-op', smoothDistance(50, 100, 0, DISTANCE_TAU_S) === 50);
  check('negative dt is a no-op', smoothDistance(50, 100, -1, DISTANCE_TAU_S) === 50);
}

// --- frameDt: clamp + first-frame zero ------------------------------------------------------

{
  check('normal frame', close(frameDt(1016, 1000), 0.016));
  check('stall clamps to ceiling', frameDt(9000, 1000) === DT_MAX_S);
  check('first frame (null prev) is 0', frameDt(1000, null) === 0);
  check('backwards clock is 0', frameDt(1000, 2000) === 0);
  check('NaN now is 0', frameDt(NaN, 1000) === 0);
}

console.log(JSON.stringify({ target: 'nav', checks, failures }));
if (failures.length > 0) process.exit(1);
