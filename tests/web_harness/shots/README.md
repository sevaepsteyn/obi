# Screenshot harness (`tests/web_harness/shots/`)

Real-browser, one-PNG-per-widget capture of every registered obi web widget — the 075
invisible-faceplate lesson, mechanized (feature 076). The node DOM-stub harnesses
(`../phy_headless.mjs`, `../ospf_headless.mjs`, …) execute widget logic but cannot see a browser
**layout** failure: a canvas that draws into a zero-height host, a panel positioned off-screen, a
faceplate with no intrinsic size. This harness renders the REAL widget modules in headless Chrome
against synthetic fixtures and writes a PNG you review by eye.

Dev tooling only: never imported by, served from, or required by the shipped app. Zero new
dependencies — Python stdlib (`capture.py`) plus a locally installed Chrome.

## Run

```sh
# every widget → out/before/ (run this on the PRE-retrofit tree)
python tests/web_harness/shots/capture.py --label before

# after a change → out/after076/, then eyeball the pairs side by side
python tests/web_harness/shots/capture.py --label after076
open tests/web_harness/shots/out/before tests/web_harness/shots/out/after076

# iterate on one widget (or a family) while developing:
python tests/web_harness/shots/capture.py --label wip --only ospf,ros

# leave the fixture server up to click around a widget by hand:
python tests/web_harness/shots/capture.py --label wip --only met --keep-server
#   then browse http://127.0.0.1:<port>/shot.html?widget=met
```

`out/<label>/<id>.png` (`:` in a minted id → `_`) is git-ignored. Exit is non-zero if any capture
is missing or zero-byte. Chrome path override: `OBI_CHROME=/path/to/chrome`.

**No pixel-diff gate** (SC-004): clock/age text makes naive diffs noisy. The deliverable is
reviewable before/after pairs, signed off in `specs/076-widget-language/tasks.md`.

## What gets captured

The id list is DERIVED at run time from `registry.js` `WIDGETS` (so a future catalog widget is
covered with no change here) plus a fixed set of representative minted-window ids that reference
committed fixtures: `ros:10.20.0.1`, `conf:core-gw-1`, `ospf:core-gw-1`, `link:ranch3~relay3`,
`ospf-lint`.

## Fixtures (`fixtures/`)

`routes.json` maps every `/api/*`, `/snapshots/*` and export URL a widget fetches on mount to a
synthetic JSON payload. Everything is fully synthetic/redacted (10.20.x.x, invented names), following
the `tests/fixtures/` conventions. `capture.py` matches on the URL PATH only (query strings ignored),
so a route whose widget fetches several shapes on one path is served ONE **union** fixture that
satisfies every consumer's mount read:

- `/api/configs.json` → `configs.json` serves both the CFG coverage board (`rows`/`totals`) and the
  per-device CONF window (`entries` history + `diff` + `content`).
- `/api/metrics.json` → `metrics.json` serves both the MET coverage probe (`devices`) and the series
  read (`series`).
- `siklu.json` serves both the SIK radios board (`data.radios`) and the LINKS board's Siklu half
  (`data.links`).

Each fixture carries a top-level **`_fresh`** list naming its timestamp fields (`generated_at`,
`created_at`, `timestamp`, `read_at`, bucket `start`, …). `capture.py` re-anchors those relative to
run time — the NEWEST value becomes ~now and the rest keep their relative spacing — so ages render
plausibly ("<1m", not "3 weeks") and time charts stay time charts. The `_fresh` manifest is stripped
before the payload is served. `ospf.json` is engine-generated once (`build_from_fleet()` +
`ospf_topology_to_dict`, the same snapshot `test_web_ospf_smoke.py` builds).

## Adding a widget

1. Add the catalog entry to `registry.js` as usual — `capture.py` picks up the new id automatically.
2. If it fetches a new route, add a synthetic fixture to `fixtures/` and a `routes.json` entry
   (with a `_fresh` manifest if it renders ages). Verify the exact path against the widget's fetch
   call.
3. `python tests/web_harness/shots/capture.py --only <id> --keep-server` and confirm it renders
   non-blank and honest (a real empty state is fine; an error at mount is not).

## The mount page (`shot.html` / `shot.js`)

`shot.html` loads `theme.css` and a full-viewport `#shot-host`; `shot.js` resolves `?widget=<id>`
via `registry.defForLayoutId` (catalog or minted), imports the real module, and mounts it with a
stub ctx (`mode:'window'`, params, `setTitle/setName/setBadge` → the corner strip, inert `open*`,
**no `openSnapshot`** — panels branch on `ctx.mode`, the D6 lesson). The host sizes via in-flow flex,
NOT `position:absolute`+`inset`, so it keeps its box even when a widget sets `host.style.position`
(the OSPF map does — an absolute+inset host collapsed to zero height and blanked its canvas).

## XR media (`xr/`, with `readme_media.mjs`)

The immersive viewer only renders inside a WebXR session, so its README media is captured
through an emulated headset: Meta's [IWER](https://github.com/meta-quest/immersive-web-emulation-runtime)
(MIT) injected before the page's own scripts. IWER is fetched at capture time, never vendored.
Like all published media it is taken from the sanitized demo bundle, and only on the opaque
backdrop — an emulator has no passthrough to show.

```sh
uv run obi web serve --demo                     # http://127.0.0.1:8765/obi/
curl -sL -o /tmp/iwer.min.js https://cdn.jsdelivr.net/npm/iwer@2.8.0/build/iwer.min.js
cat /tmp/iwer.min.js tests/web_harness/shots/xr/iwer-setup.js > /tmp/xr-init.js
export OBI_INIT=/tmp/xr-init.js OBI_CLICK='.xr .enter' OBI_ANGLE=metal

# a still (2x pixels; scale to 1400 px and palettize like the other images)
node tests/web_harness/shots/readme_media.mjs shot \
  '?w=xr#qx=-5000&qy=460&qz=-1850&tz=-3&ty=-1.2&pitch=-24&fov=75&hands=0&terrain=2&s=0.0009&yaw=-75' \
  /tmp/xr-links.png 2500 1400x875 tests/web_harness/shots/xr/view.js

# the fly-around: frames + frames.txt, assembled as animated WebP
node tests/web_harness/shots/readme_media.mjs video '?w=xr' /tmp/xr-tour 16.5 1200x750 \
  tests/web_harness/shots/xr/tour.js
```

- `xr-links.png` is the command above; `xr-fresnel.png` is `s=0.0018&yaw=150&mode=3`;
  `xr-signal.png` is `s=0.0009&yaw=20&mode=1`. All media uses the terrain wireframe
  (`terrain=2` in the stills; `tour.js` presses B twice before it starts moving).
- `OBI_ANGLE=metal` matters for `video`: the software renderer manages about 3 frames a second on
  this scene, the GPU about 60.
- The animation ships as animated WebP, not GIF. Every pixel moves in a fly-around, and a GIF of
  it is several times the size at a lower resolution (the shaded-terrain take was 1.2 MB at 1000 px against 3.7 MB at 640 px).
  Build an ffconcat list from `frames.txt` (real per-frame durations, skipping the first 1.5
  seconds while the script frames the scene and switches the terrain), then
  `ffmpeg -f concat -i list.txt -vf fps=10,scale=800:-1 -loop 0 -c:v libwebp_anim -q:v 55 -compression_level 6 xr-tour.webp`.
  The wireframe's thin moving lines compress poorly: 1.9 MB at these settings.
- `view.js` places the world with the same scale/yaw/position transform navigation uses; `tour.js`
  moves it with the emulated thumbstick and A button, through the viewer's own navigation code.
