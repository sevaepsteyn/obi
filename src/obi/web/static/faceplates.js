// obi faceplate layouts (feature 075, contracts/faceplate-layout.md) — the curated physical port
// arrangement for the fleet's five RouterOS board models, plus a generic fallback for anything else.
//
// Pure DATA + two pure functions (the bands.js static-vocabulary precedent): no fetches, no DOM, no
// state, so the node DOM-stub harness can exercise it directly. Imported ONLY by widgets/phy.js.
//
// Port ids are RouterOS DEFAULT names (ether7 / sfp-sfpplus2 / sfp28-1 / qsfpplus1-1) — the join key
// to the live payload's `default_name` (FR-005). `col`/`row` are the physical grid position; on the
// PoE switches the copper block staggers odd-top / even-bottom, encoded explicitly by the builders.
// Port counts + the SFP-DDM / PoE shapes were confirmed by a read-only live capture (research D9):
// CCR2004-1G-12S+2XS and CRS318/CRS328 seen directly; RB5009 / CRS326 arranged from the
// devicetype-library inventories + product photos (SC-002 field verification is T036).

const KIND_LABEL = { rj45: 'ether', sfp: 'sfp', sfpp: 'sfp+', sfp28: 'sfp28', qsfpp: 'qsfp+' };

// A staggered RJ45 copper block: rows:2 fills odd-top/even-bottom (ether1 top, ether2 bottom, …).
function copperBlock(count, { poe = false, rows = 2, col0 = 0, maxFor = () => 1000 } = {}) {
  const ports = [];
  for (let i = 0; i < count; i++) {
    const def = {
      id: `ether${i + 1}`,
      kind: 'rj45',
      row: rows === 2 ? i % 2 : 0,
      col: rows === 2 ? col0 + Math.floor(i / 2) : col0 + i,
      maxMbps: maxFor(i),
    };
    if (poe) def.poe = true;
    ports.push(def);
  }
  return ports;
}

// A block of cages (sfp / sfp+ / sfp28 / qsfp+). `idFor` overrides the default `${prefix}${n}` naming.
function cageBlock(prefix, count, { kind, maxMbps, col0, rows = 1, start = 1, idFor = null }) {
  const ports = [];
  for (let i = 0; i < count; i++) {
    ports.push({
      id: idFor ? idFor(i) : `${prefix}${start + i}`,
      kind,
      row: rows === 2 ? i % 2 : 0,
      col: rows === 2 ? col0 + Math.floor(i / 2) : col0 + i,
      maxMbps,
    });
  }
  return ports;
}

// A QSFP+ cage plus its breakout channels, all in ONE row: the primary channel (qsfpplusN-1) at
// col0, then -2..-4 stacked to its RIGHT (T036 field pass: channels read as part of their cage,
// not as a separate overflow row). Channels are ordinary ports — simply down unless a breakout
// cable is in use; a 40G cage breaks out into 4×10G, so they carry maxMbps 10000.
function qsfpBlock(n, { row, col0, maxMbps = 40000 }) {
  const ports = [{ id: `qsfpplus${n}-1`, kind: 'qsfpp', row, col: col0, maxMbps }];
  for (let c = 2; c <= 4; c++) {
    ports.push({ id: `qsfpplus${n}-${c}`, kind: 'qsfpp', row, col: col0 + c - 1, maxMbps: 10000 });
  }
  return ports;
}

function groupsFor(ports) {
  const seen = [];
  for (const port of ports) if (!seen.includes(port.kind)) seen.push(port.kind);
  return seen.map((kind) => ({ label: KIND_LABEL[kind] ?? kind, kind }));
}

function layout(rows, ports) {
  return { rows, groups: groupsFor(ports), ports };
}

// --- the five curated fleet models ------------------------------------------------------------

const CRS318 = layout(2, [
  ...copperBlock(16, { poe: true, rows: 2, col0: 0 }),
  ...cageBlock('sfp-sfpplus', 2, { kind: 'sfpp', maxMbps: 10000, col0: 8, rows: 2 }),
]);

const CRS328 = layout(2, [
  ...copperBlock(24, { poe: true, rows: 2, col0: 0 }),
  ...cageBlock('sfp-sfpplus', 4, { kind: 'sfpp', maxMbps: 10000, col0: 12, rows: 2 }),
]);

const CCR2004 = layout(1, [
  ...copperBlock(1, { rows: 1, col0: 0 }),
  ...cageBlock('sfp-sfpplus', 12, { kind: 'sfpp', maxMbps: 10000, col0: 1, rows: 1 }),
  ...cageBlock('sfp28-', 2, { kind: 'sfp28', maxMbps: 25000, col0: 13, rows: 1 }),
]);

const RB5009 = layout(1, [
  // ether1 is the 2.5G PoE-in port (2500); ether2–8 are 1G. Research D9: PoE-out across ether1–8.
  ...copperBlock(8, { poe: true, rows: 1, col0: 0, maxFor: (i) => (i === 0 ? 2500 : 1000) }),
  ...cageBlock('sfp-sfpplus', 1, { kind: 'sfpp', maxMbps: 10000, col0: 8, rows: 1 }),
]);

const CRS326 = layout(2, [
  // T036 field pass (core-sw-1): ether1 is the 1G management port, inline at the LEFT of the
  // faceplate on the real unit — it was missing here and fell into the generic overflow row.
  ...copperBlock(1, { rows: 1, col0: 0 }),
  ...cageBlock('sfp-sfpplus', 24, { kind: 'sfpp', maxMbps: 10000, col0: 1, rows: 2 }),
  // The two QSFP+ cages, each with its breakout channels stacked to the right in its own row.
  ...qsfpBlock(1, { row: 0, col0: 13 }),
  ...qsfpBlock(2, { row: 1, col0: 13 }),
]);

export const FACEPLATES = {
  'CRS318-16P-2S+': CRS318,
  'CRS328-24P-4S+': CRS328,
  'CCR2004-1G-12S+2XS': CCR2004,
  'RB5009UPr+S+': RB5009,
  'CRS326-24S+2Q+': CRS326,
};

// --- generic fallback + lookup ----------------------------------------------------------------

export function kindOf(id) {
  const s = String(id ?? '').toLowerCase();
  if (s.startsWith('qsfp')) return 'qsfpp';
  if (s.startsWith('sfp28')) return 'sfp28';
  if (s.startsWith('sfp-sfpplus') || s.startsWith('sfpplus')) return 'sfpp';
  if (s.startsWith('sfp')) return 'sfp';
  return 'rj45';
}

// maxMbps inferred from the default name (FR-004). Curated layouts override where the inference is
// wrong (e.g. RB5009 ether1 is 2.5G) — this is only the fallback for an unknown model.
export function inferMaxMbps(id) {
  switch (kindOf(id)) {
    case 'qsfpp': return 40000;
    case 'sfp28': return 25000;
    case 'sfpp': return 10000;
    default: return 1000; // sfp (1G), ether (1G), unknown
  }
}

const naturalSort = (a, b) => String(a).localeCompare(String(b), undefined, { numeric: true });

// Fallback for unknown models (FR-004): copper first (2 rows odd-top/even-bottom when > 8, else 1),
// then cages grouped by kind. `ports` is the live payload's ports[] (uses default_name → name).
export function genericLayout(ports) {
  const ids = (ports ?? [])
    .map((port) => port.default_name || port.name)
    .filter((id) => typeof id === 'string' && id !== '');
  const copper = ids.filter((id) => kindOf(id) === 'rj45').sort(naturalSort);
  const cages = ids.filter((id) => kindOf(id) !== 'rj45').sort(naturalSort);
  const rows = copper.length > 8 ? 2 : 1;
  const defs = [];
  copper.forEach((id, i) => {
    defs.push({
      id,
      kind: 'rj45',
      row: rows === 2 ? i % 2 : 0,
      col: rows === 2 ? Math.floor(i / 2) : i,
      maxMbps: inferMaxMbps(id),
    });
  });
  const copperCols = rows === 2 ? Math.ceil(copper.length / 2) : copper.length;
  cages.forEach((id, i) => {
    defs.push({ id, kind: kindOf(id), row: 0, col: copperCols + i, maxMbps: inferMaxMbps(id) });
  });
  return { rows, groups: groupsFor(defs), ports: defs };
}

// Normalize a live/roster model to a FACEPLATES key: trim, then strip a trailing retail suffix that
// follows the last '+' (routerboard reports `RB5009UPr+S+IN` while the key is `RB5009UPr+S+`).
export function normalizeModel(model) {
  return String(model ?? '').trim().replace(/\+(IN|RM|OUT)\+?$/i, '+');
}

// Lookup with normalization, else generic. Never throws, never returns null — the tab always renders.
export function layoutFor(model, ports) {
  const key = normalizeModel(model);
  if (FACEPLATES[key]) return { layout: FACEPLATES[key], curated: true };
  return { layout: genericLayout(ports ?? []), curated: false };
}
