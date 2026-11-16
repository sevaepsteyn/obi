# Test fixtures — recorded RouterOS responses

These JSON files are **read-only** recordings of RouterOS API responses, used to drive the
Mikrotik adapter and integration tests offline (no real device required at test time).

Each file holds the records returned by three **read-only** RouterOS commands:

- `/system/identity/print`  → `identity`
- `/system/resource/print`  → `resource`
- `/system/routerboard/print` → `routerboard`

## How these were captured (read-only)

Capture issues only `print` (read) commands — never a `set`, `add`, `remove`, or any
state-changing command (Constitution Principle I). See `tests/record_fixtures.py` for the
documented, read-only capture helper. Sensitive values (serial numbers) are anonymized.

| File | RouterOS | Notes |
|------|----------|-------|
| `border-gw-01.json` | v7.11.2 | Full metric set. |
| `ap-shade-07.json`  | v6.48.6 | Omits `total-memory` and routerboard `model` — exercises the "missing field → unavailable" path (FR-011). |

## UISP fixtures (`uisp/`) — recorded `GET /nms/api/v2.1/devices` (feature 003)

Read-only recordings of a UISP controller's device list, used to drive the UISP adapter and
integration tests offline. **The API token is never recorded** (it travels only in the request
header; scrub it from any capture). Capture issues only `GET` requests (no POST/PUT/DELETE/PATCH —
Constitution Principle I); the operator's own `github.com/<operator>/uisp_api_mcp` is the
authoritative reference for shapes.

| File | Notes |
|------|-------|
| `uisp/devices.json` | Three devices: a reachable PtP station (uptime/cpu, RAM as a %), an offline PtP AP (no IP → keyed by MAC), and a reachable switch (RAM as free/total bytes). Exercises reachable/offline, name-filter (`ptp`), and the "memory %/bytes/unavailable" mapping paths. |

## NetBox fixtures (`netbox/`) — recorded `GET /api/dcim/devices/` (feature 007)

Read-only recordings of a NetBox instance's documented device list, used to drive the NetBox
adapter and integration tests offline. NetBox is a **source of truth** (documentation, not live
health). **The API token is never recorded** (it travels only in the `Authorization: Token`
header; scrub it from any capture). Capture issues only `GET` requests (no POST/PUT/PATCH/DELETE —
Constitution Principle I), following the `next` link across pages.

The fixture is a **list of page envelopes** (`{count, next, previous, results}`) so it exercises
pagination as well as field-mapping. `tests.fakes.load_netbox_pages()` returns the pages;
`load_netbox_devices()` flattens their `results`.

| File | Notes |
|------|-------|
| `netbox/devices.json` | Six devices across **two pages** (page 1 `next` non-null): documented gateways/switch/PtP/AP with varied `status` (active/planned/inventory/offline), an **unnamed** device with no site/role/primary_ip (exercises the "missing → None + note" path), an empty serial (`""` → `None`), and doubly-nested `device_type.manufacturer.name`. Hand-crafted from the documented NetBox 4.x schema; replace with a real read-only recording (token scrubbed) once valid creds exist. |

## Netwatch fixture (`netwatch-tamarack-internet-gw.json`) — recorded `/tool/netwatch` print (feature 095)

A read-only table print recorded live from tamarack-internet-gw (RouterOS 7.x, 2026-08-24) via
`tests/record_fixtures.py` (its `LIST_PATHS` gained the `netwatch` entry for exactly this). Pins
the **`type=simple` wire shape** the dashboard's mapper consumes: `status`/`since`/`done-tests`/
`failed-tests` only — **no rtt/loss fields** (those exist only on `type=icmp` probes, none
deployed) — and the row-to-row key inconsistency observed live (one row has its `disabled`/script
keys stripped, as tamarack-gw emits). Counters are cumulative and reset when netwatch restarts;
`since` is the router-local clock. No secrets on this menu; hosts are internal addresses like
every RouterOS fixture here.

| File | Notes |
|------|-------|
| `netwatch-tamarack-internet-gw.json` | Five watched targets: the two internet references (8.8.8.8 / 1.1.1.1), a link-local peer, and site targets — all `type=simple`, all `up`, counters at 323/0. Row 1 deliberately lacks the optional keys (mapper totality). |

## Weather fixtures (`weather/`) — recorded public keyless GETs (feature 110)

Read-only recordings of the two public weather endpoints the hourly collector fetches —
Open-Meteo (BRC current + 24-hour hourly forecast, imperial units, playa-local times) and the
NWS active-alerts feed. No credentials exist on either API (keyless by design), so there is
nothing to scrub. Captured 2026-08-28 with plain GETs of the exact request shapes
`core/weather.py` issues (the NWS capture carries the required `User-Agent`).

| File | Notes |
|------|-------|
| `weather/openmeteo-forecast.json` | The real BRC forecast response (golden-spike coordinates; elevation 1189 m confirms the playa cell): a `current` block plus exactly 24 hourly rows carrying all eight requested variables. Pins the naive-local time strings + `utc_offset_seconds` anchoring the parser converts to epoch µs UTC. |
| `weather/nws-alerts-empty.json` | The real `zone=NVZ004` response with zero active alerts — the VERIFIED "none active" shape (`features: []`), distinct from a failed fetch. |
| `weather/nws-alerts-active.json` | A real active alert (CA Extreme Heat Warning, Severe — NVZ004 had none at capture time), reduced to its first feature for size; structure untouched. Pins `event`/`severity`/`onset`/`ends`/`headline` parsing with real offset-bearing timestamps. |
## VictoriaMetrics fixtures (`victoria/`) — recorded Prometheus-API `GET`s (feature 108)

Read-only recordings from ADMIN's VictoriaMetrics store
(`core-vms-1.net.example.net:8428`, captured 2026-08-28), used to drive the `victoria` adapter
tests offline. The store is **credential-free**, so there is nothing to scrub. Capture issues only
`GET` requests against the driver's five-endpoint read allowlist (`/api/v1/query`, `/query_range`,
`/labels`, `/label/<name>/values`, `/series` — Constitution Principle I); the error fixture is the
store's own 422 body for a malformed matcher, kept verbatim because the tool surfaces that text to
the model.

| File | Notes |
|------|-------|
| `victoria/instant_rf_vector.json` | Instant vector: the RF metric superset for `magnolia-ptp-manzanita-60` (AF60-LR) — real labels incl. `site`/`role`/`board`/`sta`. Exercises vector parsing + render-what-exists. |
| `victoria/range_cinr_matrix.json` | Range matrix: `cinr_Dbm` for the same radio, 1 h @ 300 s step. Exercises matrix parsing + summaries. |
| `victoria/instant_weather_vector.json` | Instant vector: `{__name__=~"weather_.*"}` — both stations (`gate_actual` with wind, `black_hole` without). Exercises the two-station merge. |
| `victoria/instant_scalar.json` | Instant `scalar(42)` — VM normalizes to a vector with empty labels (verified live); pins that shape. |
| `victoria/label_values_hostnames.json` | `label/hostname/values` with `match[]={role="ptp-rf"}` — the 24 backbone radios (resolver roster). |
| `victoria/label_values_weather_names.json` | `label/__name__/values` scoped to `station="gate_actual"` — discovery-shape fixture. |
| `victoria/series_weather.json` | `series` for `weather_temp_f` — labelset discovery shape. |
| `victoria/error_envelope.json` | The real 422 `{"status":"error",…}` body for `match[]=role="ptp-rf"` (unbraced matcher). Exercises REACHABLE-with-detail. |

## Traffic fixtures (`traffic/`) — BRC dashboard gate endpoints (feature 112)

Three REAL recordings (plain GETs of the exact request shapes `core/traffic.py` issues —
all endpoints are public and credential-free, nothing to scrub) plus three CLEARLY
SYNTHETIC feed bodies. The synthetics were written pre-event, when the feed's schema was
unverified and `/api/feed/toc` 403'd to everyone; the live schema was verified 2026-08-29
when the event went public — the real channel is `/api/feed/public` (the `toc` channel
never opened) — and `public-feed-live.json` is its capture. The synthetics stay: they pin
the defensive-parse fallback chains (research.md D4) against future drift.

| File | Notes |
|------|-------|
| `traffic/gate-status-null.json` | The real `/api/gate-status` response: all-null inbound/outbound/status — the published "not reported" state (FR-004), served with no ETag/validator headers. |
| `traffic/toc-feed-403.json` | The real `/api/feed/toc` 403 body (`{"message":"Forbidden"}`) — a refusal the collector records as a `refused` outcome, never an error (that channel stayed staff-only; the collector now polls `/api/feed/public`). |
| `traffic/public-feed-live.json` | The REAL `/api/feed/public` envelope (captured live 2026-08-29, trimmed to 3 `synced.traffic` entries / 3 `toc` rows): @bmantraffic posts of `id`/`text`/`note_tweet`/`created_at`(ISO Z)/`author{name,username}` nested at `synced.traffic`; the endpoint serves a stable weak ETag and honors If-None-Match. |
| `traffic/synthetic-feed-list.json` | SYNTHETIC: a top-level entry list with `id`/`timestamp`(ISO+offset)/`title`/`author`/`pinned`/`text` — the shape the spec's dashboard-client hints suggest. |
| `traffic/synthetic-feed-wrapped.json` | SYNTHETIC: entries under an `entries` key with alternate field spellings (`uuid`/`published`/`body`), one epoch-seconds stamp, one entry with NO stamp (first-seen fallback). |
| `traffic/synthetic-feed-drift.json` | SYNTHETIC drift case: valid JSON with a list under `data` whose rows carry NO recognizable text/id/stamp fields (one dict, one bare string) — must still store+serve as raw text (FR-006/SC-005). |

## maintenance/ (feature 135)

`maintenance/live_2026-10-14.json` — NAMES ONLY, captured read-only from the live dash board
(`board.json`) and UISP snapshot (`uisp.json`) on 2026-10-14, post-burn, with wingnut/manzanita/
noc/magnolia torn down: fleet identities, netwatch routers and `[router, target]` pairs, UISP
device identities with their `meta.maintenance` flag (21/38 flagged), recent UISP log hosts,
and the UISP site names from `[web.site_towers]`. Drives the matcher table tests
(`tests/unit/test_maintenance.py`) — e.g. `rowan-ptp-joshua-st-05` (a genuine outage) must
match nothing under the live declaration.
