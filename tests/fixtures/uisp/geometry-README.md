# UISP geometry fixtures (feature 022)

Captured **read-only** from the live production controller on **2026-07-01** via the three
allowlisted GETs (`/nms/api/v2.1/devices`, `/data-links`, `/sites`), then slimmed to exactly
the fields the geometry ingest reads (see `specs/022-ar-links/contracts/uisp-geometry.md`).
No credentials or tokens appear in any fixture.

- `geometry-devices.json` — all 77 fleet devices (identification, location, overview slim).
  Deliberately includes every observed altitude shape: plausible-AMSL values (~1190–1370 m),
  small AGL-like values (10, 1), junk (0), and radios with `status: "disconnected"`
  (off-season — 15 of 37 PtP radios were down at capture time; real `down`-health cases).
- `geometry-data-links.json` — all 42 controller data-links. Link wiredness is the `type`
  field (`"wireless"` / `"ethernet"`); there is **no** `wired` key. Includes PtMP station
  links that the both-ends-`-ptp-` filter must exclude. `sitesDistance` is the controller's own
  distance figure, used as an independent cross-check for obi's ENU math (`devicesDistance`
  exists but is null across the live fleet).
- `geometry-sites.json` — all 38 sites (identification + description.location). Sites carry
  lat/lon but no altitude (why the EPQS ground-elevation bake exists).

`../ar/epqs-response.json` — one real USGS EPQS response for a playa point. Note the
elevation `value` is a JSON **string** (`"1190.661987305"`), not a number.

## 044 augmentation (2026-07-10)

`geometry-devices.json` was augmented in place — same read-only `/devices` GET, joined to
the 2026-07-01 capture by `identification.id` — with exactly the fields the 044 ingest
newly reads: `meta.maintenance` + `meta.maintenanceEnabledAt` on all 77 elements (32
currently `true`: the off-season fleet), and the `overview` capacity/utilization sextet
(`downlinkCapacity`/`uplinkCapacity`/`theoretical*Capacity`/`downlinkUtilization`/
`uplinkUtilization`) merged only where the device's live status matched the recorded
status (55 elements; keeps each element internally coherent). No pre-044 key was modified,
so 022/025 assertions hold. Some elements deliberately lack the new keys (pre-044 shape)
— the mapper must treat absence as unknown.
