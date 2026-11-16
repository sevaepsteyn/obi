# UISP link-health fixtures (feature 034)

Captured **read-only** from the live production controller on **2026-07-08** via the two
allowlisted GETs (`/nms/api/v2.1/data-links`, `/nms/api/v2.1/devices`), issued through the
adapter's own geo transport (`UispController._default_geo_transport` — the sanctioned tooling,
same method as the 022 geometry captures).

Unlike the geometry fixtures (which were slimmed to exactly the geometry-relevant fields), these
are **untrimmed**: every field the controller returned is retained, so the RF link-health mapping
(`linkScore` sub-object, per-radio signal, capacities, channel width, …) is pinned against real
shapes. Verified secrets-free at capture time: the API token does not appear, and no
token/password/secret/credential-shaped key exists in either file.

- `links-data-links.json` — all 42 controller data-links (40 wireless + 2 ethernet). Observed
  `state` vocabulary: `active`, `disconnected`. 32 of the 40 wireless links carry a link-level
  `signal` (disconnected links generally don't). Off-season capture: many PtP radios are down —
  real down-link cases.
- `links-devices.json` — all 77 fleet devices, untrimmed. `overview.linkScore` is the per-radio
  quality sub-object (`score`, `linkScore`, `downlinkScore`, `uplinkScore`, `airTime`,
  `theoreticalDownlinkCapacity`/`Uplink`/`Total`); present on connected radios, absent/null on
  disconnected ones. Join key for link ends: `identification.id` — 0 natural join misses in this
  capture (contract tests craft one synthetically).
