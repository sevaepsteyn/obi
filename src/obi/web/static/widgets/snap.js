// SNAP widget (feature 076) — an ephemeral snapshot window.
//
// A snapshot is a static, INERT clone of a panel body, captured at a moment in time and popped out
// into its own window (kit.createPanel's pop-out fallback when the panel has no live window kind).
// It re-presents already-fetched, on-screen content — nothing here fetches, polls, or listens. The
// clone's controls are disabled and its window is excluded from layout persistence
// (registry.snapshotWidgetDef `ephemeral: true` → wm.serialize skips it), so a snapshot can never
// masquerade as live data and it vanishes on reload. Contract: contracts/snapshot-window.md.

export function mount(host, ctx) {
  host.classList.add('snap'); // body scrolls (theme.css) — a tall report/detail stays readable
  const node = ctx.params?.node ?? null;
  if (node) host.appendChild(node);

  // Title suffix stamps the capture time in LOCAL wall-clock: the window reads
  // `<source mnemonic> <subject> — snapshot · HH:MM` (e.g. `OSPF core-gw-1 — snapshot · 14:32`;
  // the mnemonic is inherited from the source window, 'SNAP' only as the no-source fallback).
  const takenAt = ctx.params?.takenAt ?? Date.now();
  const when = new Date(takenAt);
  const pad = (n) => String(n).padStart(2, '0');
  ctx.setTitle?.(`snapshot · ${pad(when.getHours())}:${pad(when.getMinutes())}`);

  return { unmount() {} }; // nothing to clean — no pollers, no listeners, no fetches
}
