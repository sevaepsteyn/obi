// Headless harness for the 098 BRC city-grid math in brc.js — runs the REAL pure module in
// node and asserts the contract (specs/098-brc-grid/contracts/brc-artifacts.md C4): the
// bilinear ground sampler (SiteTerrain orientation, dm→m, edge clamp, flat fallback) and
// buildCityGeometry (site-anchor translation, dm→m, ENU→scene axes x=east/z=−north,
// y = sample + LIFT_M, segment-pair emission, null on no anchor). Text tripwires can't
// catch a flipped row order or a missed anchor sign — executing the math does.
//
// Usage: node brc_headless.mjs [static-dir]   (defaults to src/obi/web/static beside repo)
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const staticDir = resolve(process.argv[2] ?? resolve(HERE, '../../src/obi/web/static'));

const brc = await import(`${staticDir}/brc.js`);
const { LIFT_M, makeHeightSampler, buildCityGeometry, fenceRing, pointInRing, maskWireIndex } = brc;

let checks = 0;
const failures = [];
function check(name, ok) {
  checks += 1;
  if (!ok) failures.push(name);
}
function close(a, b, eps = 1e-9) {
  return Math.abs(a - b) < eps;
}

// --- makeHeightSampler: SiteTerrain contract (data-model 031 §SiteTerrain) ------------------
// posts=3, half=100 ⇒ step=100; post (i, j) at ENU (−100+j·100, −100+i·100); heights in dm,
// row-major west→east, rows south→north. Grid: south row 0 dm, middle row 10 dm, north 20 dm.

{
  const site = {
    half_extent_m: 100,
    posts: 3,
    rel_heights_dm: [0, 0, 0, 10, 10, 10, 20, 20, 20],
  };
  const sample = makeHeightSampler(site);
  check('south edge is 0 m', close(sample(0, -100), 0));
  check('center row is 1 m', close(sample(0, 0), 1));
  check('north edge is 2 m', close(sample(0, 100), 2));
  check('bilinear midpoint north-south', close(sample(0, 50), 1.5));
  check('east-west constant rows', close(sample(-100, 50), 1.5) && close(sample(100, 50), 1.5));
  check('outside clamps to the edge (north)', close(sample(0, 5000), 2));
  check('outside clamps to the edge (south-west)', close(sample(-5000, -5000), 0));
}

{
  // A west→east gradient distinguishes column from row indexing (the transposition trap).
  const site = { half_extent_m: 100, posts: 3, rel_heights_dm: [0, 10, 20, 0, 10, 20, 0, 10, 20] };
  const sample = makeHeightSampler(site);
  check('west edge is 0 m', close(sample(-100, 0), 0));
  check('east edge is 2 m', close(sample(100, 0), 2));
  check('bilinear midpoint west-east', close(sample(50, 0), 1.5));
}

check('no terrain ⇒ flat 0', makeHeightSampler(undefined)(123, -456) === 0);
check('malformed grid ⇒ flat 0',
  makeHeightSampler({ half_extent_m: 100, posts: 3, rel_heights_dm: [1, 2, 3] })(0, 0) === 0);

// --- buildCityGeometry: anchor + axes + segments (contract C4) ------------------------------

const DOC = {
  year: 2026,
  sites: { mast: { east_m: 100, north_m: -50 } },
  features: [
    { kind: 'streets', lines: [[0, 0, 10, 0, 10, 10]] },  // dm: (0,0)→(1,0)→(1,1) m
    { kind: 'trash_fence', lines: [[0, 0, 20, 0]] },
  ],
};

{
  const built = buildCityGeometry(DOC, 'mast', () => 5);
  check('per-kind arrays', built !== null && 'streets' in built && 'trash_fence' in built);
  const s = built.streets;
  check('two segments = 12 floats', s.length === 12);
  // v0 = spike-frame (0,0) m + anchor (100,−50) ⇒ site ENU (100,−50) ⇒ scene (100, y, 50)
  check('anchor applied + z = −north', close(s[0], 100) && close(s[2], 50));
  check('y = sample + LIFT', close(s[1], 5 + LIFT_M));
  // v1 = (1,0)+anchor ⇒ (101,−50) ⇒ scene x=101, z=50; v2 = (1,1)+anchor ⇒ scene z=49
  check('dm→m on east', close(s[3], 101) && close(s[5], 50));
  check('segment pair repeats the shared vertex', close(s[6], s[3]) && close(s[8], s[5]));
  check('northward vertex decreases z', close(s[11], 49));
}

{
  const flat = buildCityGeometry(DOC, 'mast', makeHeightSampler(undefined));
  check('flat drape sits at LIFT', close(flat.streets[1], LIFT_M));
}

check('no anchor for the site ⇒ null', buildCityGeometry(DOC, 'elsewhere', () => 0) === null);
check('no sites at all ⇒ null', buildCityGeometry({ features: [] }, 'mast', () => 0) === null);
check('anchored but featureless ⇒ empty object', (() => {
  const built = buildCityGeometry({ sites: { mast: { east_m: 0, north_m: 0 } } }, 'mast', () => 0);
  return built !== null && Object.keys(built).length === 0;
})());
check('malformed lines are skipped', (() => {
  const built = buildCityGeometry(
    { sites: { mast: { east_m: 0, north_m: 0 } },
      features: [{ kind: 'streets', lines: [[1], 'junk', [0, 0, 10, 0]] }] },
    'mast', () => 0);
  return built.streets.length === 6; // exactly the one valid segment
})());

// --- fence mask (field round 1, 2026-08-25): the wireframe stops at the trash fence --------

{
  const doc2 = {
    sites: { mast: { east_m: 100, north_m: 0 } },
    features: [
      { kind: 'trash_fence', lines: [[-100, -100, 100, -100, 100, 100, -100, 100, -100, -100]] },
    ],
  };
  // dm ring + anchor ⇒ a ±10 m square centered on site-frame (100, 0)
  const ring = fenceRing(doc2, 'mast');
  check('fence ring converts to site frame',
    ring !== null && close(ring[0][0], 90) && close(ring[0][1], -10));
  check('inside the ring detected', pointInRing(ring, 100, 0) === true);
  check('outside the ring detected', pointInRing(ring, 130, 0) === false);
  check('no fence feature ⇒ null',
    fenceRing({ sites: { mast: { east_m: 0, north_m: 0 } }, features: [] }, 'mast') === null);
  check('no anchor ⇒ null', fenceRing(doc2, 'elsewhere') === null);

  // Two triangles: one touches the ring interior with a single vertex, one is fully outside —
  // any-vertex-inside drops the whole triangle so no wire edge pokes past the fence line.
  const verts = [[100, 0], [120, 0], [120, 20], [200, 200], [220, 200], [220, 220]];
  const masked = maskWireIndex([0, 1, 2, 3, 4, 5], (i) => verts[i], ring);
  check('touching triangle dropped, outside triangle kept',
    masked.length === 3 && masked[0] === 3 && masked[2] === 5);
}

console.log(JSON.stringify({ harness: 'brc_headless', checks, failures }));
if (failures.length) process.exit(1);
