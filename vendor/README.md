# Vendored viewer assets

- `three.module.min.js` — [three.js](https://threejs.org/) **r160.1** (`three@0.160.1`,
  `build/three.module.min.js`, 670,681 bytes), MIT license (Copyright 2010-2023 Three.js
  Authors; SPDX header retained at the top of the file). Fetched 2026-07-01 from
  `https://unpkg.com/three@0.160.1/build/three.module.min.js`.

Vendored (not a CDN reference) because the viewer MUST load with zero internet access
(FR-011 — field/playa operation). To upgrade: replace the file with a newer pinned release,
update this README, and re-run the field verification in `specs/022-ar-links/quickstart.md`.
