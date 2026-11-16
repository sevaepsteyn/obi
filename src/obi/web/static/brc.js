// BRC city grid (feature 098, contracts/brc-artifacts.md C4): pure geometry prep for the
// XR viewer — no three.js, no DOM, no fetch, so the node harness runs the REAL math
// headless (the nav.js discipline). viewer.js owns the scene objects; this module only
// turns the served brc.json document into flat scene-coordinate line-segment arrays.
//
// The document carries the city ONCE in a golden-spike-centered ENU frame (integer
// decimeters) plus each in-range site's exact spike offset — the whole projection burden
// stays export-side; here it is one translation per vertex (the browser does zero geodesy).

// Draped lines sit this far above the sampled ground — enough to clear the shaded terrain
// mesh between grid posts on the flat playa, small enough to still read as "on the ground".
// A field-pass tuning knob (research D7).
export const LIFT_M = 0.6;

// Bilinear ground sampler over a SiteTerrain grid (terrain.json §sites): heights are
// DECIMETERS relative to the site origin, row-major west→east within a row, rows
// south→north; post (i, j) sits at ENU (−half + j·step, −half + i·step). Coordinates
// outside the grid clamp to the edge sample (continuous — no cliff at the boundary);
// missing/malformed terrain yields the flat sampler (always 0), because the city must
// never be hostage to terrain availability (C5).
export function makeHeightSampler(siteTerrain) {
  const flat = () => 0;
  if (!siteTerrain) return flat;
  const half = siteTerrain.half_extent_m;
  const posts = siteTerrain.posts;
  const heights = siteTerrain.rel_heights_dm;
  if (!Number.isFinite(half) || half <= 0 || !Number.isInteger(posts) || posts < 2) return flat;
  if (!Array.isArray(heights) || heights.length !== posts * posts) return flat;
  const step = (2 * half) / (posts - 1);
  return (east, north) => {
    const clamp = (v) => Math.min(posts - 1, Math.max(0, v));
    const col = clamp((east + half) / step);   // j: west → east
    const row = clamp((north + half) / step);  // i: south → north
    const j0 = Math.floor(col);
    const i0 = Math.floor(row);
    const j1 = Math.min(posts - 1, j0 + 1);
    const i1 = Math.min(posts - 1, i0 + 1);
    const fj = col - j0;
    const fi = row - i0;
    const h00 = heights[i0 * posts + j0];
    const h01 = heights[i0 * posts + j1];
    const h10 = heights[i1 * posts + j0];
    const h11 = heights[i1 * posts + j1];
    const south = h00 + (h01 - h00) * fj;
    const north_ = h10 + (h11 - h10) * fj;
    return (south + (north_ - south) * fi) / 10; // dm → m
  };
}

// The whole city as per-kind flat [x, y, z, …] scene-coordinate LINE-SEGMENT arrays
// (consecutive vertex pairs, ready for LineSegments): spike-frame dm → site-frame meters
// via the site's anchor, ENU → scene axes (x = east, y = up, z = −north — the enuToScene
// convention), y = ground sample + LIFT_M. Returns null when the document has no anchor
// for this site (out of range / malformed) — the caller renders nothing and offers no
// toggle; a valid anchor with no usable features returns {} (also nothing to render).
export function buildCityGeometry(doc, siteName, sampler) {
  const anchor = doc && doc.sites ? doc.sites[siteName] : undefined;
  if (!anchor || !Number.isFinite(anchor.east_m) || !Number.isFinite(anchor.north_m)) return null;
  const sample = typeof sampler === 'function' ? sampler : () => 0;
  const built = {};
  const features = Array.isArray(doc.features) ? doc.features : [];
  for (const feature of features) {
    if (!feature || typeof feature.kind !== 'string' || !Array.isArray(feature.lines)) continue;
    const segments = [];
    for (const line of feature.lines) {
      if (!Array.isArray(line) || line.length < 4) continue;
      let px = null, py = null, pz = null;
      for (let k = 0; k + 1 < line.length; k += 2) {
        const east = line[k] / 10 + anchor.east_m;
        const north = line[k + 1] / 10 + anchor.north_m;
        if (!Number.isFinite(east) || !Number.isFinite(north)) { px = null; continue; }
        const x = east;
        const y = sample(east, north) + LIFT_M;
        const z = -north;
        if (px !== null) segments.push(px, py, pz, x, y, z);
        px = x; py = y; pz = z;
      }
    }
    if (segments.length) built[feature.kind] = segments;
  }
  return built;
}

// --- the fence mask (field round 1, 2026-08-25) ---------------------------------------------
// Inside the trash fence the playa is the city floor — terrain wireframe grid lines under
// the streets read as noise (operator field pass). The viewer punches a fence-shaped hole
// in the terrain WIRE mesh only (the shaded ground stays whole); the math lives here so the
// node harness exercises it for real.

// The trash-fence ring in SITE-frame meters ([e, n] vertex pairs) from the served doc.
// Null when the doc has no anchor for this site or no usable fence feature.
export function fenceRing(doc, siteName) {
  const anchor = doc && doc.sites ? doc.sites[siteName] : undefined;
  if (!anchor || !Number.isFinite(anchor.east_m) || !Number.isFinite(anchor.north_m)) return null;
  const features = Array.isArray(doc.features) ? doc.features : [];
  const fence = features.find((f) => f && f.kind === 'trash_fence' && Array.isArray(f.lines));
  const line = fence && Array.isArray(fence.lines[0]) ? fence.lines[0] : null;
  if (!line || line.length < 6) return null; // a ring needs at least three vertices
  const ring = [];
  for (let k = 0; k + 1 < line.length; k += 2) {
    ring.push([line[k] / 10 + anchor.east_m, line[k + 1] / 10 + anchor.north_m]);
  }
  return ring;
}

// Even-odd ray cast. The ring may or may not repeat its first vertex — a zero-length
// closing edge contributes nothing.
export function pointInRing(ring, e, n) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const [ei, ni] = ring[i];
    const [ej, nj] = ring[j];
    if ((ni > n) !== (nj > n) && e < ((ej - ei) * (n - ni)) / (nj - ni) + ei) inside = !inside;
  }
  return inside;
}

// A triangle index with every fence-touching triangle dropped: a triangle goes when ANY
// vertex lies inside the ring, so no wire edge pokes past the fence line (the outside
// undercut is at most one grid cell). getEN maps a vertex index to site-frame [e, n].
export function maskWireIndex(index, getEN, ring) {
  const kept = [];
  for (let t = 0; t + 2 < index.length; t += 3) {
    let touches = false;
    for (let v = 0; v < 3; v += 1) {
      const [e, n] = getEN(index[t + v]);
      if (pointInRing(ring, e, n)) { touches = true; break; }
    }
    if (!touches) kept.push(index[t], index[t + 1], index[t + 2]);
  }
  return kept;
}
