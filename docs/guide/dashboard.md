# Status dashboard (`obi dash`)

`obi dash` is one green/yellow/red board answering "is everything OK right now?". It is the fast, deterministic counterpart to the [watcher](./watcher.md)'s deeper hourly analysis: every read is read-only and no LLM is involved.

## Signals

A 1-minute cycle collects and judges five alerting signals, plus the display-only mesh and lighting tiles:

| Signal | What it shows |
| --- | --- |
| **fleet** | Reachability and RTT of every RouterOS device and backhaul radio. UISP maintenance-flagged devices show yellow without tainting the rollup. |
| **netwatch** | Per-router netwatch state from live table reads: up/down plus loss derived from the test counters. A probe upgraded to `type=icmp` contributes real latency automatically. |
| **ping** | Reference latency to 8.8.8.8 / 1.1.1.1, measured *from* the border router. |
| **internet** | Uplink utilization in both directions vs provisioned capacity, from the 5-minute metrics store (labeled as such). |
| **pipeline** | Whether obi's own collectors are writing. |

`stale` is a first-class state: it outranks green and never pages, so a dead feed can't leave a green wall.

## Setup

```toml
[dash]                          # presence + enabled opts in
enabled = true

[[dash.internet_links]]         # capacity is operator knowledge — no defaults
device = "aspen-internet-gw"
interface = "sfp-sfpplus5-uplink"
label = "uplink"
capacity_mbps = 2500

[dash.pager]
enabled = false                 # flip after a day of green board (needs the [slack] bot token)

[dash.mesh]                     # optional, display-only, never pages
# heard_yellow_after = "2h"     # tracked mesh node quiet this long -> yellow
# heard_red_after    = "12h"    # ... this long -> red
# fix_yellow_after   = "6h"     # newest recorded position older than this -> yellow
# read_interval      = "5m"     # how often the serial read happens

[dash.lighting]                 # optional, display-only, never pages
# timeout = 5.0                 # per-controller read deadline; defaults to [wled] timeout
```

```cron
* * * * * /path/to/.venv/bin/obi dash run >> ~/.local/state/obi/dash.log 2>&1
```

The dash cycle absorbs the ICMP collector cron: it runs the same sweep in-process and writes the same store. Installing its line replaces the old `1-59/2 … obi icmp run` entry; `obi icmp run` and `obi icmp status` stay for manual use.

## Reading the board

- `obi dash show` in the terminal.
- The **DASH** widget in the [web workspace](./web.md), self-refreshing every 60 s.
- `?w=dash` as a full-screen wall display.

A board older than 3 minutes renders stale everywhere.

## Pager

The opt-in pager DMs red transitions within about a minute: one line per item, a recovery line when it clears, and mass events coalesced into a single message. Paging setup is covered in [../setup/paging.md](../setup/paging.md).

While the pager is enabled, `obi watch run` automatically skips its hourly ICMP pass (same store, already paged an hour earlier). That pass's baseline stays fresh, so turning the pager off resumes it cleanly.

## Display-only tiles: mesh and lighting

- **mesh**: whether the mesh radio is attached, how long since each tracked node was heard, and how old the newest recorded fix is. See [mesh.md](./mesh.md).
- **lighting**: each allowlisted WLED controller's reachability and current state. Reachable-but-off is green (a dark portal is a decision, not a fault); unreachable is yellow, never red, so lighting can't make `dash_status` report the network as red. See [lighting.md](./lighting.md).

Neither tile can alert. They are absent from the pager's signal list, and naming one in `[dash.pager] signals` is a config error, not a way to opt in.

The mesh tile never transmits: it reads the node table passively and position history from disk, never the locate path. Its serial read runs every `read_interval` (5 m) rather than every cycle, because the mesh port is exclusive-access and `obi mesh locate` needs it. Each tile is omitted entirely when its subsystem isn't configured.

## Maintenance mode

Things that are deliberately off (torn-down sites, a switched-off radio station, the portal lights) stay visible but stop alarming. Two sources are unioned into one effective set, honored by every alarm path: DASH tiles and overall state, `dash_status`, the pager, the status band, the watcher's passes and log window, and the radio station's DMs.

1. **UISP's own flag.** Mark a device *Maintenance* in the UISP controller and obi picks it up from the snapshot on the next cycle (cached through snapshot gaps). The flag also covers the router-side netwatch probe of the same link (`aspen-ptp-cedar-11` ⇒ `aspen-cedar-11`), but never spreads to the rest of the site.
2. **The config declaration**, for everything UISP doesn't manage (gateways, APs, netwatch, whole subsystems):

```toml
[maintenance]
reason     = "post-season teardown"
sites      = ["aspen", "oak"]                    # whole name tokens: aspen-gw, birch-ptp-oak-60,
                                                 # cedar-gw→cedar-oak-11 — never oakmont-gw
uisp_sites = ["Valley - Aspen", "Valley - Oak"]  # UISP outage events are keyed by SITE name
signals    = ["radio", "mesh", "lighting"]       # radio = the walkie-talkie station's DMs
# devices  = ["dogwood-gw"]                      # exact identities, for one-offs
```

Effects of a match:

- The item is labeled `maintenance` and shown, but excluded from its tile's rollup, the board's overall state, the status band and the pager.
- If the item was already paged, its incident is closed without a recovery message, and the closure is audited.
- The watcher skips matched items at the same point its `ignore_*` lists apply.

There is no expiry, so obi keeps the declaration in sight:

- `obi dash show`, the web DASH widget and every `dash_status` answer carry a `MAINT:` line naming it.
- Any entry that matches nothing (a typo like `shorline`) is called out.
- Every change to the effective set is audited.

To leave maintenance, delete the entry. The next cycle re-evaluates the item normally, so something still down pages once, as a fresh outage.

- No restart is needed: dash and watch re-read config every run, and the radio station re-reads `[maintenance]` each time it would send a DM.
- The section is parsed strictly: an unknown key or signal is a config error (exit 2), not a silent no-op.
- If the radio station can't read the section, it sends the DM anyway.
