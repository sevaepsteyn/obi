// MAP widget (feature 037) — the 2D link map as a workspace citizen.
//
// The thinnest possible wrapper: viewer2d.js already IS the widget (canvas render, host-
// driven sizing, its own poll lifecycle); this module adapts its age readout onto the
// window's title-bar badge and turns its disposer into the contract's unmount().

import { start2D } from '../viewer2d.js';

export async function mount(host, ctx) {
  // start2D writes plain text into `ageTarget.textContent` — feed that to the badge.
  const ageTarget = {
    set textContent(value) {
      ctx.setBadge(value || null);
    },
  };
  // 058/076 D6: hand the viewer the workspace's link-window opener so a link tap opens a real LINK
  // window — but ONLY in a real workspace window. Gate on ctx.mode, NOT truthiness: the full-screen
  // `?w=map` ctx carries a truthy no-op openLink stub, which viewer2d's `if (openLink)` gate used to
  // take, opening NOTHING (neither window nor panel). Passing null in fullscreen lets it fall back
  // to its in-canvas kit panel again.
  const viewer = await start2D(host, {
    ageTarget,
    openLink: ctx.mode === 'window' ? ctx.openLink : null,
  });
  return {
    unmount() {
      viewer.dispose();
    },
    // No onResize needed: start2D observes the host element directly (research R5).
  };
}
