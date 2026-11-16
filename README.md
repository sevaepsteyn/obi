# obi

obi is a network operations assistant built to run the backbone of a large temporary event
network: dozens of routers, point-to-point radios and access points spread over many kilometres,
run by a small volunteer team. It collects inventory, health, metrics, logs and configuration
history from the gear (MikroTik RouterOS, Ubiquiti UISP and UniFi, Siklu EtherHaul, anything
that speaks SNMP), and puts an LLM in front of it. You can ask it questions from the terminal,
from Slack, from a browser workspace, by voice, or over a two-way radio. Device access is
read-only by design; the few deliberate exceptions (decorative status lighting) are allowlisted
and audited.

Everything that identifies a particular deployment (addresses, device and site names,
coordinates, call signs, chat IDs, service URLs) lives in your own config and state, outside
this repository. The examples and test data use a fictional network. Start with
[Install](#install) and [Configure](#configure); the optional event features (city grid, gate
traffic, paging, radio) stay off until you configure them.

License: [MIT](LICENSE).

## Where it started

Read-only MikroTik (RouterOS) network auto-discovery, inventory, and health status.

`obi` maps the Mikrotik network and reports each device's health through three commands:

- **`obi routeros discover`** (run occasionally) crawls the network from one or a few seed devices,
  following each device's neighbor table outward — bounded by an operator-defined scope — and
  writes a durable **inventory** (the roster: identity, address, provenance, no live health).
- **`obi status`** (run often) reads that inventory as its source of truth and reports each
  device's live health (uptime, CPU, memory) as a table or JSON.
- **`obi chat`** is a natural-language, read-only front door over the same core — ask about the
  network in plain language instead of remembering subcommands.

It is **strictly read-only** — it never changes device or network state — and resilient to
field conditions: a slow/unreachable device never blocks the rest, the crawl is hard-bounded
(scope + visited set + time budget), and every action is recorded in a durable audit trail.

## Install

[`uv`](https://docs.astral.sh/uv/) manages the entire Python stack (Python, venv, deps).

```bash
uv sync   # creates the venv and installs from uv.lock
```

## Configure

Create `~/.config/obi/config.toml` (see quickstart (feature 001)):

```toml
[discovery]
seeds  = ["192.0.2.1"]     # where the crawl starts
ranges = ["10.0.0.0/16"]   # the crawl BOUNDARY — neighbors are followed only inside this

[routeros]
username = "obi-readonly"
password = "your-readonly-password"   # plaintext — protect the file
use_tls  = true

# optional:
# [connection]
# timeout = 4        # per-device probe/query timeout (s)
# concurrency = 16   # max concurrent probes/queries
# budget = 30        # total crawl wall-clock budget (s)
# [audit]
# path = "~/.local/state/obi/audit.jsonl"
# [inventory]
# path = "~/.local/state/obi/inventory.json"
```

> **Boundary:** the crawl follows neighbors only to addresses within the configured scope.
> Seeds alone bound it to exactly those seeds — to map a network, configure the `ranges` that
> bound it. This is intentional containment (it can never wander into networks it shouldn't).

The password is stored in plaintext, so protect the file and use a **least-privilege,
read-only** RouterOS user (the real blast-radius control):

```bash
chmod 600 ~/.config/obi/config.toml
```

## Run

```bash
# 1) Build / refresh the inventory (run occasionally):
uv run obi routeros discover                 # crawl from seeds → write inventory.json
uv run obi routeros discover --json | jq .   # the inventory document
uv run obi routeros discover --budget 60     # allow a longer crawl

# 2) Report live health from the inventory (run often):
uv run obi status                       # all configured vendors, one section each
uv run obi status --json | jq .         # machine-readable JSON
uv run obi routeros status              # just the Mikrotik section
```

- `discover` crawls neighbor tables outward from the seeds, bounded by scope, and persists a
  roster of every device found — each with its discovery **provenance** (seed / neighbor-of).
  Non-Mikrotik nodes are recorded as `unsupported` and never traversed.
- `status` queries exactly the inventory's devices read-only and prints **every** roster entry
  with its reachability, raw metrics, and discovery state. Re-running `discover` refreshes the
  roster (new devices added; vanished ones flagged `absent`).
- With **no inventory yet**, `obi status` tells you to run `obi routeros discover` first and exits `2`.
- Each vendor also has a scoped view — `obi routeros status`, `obi uisp status`, and
  `obi unifi status` print only that vendor's section of the combined report.
- Exit codes: `0` produced a result (even if some devices are down or none were found);
  `2` usage/config error or status-without-inventory.
- Every crawl, probe, and query is appended to the audit trail:
  `tail ~/.local/state/obi/audit.jsonl`.

### Ask obi (chat)

`obi chat` answers questions about the **whole network** in plain language — Mikrotik, UISP, Siklu,
and what's **documented** in NetBox, read-only:

```bash
uv run obi chat                           # interactive session
uv run obi chat "what's down right now?"  # one-shot (scriptable), spans both vendors
uv run obi chat "what UISP devices are offline?"
uv run obi chat "are any Siklu links down?"               # E-band PtP backhaul radios
uv run obi chat "what is core-gw-1 supposed to be?"          # NetBox documented (source of truth)
uv run obi chat "is anything on the network not documented in NetBox?"   # documented vs discovered
uv run obi chat "show me the interface traffic on core-gw-1 over SNMP"   # live SNMP metrics
uv run obi chat "what errors did birch-gw log in the last hour?"         # device syslog
uv run obi chat "show the ip routes on core-gw-1 for 198.51.100.0/24"       # any live RouterOS read
uv run obi chat "can core-gw-1 reach 8.8.8.8?"                           # ping/traceroute from a device
uv run obi chat "what's the wind doing tonight?"                         # BRC weather + NWS alerts
uv run obi chat "how long is the gate wait time?"                        # gate travel time, as published
uv run obi chat --json "summarize the network" | jq .
```

It answers from the persisted Mikrotik inventory plus live read-only reads — and from a **live
UISP controller read** for UISP devices (one read covers the fleet), including **wireless link RF
health** (`uisp_links`: per-link state, signal, link scores, frequency, distance — *"how's
the chestnut backhaul?"*). UISP is read live: there is
no cached UISP roster, no UISP neighbor/topology, and no UISP rescan. It also reads the
**documented** inventory from **NetBox** — the network's *intended* state (sites, roles, models,
documented status, primary IPs), **not** live health: documented status (active/planned/offline)
is never presented as current reachability. Because chat sees both the discovered/live network and
the documented source of truth, it can **reconcile** them in one conversation — flagging devices
present-but-undocumented, documented-but-absent, or whose documentation doesn't match reality.
It answers **"how much traffic?"** from obi's own **stored metric history** (`metrics_history`) —
the time series the SNMP collector has been accumulating — reporting **rates**, in bits per second
with the average and the peak, over any window (*"how busy has the uplink fiber been today?"*, *"which
interface is busiest on aspen-internet-gw?"*), plus the trend of interface errors, CPU, memory and
temperature. A rate needs two readings over time, so the live spot read (`snmp_metrics`) — which
returns cumulative counters from one moment — can never express utilization; `metrics_history` is
what answers throughput questions, and `snmp_metrics` covers present-moment port state and devices
the collector doesn't target. History reads a **local store and contacts no device**, so it answers
even with the network down, and an empty result is always reported as *missing data*, never as zero
traffic. It answers **hardware-health** questions (`query_health`) from the feature-096 health
store — *"how hot is core-gw-1?"*, *"any PSU or optics problems?"*, *"how full are the collectors'
disks?"* — with breach verdicts from the **same thresholds** the health watcher alerts on and the
HEALTH board shows (the surfaces cannot disagree); another local, contacts-nothing read whose
answers stay bounded, so it works on every surface down to the radio. It answers **"where is
Tumble?"** (`mesh_locate`) from the Meshtastic radio attached over USB — resolving a mesh node by
name and translating its position into a **Black Rock City address** ("6:15 & A", "6:15 and 2,670
feet from the Man") with its age and how the node was last heard; passive-first, with at most one
**rate-limited** on-mesh position request when the stored position is missing or stale (needs a
`[meshtastic]` section — an empty one is a valid all-defaults opt-in; the survey translation reuses
the feature-098 city cache). It answers **weather**
questions (`weather_forecast`) from the feature-110 snapshot store — *"what's the wind doing
tonight?"*, *"any weather alerts?"*, *"what was it like at 2am?"* — Black Rock City current
conditions, official NWS alerts for the BRC zone, and the raw hour-by-hour numbers for the next
24 hours (temperature, humidity, dew point, precipitation, wind/gusts), always stamped with the
snapshot's age; the hourly cron fetches Open-Meteo + NWS, the tool reads only the local store, so
weather answers keep working when the playa uplink is down. It answers **gate travel-time**
questions (`gate_traffic`, feature 112) from the local traffic store — *"how long is the gate
wait?"*, *"how slow is gate?"*, *"is the gate open?"* — serving the Gate/Perimeter TOC's published
Travel Time Log reports and the dashboard's ingress/egress status **verbatim** with their ages
(never a computed or predicted wait); the 5-minute cron polls the BRC dashboard, and pre-event —
while the dashboard publishes nothing — the answer states that honestly. It also searches
**device syslog** (`logs_query`) — the historical RouterOS log lines obi
has ingested from the collectors — filtered by host, severity, text, and time window.
It can additionally query an **external VictoriaMetrics monitoring stack** (feature 108 — a
**second-tier** source behind obi's own stores, reached read-only over HTTP when `[victoria]`
is configured): the **backbone PtP radios' RF** (`backbone_rf_health` — CINR/RSSI,
modulation, capacity and trend for one radio or a whole site), the store's **weather stations**
(`weather_observed` — wind, gusts, temperature, rain, with the reading's age; wind numbers gate
tower climbs), and the rest of that store via free-form PromQL (`victoria_query`) grounded
by discovery (`victoria_discover`). Questions about devices obi collects itself still route to obi's own
`metrics_history`/`snmp_metrics`/`query_health`; the victoria tools cover what obi doesn't collect
and cross-check what it does. For RouterOS it
can additionally read **anything live**: `routeros_query` issues a read (`print`) of any RouterOS
menu (`interface`, `ip/route`, `ip/firewall/filter`, wireless registrations, …) with optional
filtering and field selection, and `routeros_ping` / `routeros_traceroute` run bounded diagnostics
**from** a device. These are **read-only by construction** — `routeros_query` only ever issues
`print` (a static denylist refuses `set`/`add`/`remove`/`reboot`/`fetch`/… before any device
contact), every result is **redacted** (passwords, keys, SNMP communities never reach the model),
and replies are size-capped. The autonomous watcher (`obi watch`) can ground its findings in the
same read-only tools a human has here — the three RouterOS live tools, UISP device health
(`uisp_status`) and wireless-link RF (`uisp_links`), Siklu radio/link health (`siklu_status`),
stored device config (`config_query`), stored metric history (`metrics_history`), and the external
store (the four feature-108 tools) — each gated
by the corresponding section being configured. Live device/controller reads (the RouterOS trio plus
the UISP, Siklu, and victoria reads) are bounded per run by `[watch].investigation_live_call_budget`
(default 8; `0` disables the live tools for the watcher); `config_query` and `metrics_history`
read only local stores and are not budgeted. Per-call
RouterOS caps live under `[routeros.live]`.
Whole-fleet questions consult every source and label each device. Chat runs with **any source
combination** — Mikrotik, UISP, NetBox, and/or logs, including a **logs-only** host. It **declines** any
change ("obi is read-only") and audits every tool call (with your question as the intent). Chat is the one **cloud-dependent** feature —
a deliberate, time-bounded carve-out (constitution v1.3.0): it needs network access to a
reasoning model and an API key (`ANTHROPIC_API_KEY` or `[chat].api_key`), and **degrades cleanly**
when offline while `discover`/`status` keep working. Exit codes add `3` (model unreachable / no
key) and `4` (no inventory — run `obi routeros discover` first).

```toml
# optional, in ~/.config/obi/config.toml:
# [chat]
# api_key = "sk-ant-..."        # or set ANTHROPIC_API_KEY
# model = "claude-sonnet-5"     # default
# max_tokens = 1024             # default
# context_file = "~/.config/obi/network.md"   # optional operator network context (see below)
```

**Network context (optional).** Point `[chat].context_file` at a free-form document describing
*your* network — site names, the device-naming grammar, address planes, topology — and chat reads
it once at startup to **interpret your terminology** and route the right read-only tools (so "the
cedar gateway" resolves to `cedar-gw`). It is reference for interpretation only: obi still
grounds every fact in tool output and never answers from the document. Keep it under **16 KiB**
and put **no credentials** in it — it is sent to the cloud model as background. A missing,
oversized, or unreadable file is skipped with a warning (chat still runs); with no `context_file`
set, chat is unchanged.

### Ask obi from Slack

`obi slack` runs a long-lived listener so authorized operators can ask obi the same read-only
questions **from Slack** — no shell access to the host needed. It is the same chat brain over a
second interface:

```bash
uv run obi slack run    # connect to Slack and answer DMs until Ctrl-C
```

To keep it running unattended, supervise it with launchd instead of a terminal — see
[Running obi's daemons](#running-obis-daemons-launchd) below (`obi slack start`).

- **DMs only.** obi answers only in direct messages; it never posts network data into a channel
  (an @-mention just gets a "please DM me" nudge).
- **Allowlist.** Only Slack **user IDs** in `[slack].allowlist` may use obi; everyone else gets a
  brief refusal and the attempt is audited. Every answer is audited with the asker's user ID.
- **Thread = session.** Replies in a DM thread continue one conversation; a new top-level DM
  starts fresh.
- **Reply to a report.** Replying in the thread under a watch report (or any thread obi has no
  session for, e.g. after a restart) pulls that thread's earlier messages from Slack as context —
  ask "which device is this about?" right under the report and obi knows. Seeded context is
  bounded (newest 40 messages / 16k chars); if the thread can't be read, obi says so and answers
  without it.
- **Connects outbound** via Socket Mode (no public endpoint/port-forward). Read-only and
  cloud-dependent like `obi chat`; the offline core is unaffected whether or not the listener runs.

```toml
# in ~/.config/obi/config.toml (reuses [chat] for the model):
# [slack]
# bot_token = "xoxb-..."          # Bot User OAuth Token
# app_token = "xapp-..."          # App-Level Token (Socket Mode)
# allowlist = ["U0XXXXXXXXX"]     # authorized Slack user IDs
```

See the Slack quickstart (feature 009) to create the Slack app and run it.

## Running obi's daemons (launchd)

obi has two always-on processes — the **Slack listener** (`obi slack run`) and the **web server**
(`obi web serve`). On macOS you can supervise each with launchd behind obi's own commands, so it
restarts on crash and comes back after a reboot, without hand-editing a plist:

```bash
obi web start          # install + load a LaunchAgent (RunAtLoad + KeepAlive), start now
obi slack start        # same for the listener  (add --no-login to skip start-at-login)
obi web status         # launchd state + a live /healthz probe + cron-collector freshness
obi slack status       # launchd state + the listener's heartbeat + collector freshness
obi web restart        # kickstart -k after a deploy
obi web stop           # bootout — gone, and NOT restarted until the next `start`
obi web logs --follow   # tail ~/.local/state/obi/web-serve.log
```

- **Native supervision.** `start` writes `~/Library/LaunchAgents/com.obi.<name>.plist`, loads it
  into your user session, and points stdout/stderr at the existing log. Auto-restart on crash
  (`KeepAlive`), auto-start at login (`RunAtLoad`, unless `--no-login`).
- **Real liveness.** `status` doesn't just say "the process exists": `web` runs a live
  `GET /healthz` (200 ⇒ healthy), and `slack` reads the listener's heartbeat to tell *connected*
  from *alive-but-wedged*. Both also show the eleven cron collectors' freshness.
- **No secrets in the plist.** Daemons read `~/.config/obi/config.toml` at runtime; the plist only
  sets `PATH`. If your API key is in the environment instead of `[chat]`, add it under
  `[service].environment`.
- **Primary checkout only.** `start` refuses a daemon whose `obi` lives in a feature worktree
  (`~/src/obi-worktrees/…`) — a supervised daemon must run from the stable checkout (override with
  `--exec`).
- **Cutover.** If a listener is already running from a hand-typed `nohup obi slack`, stop it first
  (`obi slack start` warns you) so you don't run two listeners.
- **Config.** Optional `[service]` section:

```toml
# in ~/.config/obi/config.toml
# [service]
# run_at_login = true                  # default RunAtLoad for `start` (--login/--no-login override)
# environment = { }                    # extra env for the daemons (only if a secret lives in env)
```

Log rotation is not handled here (the daemon logs are tiny); cron stays on cron (this only
supervises the two persistent daemons and *reads* the collectors' freshness). Full walkthrough:
specs/081-daemon-mgmt/quickstart.md (feature 081).

## UISP (second vendor)

obi can also report health for devices managed by a **UISP controller** (Ubiquiti's ISP
platform). UISP is queried **live** by `obi status` — point obi at the controller and it pulls
the whole managed fleet in one read-only call. This is separate from the Mikrotik roster: UISP
devices are not persisted, and `obi routeros discover` stays Mikrotik-only. See the
UISP quickstart (feature 003).

Add a `[uisp]` section to the config (you can run UISP-only, Mikrotik-only, or both):

```toml
[uisp]
base_url   = "https://uisp.example.com"   # no trailing slash; obi appends /nms/api/v2.1
token      = "YOUR_UISP_API_TOKEN"         # Settings → Users → API Token; never logged/audited
verify_tls = true                          # default on; set false for a self-signed controller
timeout    = 15                            # one call covers the whole fleet
name_filter  = "ptp"                       # optional include filter; override with --filter
ignore_names = ["lab"]                     # optional exclude: sites/devices/links out of your scope
```

```bash
uv run obi status                  # UISP devices, vendor-labeled (offline ones still listed)
uv run obi status --filter ptp     # focus a large fleet by device name (case-insensitive)
uv run obi status --json | jq .    # uisp-status-output schema
uv run obi uisp status             # only the UISP section (same flags)
```

A controller that is unreachable, slow, or rejects the token is reported as a clean,
guided result (no crash, no hang) and obi still exits `0`. The API token travels only in the
request header — never in output, the audit trail, or logs.

**Ignoring out-of-scope parts of the controller (`ignore_names`, feature 035).** A UISP
controller often reports sites and devices you don't manage. `ignore_names` is a case-insensitive
substring list that marks matching UISP entities as **out of scope for alerting and listing**:
`obi uisp links`, `obi uisp status` / the `obi status` UISP section, the `uisp_links` chat
tool, and the `obi watch` UISP passes (log/outage findings and link findings) all drop matching
entities. It is the exclude companion to `name_filter` — the include filter narrows first, then
`ignore_names` removes matches from what remains (a name matching both is excluded). Crucially, it
**never gates ingest**: `obi uisp logs ingest` keeps collecting every event and outage, so the
ignored rows stay in the store and remain queryable (`obi logs query --source uisp --contains
lab`). The knob is **UISP-scoped** — a RouterOS or UniFi host that coincidentally shares the
substring is not affected. Accepts a single string or a list; empty/absent excludes nothing.

### Wireless link health (`obi uisp links`)

Beyond per-device status, obi surfaces the controller's **wireless link** knowledge (feature
034): one row per wireless data-link — both endpoint radios (and sites), state, link signal,
SSID, frequency, distance — joined with each end's radio quality from the device list (link
score, per-radio signal, airtime, theoretical capacity). Down links render in red; an end whose
link score falls below 0.5 is flagged as quality-degraded. Read-only (two GETs), never persisted.

```bash
uv run obi uisp links                    # every wireless link, RF health joined per end
uv run obi uisp links --filter chestnut     # either-endpoint name filter (case-insensitive)
uv run obi uisp links --json | jq .counts
```

Chat/voice get the same view as the `uisp_links` tool — *"how's the chestnut backhaul?"*,
*"any weak links right now?"* — and the autonomous watcher can both consult it while
investigating and **alert on link changes** (see "Watching wireless links" below). See the
uisp-links quickstart (feature 034).

## Siklu (point-to-point radios)

obi reports health for **Siklu EtherHaul** mmWave point-to-point radios — the E-band backhaul hops
(e.g. the 70 GHz `showers ↔ mountain` link). Siklu has **no controller**: each radio is managed
individually, so obi reads each one **live over SSH** (read-only `show` commands only) from an
operator-maintained **roster**. There is no auto-discovery — these L2 radios don't appear in the
Mikrotik crawl — and Siklu is separate from the Mikrotik roster and the UISP fleet. See the
Siklu quickstart (feature 020).

Add a `[siklu]` section listing the radios and a read-capable account (run Siklu-only, or alongside
any other vendor):

```toml
[siklu]
hosts = [
  "aspen-ptp-birch-70.net.example.net",
  "birch-ptp-aspen-70.net.example.net",
]
username = "obi-readonly"     # a Tech-level (read) account; never logged/audited
password = "your-password"    # plaintext — protect the 0600 config file
timeout  = 10                 # per-radio SSH timeout (s); radios are queried concurrently
# pairs  = [["odd-name-a", "odd-name-b"]]   # only for radios whose names don't follow the
                                            # <site>-ptp-<far>-<band> naming convention
```

```bash
uv run obi siklu status                   # per-radio table: identity, model, firmware, uptime,
                                          #   temp, RF link (up/down, RSSI, CINR, modulation,
                                          #   TX power, capacity), and Ethernet-port state
uv run obi siklu links                    # each point-to-point hop as ONE logical link
uv run obi siklu status --filter showers  # focus by radio name (case-insensitive)
uv run obi siklu status --json | jq .     # siklu-status-output schema
```

Siklu radios also appear in `obi status` (their own section) and are askable in `obi chat`
(e.g. *"are any Siklu links down?"*). With `--links`, obi pairs the two ends of each hop by the
naming convention and flags only **hard** disagreement — one end up while the other is down, or an
end unreachable — it does **not** flag the per-direction modulation/capacity differences that are
normal for an adaptive E-band link. A radio that is unreachable, slow, or rejects credentials is
reported as a clean, guided result (no crash, no hang) and obi still exits `0`; the password is used
only to open the SSH session and appears in no output, audit record, or log.

## NetBox (source of truth)

obi can also read your **documented** inventory from **NetBox** (DCIM). Unlike Mikrotik/UISP
(live health), NetBox documents the network's *intended* state — sites, devices, roles, models,
documented status, and primary IPs. `obi netbox inventory` reads it live, read-only (GET only), following
pagination to return the complete inventory. This is documentation, not live health: for live
device status use `obi status`. See the
NetBox quickstart (feature 007).

Add a `[netbox]` section (you can run NetBox-only, or alongside Mikrotik/UISP):

```toml
[netbox]
base_url   = "http://netbox.example.com:8000"  # no trailing slash; obi appends /api/dcim/devices/
token      = "YOUR_NETBOX_API_TOKEN"            # Profile → API Tokens; never logged/audited
verify_tls = true                               # default on; only applies to https:// instances
timeout    = 15                                 # one read covers the inventory (pages followed)
name_filter = "core"                            # optional default filter; override with --filter
```

```bash
uv run obi netbox inventory                # documented devices (name, site, role, model, status, IP)
uv run obi netbox inventory --filter core  # focus by device name (case-insensitive)
uv run obi netbox inventory --json | jq .  # netbox-output schema
```

A NetBox instance that is unreachable, slow, or rejects the token is reported as a clean,
guided result (no crash, no hang) and obi still exits `0`. The API token travels only in the
request header — never in output, the audit trail, or logs.

## SNMP (live metrics, any vendor)

obi can read **live SNMP metrics** from the devices it already knows — the Mikrotik discovered
roster and NetBox-documented hosts — read-only (GET/GETNEXT/GETBULK only, never SET). SNMP is the
cross-vendor path for the metrics operators reach for during the event: interface traffic and
per-port up/down state, plus device CPU, memory, and uptime. It is **real-time only**; collecting
and storing SNMP history is a separate future feature. See the
SNMP quickstart (feature 012).

Add an `[snmp]` section (optional and independent — it layers over your existing rosters):

```toml
[snmp]
version   = "v2c"        # "v2c" (community, default) or "v3" (authenticated + encrypted)
community = "public"     # required for v2c; a credential — never logged/audited/rendered
port      = 161
timeout   = 5.0          # per-target; bounded so a dead agent never stalls the sweep
profile   = "system"     # default metric group: "system" or "interfaces"
# v3 instead: version = "v3", v3_username, v3_auth_key, v3_priv_key (all secrets)
```

```bash
uv run obi metrics live                                   # system metrics for every known device
uv run obi metrics live --target core-gw-1 --profile interfaces   # per-port status + counters
uv run obi metrics live --source netbox --json | jq .     # snmp-report schema
```

Each device is reported with an explicit per-device outcome (reachable / auth-failed / unreachable
/ timed-out) — a device with SNMP off, a wrong community, or a slow agent never aborts the sweep or
drops the others. The community string and any v3 keys appear in no output, log, or audit record.
You can also ask in chat — e.g. `obi chat "show me the interface traffic on core-gw-1 over SNMP"`.

## Historical metrics over SNMP (`obi metrics`)

`obi metrics live` is a live spot-read; **the rest of `obi metrics`** keeps the readings. A scheduled collector polls
your Mikrotik gateways over SNMP and persists each reading into a local **time-series store**
(SQLite, **separate** from the logs DB), so you can ask how a metric behaved **over time** — not
just right now. It is obi's first persistent time-series store: strictly read-only, fully offline,
and opt-in. See the metrics quickstart (feature 021).

Add an `[snmp.history]` subsection under your existing `[snmp]` section — its presence opts you in
(a config with only `[snmp]` leaves the collector inactive and `obi metrics live` unchanged):

```toml
[snmp]                       # required — provides the SNMP credential the collector reuses
version   = "v2c"
community = "your-ro-community"

[snmp.history]               # presence opts in (feature 021)
db_path  = "~/.local/state/obi/metrics.db"   # SEPARATE from logs.db
targets  = "-gw"             # name-substring filter on roster identity (the gateways)
profiles = ["interfaces", "system"]          # interface counters + system gauges
retain   = "90d"             # readings older than this are pruned each cycle
```

Each cycle collects, per matched gateway: every interface's counters — in/out **octets**, **errors**,
and **discards** (plus `ifOperStatus`) — and the device's system gauges (**CPU**, **memory**,
**uptime**).

```bash
uv run obi metrics ingest                                   # one cycle: poll → store → prune
uv run obi metrics history --target core-gw-1 --metric ifHCInOctets --since 24h   # per-iface rates
uv run obi metrics history --target core-gw-1 --metric ifInErrors --interface sfp-sfpplus6 --since 7d
uv run obi metrics history --target core-gw-1 --metric cpuLoad --since 2d          # gauge trend
uv run obi metrics status                                   # coverage: readings, devices, retention
uv run obi metrics history --target core-gw-1 --metric ifHCInOctets --json | jq .
```

Counter metrics are returned as **rates** computed from consecutive readings; a reboot (counter
reset) drops just that interval and a collection gap is spanned with the real elapsed time (no false
spike). Any window is auto-bucketed (~60 buckets, avg/min/max) so it stays readable. Schedule
`obi metrics ingest` on cron **staggered** beside the other ingest jobs (e.g. `3-59/5`). There is
**no backfill** — history is only as deep as the collector has been running (bounded by retention),
and a missed cycle is a permanent gap. The SNMP credentials appear in no output, log, or audit
record.

## Weather (`obi weather`, optional)

obi can keep a local, hourly snapshot of the **weather at one configured location**: current
conditions and the next 24 hours' raw hourly forecast from **Open-Meteo** (keyless; HRRR-blended,
imperial units, site-local times) plus, when you name an NWS zone, the active **NWS alerts**
for it (High Wind / Dust Storm Warnings and the like). The `weather_forecast` chat tool answers
**only from that store** — numbers, units, and the snapshot's age; no verdicts — so *"what's the
wind doing tonight?"* works on every surface (chat, Slack, voice, web, MCP, radio) and **keeps
working when the uplink is down**, aging honestly instead of erroring. History accumulates from
deployment on: *"what was the wind at 2am?"* answers from the snapshot recorded nearest that
time. There is deliberately **no weather watch pass and no widget** — weather never alerts.

Weather is **off until you configure a location** — there is no built-in one, and with no
`[weather]` section the weather tools are not offered to the model at all.

```bash
uv run obi weather run       # one snapshot: forecast + alerts (the hourly cron entrypoint)
uv run obi weather status    # snapshot age, per-source outcome, history coverage
```

```cron
# hourly at :17 (clear of the :00–:04 collector stagger); the log path feeds the
# dash pipeline tile's freshness
17 * * * * cd ~/src/obi && uv run obi weather run >> ~/.local/state/obi/weather.log 2>&1
```

```toml
[weather]
latitude = 37.16                  # required: the forecast location
longitude = -118.27               # required
place_name = "Example Valley"     # optional: how answers name the place
# nws_zone = "ABZ001"             # optional: the NWS forecast zone for alerts
# db_path = "~/.local/state/obi/weather.db"
# timeout_s = 20.0
# retention_days = 120

[site]
timezone = "America/Los_Angeles"  # local times in answers (default UTC)
```

A fetch failure is a **recorded outcome** on the snapshot (visible in `obi weather status`, the
dash `pipeline` tile, and served honestly by the tool as *"unknown as of …"*), never a crash;
each source fails independently, so NWS being down never costs the forecast, or vice versa.

## Event gate traffic (`obi traffic`, optional)

For an event with a published gate dashboard, obi can keep a local, 5-minute record of **gate
travel times** — the dashboard's **Travel Time Log** (*"Gate: 90 min"*) and its **ingress/egress
gate status**, polled with conditional requests. The `gate_traffic` chat tool answers **only
from that store** — the newest published report verbatim with its time and age, recent earlier
reports for trend, and the lanes' state + advisory — so *"how long is the gate wait?"* works on
every surface and **keeps working when the uplink is down**. obi **never computes or predicts a
wait**: what the dashboard published is what you hear. Before the event the feed may answer 403
— a recorded outcome, answered as *"the dashboard hasn't published travel times yet"*. There is
deliberately **no traffic watch pass and no widget** — traffic never alerts, never pages.

Traffic is **off until you configure `base_url`**; with no `[traffic]` section the tool is not
offered to the model.

```bash
uv run obi traffic run       # one poll: feed + gate status (the */5 cron entrypoint)
uv run obi traffic status    # newest entry, gate status, poll outcomes, coverage
```

```cron
# every 5 minutes; the log path feeds the dash pipeline tile's freshness
*/5 * * * * cd ~/src/obi && uv run obi traffic run >> ~/.local/state/obi/traffic.log 2>&1
```

```toml
[traffic]
base_url = "https://traffic.example.net"   # required: the event dashboard to poll
# timeout_s = 20.0
# retention_days = 120
# db_path = "~/.local/state/obi/traffic.db"
```

The feed's entry schema is **unverified until the event** (the pre-event 403 hides it), so
parsing is deliberately defensive: recognizable fields are used, every entry's raw JSON is
retained verbatim, and an entry that doesn't parse is still served as text with its capture
time — a schema surprise degrades polish, never availability.

## RouterOS config backup (`obi configs`)

obi can keep a **versioned, redacted backup** of every RouterOS device's configuration in a local
**git repository** — so you have both the current config and a full change history (who/what/when),
and `obi chat` can answer questions about it. It is **strictly read-only**: obi runs `/export` over
SSH and never writes to a device.

Why SSH and not the API: the RouterOS binary API can't return an `/export` to a read-only account
without writing a `.rsc` file *onto the device* (a mutation obi won't do). RouterOS honors a proper
SSH exec channel, so obi runs `/export` there and commits the result. This means the backup account's
RouterOS **group must include the `ssh` policy** (a read-only user is otherwise fine — `/export`
without `show-sensitive` reveals no secrets):

```
/user group set read policy=read,test,winbox,api,ssh,!sensitive
```

Configure it with a `[configs]` section (RouterOS-only; targets come from the `[routeros]` roster).
Its presence opts you in; credentials fall back to `[routeros]` unless you set your own:

```toml
[configs]                 # presence enables `obi configs ingest`
# username / password     # optional — omit to reuse the [routeros] account
# ssh_port   = 22
# timeout    = 10.0       # seconds per device (/export is slower than a status read)
# concurrency = 8
# targets    = ""         # roster-identity substring; "" = every identified device
# repo_path  = "~/.local/state/obi/config-repo"
```

```
obi routeros discover      # ensure the inventory roster exists
obi configs ingest         # SSH /export each device, commit only the changed ones
obi configs status         # coverage: who's backed up, how fresh, any orphans
obi configs show core-gw-1 --contains firewall   # current config (redacted), just the firewall
obi configs show core-gw-1 --at 1w               # as of a week ago
obi configs history core-gw-1                    # the change points (git history)
obi configs diff core-gw-1 --since 1w            # what changed in the last week
```

obi captures `/export terse` (one command per line → clean, meaningful diffs; set `verbose = true`
in `[configs]` to also include default values). `/export` hides passwords, wireless keys, and IPsec
secrets; the one thing it leaves cleartext — the **SNMP community** — obi scrubs before storing, so
no secret enters the repo. A device's file is committed **only when its config actually changed** —
the volatile `/export` timestamp header is normalized away, so an unchanged device never churns a
commit. Schedule
`obi configs ingest` on cron **staggered** beside the other ingest jobs (e.g. `4-59/5`, or less
often — config drift is slower than telemetry). A device that is unreachable is recorded and skipped
without affecting the others, and its last-known version is retained. In chat: *"show core-gw-1's
firewall rules"*, *"what changed on the gateway config last week?"*, *"when did NAT last change on
core-gw-1?"* — answered by the `config_query` tool from the local backup. Secrets never enter the
repo, and every run writes one audit record (counts only, never config text).

## Device syslog

obi can answer questions about **device syslog** — the RouterOS log lines your `syslog-ng`
collectors already keep. obi doesn't change the collectors: a scheduled **rsync** mirrors their
per-host daily files locally (an ops task — see the
syslog quickstart (feature 013)), `obi logs ingest` parses them into a
local **SQLite** store (incremental, idempotent, and deduped across collectors), and `obi logs
query` (or the `logs_query` chat tool) reads it — strictly read-only.

Add a `[logs]` section (you can run logs-only, or alongside Mikrotik/UISP/NetBox):

```toml
[logs]
mirror_root = "~/.local/state/obi/logs"     # where rsync lands; obi only reads it
db_path     = "~/.local/state/obi/logs.db"  # the local store obi ingests/queries
log_tz      = "UTC"                          # the collectors' RouterOS timezone
```

```bash
uv run obi logs ingest                                      # parse the mirror → local store
uv run obi logs query --host birch-gw --severity error --since 1h
uv run obi logs query --contains "login failure" --since 2026-06-03 --until 2026-06-04
uv run obi logs query --source syslog --since 1h            # device syslog only, no controller events
uv run obi logs query --host core-gw --json | jq .
```

Filters (all optional): `--host` (substring), `--severity`, `--contains` (full text),
`--since`/`--until` (a relative `1h`/`2d` lookback or an ISO date/time), `--source`, `--limit`.
`--source` selects a source **family** — `syslog` (device syslog only), `unifi`, or `uisp` — or any
collector hostname/substring (which still disambiguates a UniFi site from a like-named RouterOS
host). Results are newest-first and bounded. A missing or unreadable store is reported as a clean, guided result (no
crash); every query writes one audit record (your question + filters + match count) — never the log
text itself.

### UniFi controller events

UniFi's cloud-hosted console doesn't ship logs to `syslog-ng` (and its official API key exposes
inventory only), so `obi unifi logs ingest` **pulls** the controller's event log into the same
`[logs]` store via the reverse-engineered controller API — authenticated by a console-**local**
admin account (read-only; see the UniFi-logs quickstart (feature 016)).
Once ingested, the events answer the same `obi logs query` / `logs_query` questions as RouterOS
syslog and are judged by `obi watch`. Client connect/disconnect/roam churn (`CLIENT_DEVICES`) is
excluded by default, and the local store keeps the history long after the controller's short
retention window ages it out.

UniFi config is three sections — `[unifi]` (coordinates common to the console), `[unifi-network]`
(the official api-key inventory, feature 011), and `[unifi-internal]` (the local-admin creds this
feature uses). An existing `[unifi-network]`-only config keeps working unchanged:

```toml
[unifi]
base_url = "https://<console-id>.unifi-hosting.ui.com"
sites    = ["Aspen", "Birch"]        # omit for all sites

[unifi-internal]
username = "<local-admin>"            # a Restrict-to-local-access admin (MFA-exempt)
password = "<password>"
```

```bash
uv run obi unifi logs ingest         # poll each site → the [logs] store (first run backfills 7d)
uv run obi unifi logs status         # per-site ingest watermark
uv run obi logs query --host Aspen --since 1d   # query UniFi events like any other log
```

Schedule `obi unifi logs ingest` on the same `*/5` cron as the syslog rsync; a transient
unreachable/auth failure is recorded and never crashes the cycle, and the next run resumes from the
last watermark.

### UISP controller logs

The UISP controller (Ubiquiti's ISP platform, obi's second vendor — see above) keeps its own event
log and structured device-outage history, but only for a limited window. `obi uisp logs ingest`
**pulls** both into the same `[logs]` store via the read-only REST API and the existing feature-003
`[uisp]` token — the controller's event log (`GET /logs`) and device up/down records
(`GET /outages`). Once ingested, they answer the same `obi logs query` / `logs_query` questions as
RouterOS syslog and UniFi events and are judged by `obi watch`. A device-down **outage onset** is
recorded at `warning` (so the watcher alerts) and its **clearance** at `info`, so down duration is
recoverable. History outlives the controller's ~1-month retention. See the
UISP-logs quickstart (feature 019).

Ingestion is opt-in: add the flags to the existing `[uisp]` section (a status-only config keeps
working unchanged):

```toml
[uisp]
base_url = "https://uisp.example.com"
token    = "<api-token>"               # reused from feature-003 status
ingest_events   = true                  # GET /logs
ingest_outages  = true                  # GET /outages
first_run_lookback = "30d"              # events backfill + outages per-cycle rescan window
```

```bash
uv run obi uisp logs ingest          # poll the controller → the [logs] store (first run backfills)
uv run obi uisp logs status          # per-source (logs / outages) ingest watermark
uv run obi logs query --source uisp --since 1d   # query UISP events + outages like any other log
```

Schedule `obi uisp logs ingest` on cron next to the other ingest jobs; events are pulled
incrementally from a watermark, outages are re-scanned over the retained window each cycle and
deduped (a long-open outage clears with no incremental cursor), and a transient controller failure
is recorded, advances nothing, and never crashes the cycle.

## Web terminal (2D + 3D/AR/VR)

obi serves a **Bloomberg-terminal-style workspace to any browser on the LAN** from one
origin: dark mono theme, each tool in its own **floating window** — drag, resize, snap to
half/quarter, minimize, maximize — arranged how you like it, and the arrangement survives
reloads (saved per browser). Open tools from the **command line** at the bottom (`/`
focuses it; type a mnemonic — `MAP`, `TALK`, `XR` — and Enter) or from the `[+ widget]`
menu (the touch path, for headsets and phones). Every widget also runs full-bleed as its
own page at `?w=<id>` (`?w=map`, `?w=talk`, `?w=xr`); the old `?version=2d|3d` addresses
redirect there. Read-only end to end; needs the `[uisp]` section (the controller provides
coordinates + link topology). See the
workspace quickstart (feature 037).

The widgets: **MAP** (the 2D link map), **TALK** (the ask-obi conversation, voice + text
with a transcript), **XR** (site/eye-height/mode setup that launches the immersive 3D/AR/VR
session on a Quest 3 — links drawn over reality from where you stand; on headset browsers
the XR window opens by default), **LOGS** (the device-log tail, below), the **tool
boards** — split devices-vs-connections for the wireless vendors: UISP (UISP devices),
SIK (Siklu radios), and LINKS (the backhaul view: every device-to-device wireless
connection, UISP PtP links and Siklu pairs merged into one up/down/signal/band table) —
plus **ROS** (the RouterOS fleet — name, address, state, model, version, uptime, CPU,
memory; click a row to drill into that router, below),
UNIFI (UniFi devices), NBX (NetBox drift), WATCH (watcher findings) — plus **MET**
(interface/system metrics from the history store as time-series charts: reset-safe rates,
gaps drawn as gaps, one line per interface) and **CFG** (config-backup coverage, per-device
change history, colored diffs, and a **full-config view** — toggle a version's diff to its
entire stored config, filter it to matching lines, and copy it; all read from the local repo,
no device contacted) and **HEALTH** (fleet hardware health from the metrics store — temps,
PSU/fan states, CPU/memory, SFP optics, collector-VM disks — worst-first with breaches
named and colored; thresholds shared with the `[watch.health]` pass, so the board and the
Slack alerts can never disagree; click a RouterOS row to open its router window). Adding a
tool later is one registry entry + one widget module — the shell, launcher, and layout
machinery don't change.

**OSP (the routing map).** The OSPF topology as a pannable graph: per-direction costs,
the SPF tree from any root, ghost/backup links, SPOF rings, the internet cloud with each
router's default egress, and a lint window. Drag nodes (and the cloud) to taste — positions
persist per browser. To make an arrangement **the default for every browser** (fresh
profiles, the headset): press **export layout** on the OSP toolbar (copies the positions as
JSON), save it to a file on the obi host, and point `[web] ospf_layout_path` at it — from
the next `obi web snapshot` tick every listed router boots at the blessed position, routers
not listed keep the computed layout, stale entries are ignored (counted in the snapshot's
`meta`), and a missing or malformed file never fails the export (the problem lands in
`meta.layout_error`). Local drags still win over the blessed default. The file is exactly
what the button copied — no editing needed. Without a blessed layout the automatic
arrangement is a hop-ring tree around the best-connected router that also orders sibling
branches to keep cross-links short (accepted only when it strictly reduces line crossings).

**Router detail (ROS drill-down).** Click a router in the **ROS** board — or type
`/ROS <name>` / `/ROS <ip>` at the command line — to open a window for that one router: its
IP addresses, its interfaces with live running/disabled status, and a device summary
(model, version, uptime, CPU, memory); a **HEALTH** tab shows that device's newest
hardware-health readings with a 24 h trend sparkline per series (from the local metrics
store — no device contact, breaches marked). Each router is independent, so you can have several
router windows open at once; they persist across a reload and reopen where you left them.
The detail is a **live, read-only** RouterOS read (`/interface`, `/ip/address`,
`/system/resource` — print only), performed when you open or refresh the window: it needs
`[routeros]` configured, is redacted and audited like every other device read, and degrades
to a clear "unreachable"/"timeout" state (keeping the last good data) when a router is down.
This is the one place the LAN web server reaches a device — everything else it serves is a
local file or a cron snapshot (constitution 1.8.0).

Every table in the workspace sorts and resizes: click a column header to cycle
ascending/descending/original order (numeric-aware, blanks last; a sorted log tail pauses
its auto-follow), and drag a header boundary to resize columns — widths persist per widget
(double-click the boundary to reset).

Every table also **copies** (its visible rows as tab-separated text, ready to paste into a
sheet) and exports **CSV** (a `.csv` download of the shown rows, honoring the current
sort/filter — generated in the browser, so it works offline), and every text pane (CFG diffs
and full configs, PING/TRACE results, AUDIT payloads, WATCH reports) has a one-click **copy**.

```bash
uv run obi web export    # geometry → ~/.local/state/obi/ar/links.json (atomic, audited)
uv run obi web serve     # HTTPS on the LAN (default 8443); prints the URL(s) to open
```

The first export fetches each site's ground elevation from USGS once and caches it durably —
after that both commands work with **zero internet** (the viewer's three.js is vendored, no
CDN; the 2D map is dependency-free canvas with no external basemap or tiles). Serving is
HTTPS with a self-signed certificate (generated once; accept the browser warning once per
device) because WebXR and browser microphones only run on secure origins. UISP altitudes are
mixed-datum in the wild, so every resolved height records its provenance in the export;
hand-fix a site with `[web] ground_overrides` if needed. An optional `[web]` section tunes
paths/port/heuristics — all fields have defaults.

**MAP (any browser).** The whole network plotted by real coordinates on a plain
background — links colored by health (same palette meaning as the headset), marginal or
obstructed clearance drawn dashed, site labels, north arrow, scale bar. **Line thickness
follows each link's current capacity** and a small midpoint readout shows the numbers
(`2.16 Gbps ↓ / 1.75 Gbps ↑ · 2.2% used`) — a radio the controller has no numbers for
draws at a neutral width with the panel stating capacity is *unavailable*, never zero.
Links whose devices are in **maintenance mode render orange** (planned-dark), distinct from
the red of an unexpected outage. Pan, pinch-zoom, tap
a link for its details: endpoints and sites, health (with the maintenance flag when set),
band/frequency, length, signal, capacity and usage split by direction, the
terrain-clearance verdict with worst margin and location, and — when the export carries the
link's terrain profile — an **elevation-profile chart** along the path: terrain, line of
sight, the first Fresnel envelope, and the worst-clearance point marked. The data age shows
on the window's title bar. Works at phone width; the detail
panel becomes a bottom sheet.

**Frequency bands (both viewers).** Overlapping links along the same sight line untangle
with **per-band visibility toggles** — 5 GHz, 11 GHz, 60 GHz, 70 GHz (e-band), plus an
*other* group for anything unrecognized; only bands present in the data get a toggle. In
the 2D map they're chips in the corner; in the headset a chip palette floats at the left
of view — aim and pinch (hands) or point and trigger (controllers) to flip one. Hiding a
band hides its lines, labels, Fresnel zones, and terrain profiles as one unit, and the
choice **persists per browser** across reloads and live refreshes. The 70 GHz group is fed
by the **Siklu e-band hop**: when `[siklu]` is configured, `obi web export` also reads the
radios (read-only SSH, ~9 s) and merges their logical link into the viewer — geometry
inherited from the UISP radios on the same masts, capacity from the air interface, same
Fresnel/terrain treatment; unreachable radios degrade to a named exclusion, never a failed
export. Opt out with `include_siklu = false` in `[web]`.

**TALK.** The chat conversation in a window: hold the mic to talk, or type
and press Enter — questions, obi's answers, and tool-progress notes accumulate in a
transcript (session-scoped, never persisted). Voice mounts by default (feature 048); when it
is off (a missing STT/TTS dep or `[web] voice = false`) the widget renders an explicit
"voice is off" state, and a device without a mic still types.

**PING / TRACE.** Live network diagnostics from the workspace (feature 048): pick a source
RouterOS device from the roster, type a destination, and obi runs a **ping** or **traceroute
FROM that router** — the same read-only `routeros_ping` / `routeros_traceroute` tools obi uses
in chat, now one click away (and audited with source `web`). These run against a LAN device on
operator action, so — unlike the offline-by-construction viewer — they need `[routeros]`
configured; without it the widget says so. On by default (no flag).

**XR (Quest 3).** In the headset: pick the site you're standing at, set your eye
height, and nudge the thumbstick until a link lies on a dish you can see — everything else
falls into place. When the immersive session ends you're back in the workspace exactly as
you left it.

**LOGS (any browser).** The fleet's device-log tail in a window: RouterOS syslog + UniFi and
UISP controller events from the local log store, severity-colored, following live (new
entries appear within ~20 s of ingest; scroll up to pause, jump back to live). Filters
combine: source (`all / syslog / unifi / uisp` — `syslog` is device syslog only), device
substring, minimum severity, free text, time window. Served by a
read-only local-data route (`/api/logs.json`) that only reads `logs.db` — the web server
still never contacts a device or controller; freshness is the ingest crons'. The window
badge shows the newest entry's age, so a stalled ingest is visible at a glance.

**Tool boards (any browser).** Each read-only tool as a monospace board with a text filter,
counts, and the data's age on the window badge. The device/controller-touching tools (UISP,
LINKS, ROS, SIK, UNIFI, NBX) read **cron-written snapshots**: schedule `obi web snapshot`
(below)
and the boards stay fresh while the web server itself still never touches a device — a tool
whose controller is down keeps serving its last good snapshot with a growing age. WATCH
reads the local watch-reports store live (a read-only local-data route like LOGS); ROS adds
a per-router live drill-down (above) that is the one board reaching a device on demand.
Every board's data matches its CLI `--json` exactly. LINKS is the one two-snapshot
board (`uisp-links` + `siklu`, merged in the browser): if one vendor's snapshot is missing
its links still render from the other, the gap is named in the counts line, and the badge
age reports the OLDER of the two sources.

```cron
4-59/5 * * * * .../.venv/bin/obi web snapshot >> ~/.local/state/obi/web-snapshot.log 2>&1
```

(`:04` offset keeps it clear of the logs-sync, metrics, and export crons.) A tool snapshots
only if its config section exists; `--only uisp-links,status` splits cadences if you want
some tools fresher than others.

Press **A/X** in the headset to overlay each link's **first Fresnel zone** — the translucent
clearance ellipsoid the radio path needs, with a stronger inner surface at the 60% core that
must stay unobstructed. Stand under a mast, look up, and judge with your own eyes whether the
real tree line or ridge intrudes (passthrough reality is the terrain model). Zone size comes
from each link's actual channel frequency, read from the controller with provenance; labels
read `name · 9.8 km · F1 11.6 m`, with a second line carrying capacity/usage when known
and the maintenance flag when set. Link bodies are drawn as thin tubes whose **radius
follows capacity**, same scale as the 2D map's line widths. A link whose frequency can't
be resolved simply renders without a zone and is named in the export summary.

**Move around, zoom, rotate — controllers optional.** The scene is grabbable with bare hands
(Quest hand tracking, no controller batteries needed in the field): pinch-drag moves it,
two-hand pinch zooms and rotates it (spread, squeeze, turn), and a quick pinch tap toggles
the Fresnel zones. With controllers: trigger-drag grabs the world the same way, the left
stick moves, the right stick rotates (as before) and zooms, and A/X still toggles zones.
Zoom out until the whole network sits on a tabletop in front of you, or zoom in on one far
link; zoom and pan are clamped so the scene can never be lost.

| Hands | Controllers | Action |
|---|---|---|
| pinch + drag | trigger + drag, or left stick | move the scene |
| two-hand pinch, turn | right stick ← → | rotate (yaw) |
| two-hand pinch, spread/squeeze | right stick ↑ ↓ | zoom |
| quick pinch tap | quick trigger tap, or A/X | toggle Fresnel zones |
| — | B/Y | toggle terrain + peak labels |
| pinch a band chip | trigger a band chip | toggle that frequency band |
| pinch-hold the talk button | hold grip (squeeze) | push-to-talk to obi (voice is on by default) |

**Terrain & clearance.** The export also samples a public elevation model (AWS Terrain
Tiles — keyless; US data derives from USGS 3DEP) along every link's path and judges its
clearance: does the ground rise into the link's Fresnel zone, and by how much? Every
marginal or obstructed link is named in the export summary with its worst-case margin and
where along the path it sits (`obi web export --json` carries the same under
`clearance_flagged`); the math includes the standard 4/3-earth curvature bulge and anchors
the elevation model to the per-site USGS grounds so datum offsets can't invent
obstructions. In the headset, each link shows its **ground profile** as an earth-toned line
under the path, flagged links carry `⚠ margin @ location` in their labels, and a
low-opacity **terrain wireframe** (toggled with **B/Y**, on by default) puts the whole
network on its actual landscape — with **named summits labeled** from the USGS GNIS
gazetteer, which double as yaw-alignment landmarks (rotate a labeled wireframe peak onto
the real one). Peaks are opt-in: set `gnis_states = ["NV"]` in `[web]`. Like the elevation
cache, DEM tiles and the gazetteer are fetched once and cached durably under
`~/.local/state/obi/ar/` — every later export runs with zero internet; anything that can't
be built offline is skipped and named in the summary, never a failure. The terrain scenery
ships as a separate `terrain.json` each viewer version loads once per page, so the 5-minute
live poll stays as light as before. (The 2D version renders the same profile data as its
per-link elevation chart.)

**Live data.** Schedule `obi web export` with cron — the same pattern as obi's other data
fetches (log/metrics/config ingests) — and both viewer versions pick the new data up every
5 minutes: link colors, markers, and labels update in place without resetting your
viewpoint, selection, or zone toggle. Each run is the same read-only, audited, atomic
export — if the controller is unreachable it exits leaving the last good data being served,
and both versions show the data's age so staleness is visible at a glance.

```cron
2-59/5 * * * * .../.venv/bin/obi web export >> ~/.local/state/obi/ar-export.log 2>&1
```

**Migrating from pre-033 (`obi ar` / `obi voice web`).** Feature 033 merged the AR viewer
and the standalone talk page into this one surface — a hard rename, fleet-style: the old
commands fail naming their replacement, and a leftover `[ar]` config section is a loud
error. Three steps: rename `[ar]` → `[web]` in your config (keys unchanged; also delete
`[voice] web_port`/`web_bind` if set), change any cron line from `obi ar export` to
`obi web export`, and replace both old serve processes (`obi ar serve [--voice]` on 8443
and `obi voice web` on 8444) with the single `obi web serve`. Port 8443, the
certificate, the state dir, and every device's trust step survive; nothing listens on 8444
afterwards. Exact commands: quickstart §migration (feature 033).

## Voice (talk to obi)

`obi voice talk` is chat out loud: push-to-talk in the terminal, spoken answers back. Press Enter,
ask "is the showers link up?", press Enter again — obi shows what it heard, answers in short
conversational prose, and speaks the answer. Follow-ups keep context ("and its signal?"), and
Ctrl-C interrupts anything: a recording, a slow lookup, or obi mid-sentence.

```bash
uv sync --extra voice    # one-time: the local speech stack (Apple Silicon Mac)
uv run obi voice talk    # first run offers a one-time ~1.6 GB speech-model download
```

**What you say stays on the Mac.** Speech is captured, transcribed (MLX Whisper), and
synthesized (macOS `say`) entirely on-device; audio is never written to disk, never retained,
and never transmitted. Only the transcribed text goes to the reasoning model — exactly what
typed `obi chat` sends. The tools are the same read-only set as chat; the mic is live only
between your two Enter presses. Silent or noise-only captures are gated locally and never
reach the model at all.

An optional `[voice]` TOML section tunes it (all fields have defaults; strings quoted):

```toml
[voice]
stt_model = "mlx-community/whisper-large-v3-turbo"  # or "mlx-community/whisper-small-mlx" (~0.5 GB)
tts_voice = "Samantha"                              # `say -v ?` lists installed voices
tts_rate  = 190                                     # words per minute
```

### Voice in the browser (the web viewer's talk control)

The same spoken conversation from any browser on the network — phone, laptop, or the Quest 3
— lives on the **web viewer's origin** (since 033; it was the standalone `obi voice web`
talk page on 8444 before that):

```bash
uv run obi web serve   # voice + PING/TRACE on by default; set [web] voice = false to drop voice
```

On the **2D version**, a microphone button and a text box sit in the header: hold the mic,
speak, release, read what obi heard, hear the answer in obi's voice — or just **type the
question** and press Enter (typed questions join the same conversation and need no
microphone at all). Follow-ups keep context per page; reloading (or ~30 idle minutes)
starts fresh. Two once-per-device steps for the spoken path: accept the
self-signed-certificate warning (browsers grant the microphone only to a trusted HTTPS
origin), then allow microphone access on the first hold. With voice off on the server, the
mic renders grayed-out and says how to enable it.

In the **3D version**, ask obi without taking the headset off: **hold grip (squeeze) on
either controller — or pinch-hold the small talk button at the bottom of your view with
bare hands — speak, release.** A head-locked voice card walks through the turn (listening →
what obi heard → thinking, with the tools obi is consulting → the answer as text) while the
answer plays in obi's voice. One-time setup per headset: on the setup page, tap **Enable
microphone** *before* entering the immersive session (permission prompts can't render
in-immersive; the grant persists per origin). Declining just leaves voice off — every viewer
feature works unchanged, which is also true when voice is off (`[web] voice = false`). Failure modes
are spoken for in-scene: obi unreachable, too-quiet capture (wind), busy with the previous
question, model unavailable — each renders a distinct card, and the viewer keeps navigating
throughout.

Both versions drive the **one 024 turn engine** on the viewer's own origin — no second trust
step, no second port. The privacy boundary widens by exactly one hop: your speech is
captured in the browser, crosses **only your LAN** to the Mac, and is transcribed and
synthesized there — never retained, never sent to any cloud speech service. Only the
transcribed text goes to the reasoning model, as with typed chat. The service is
deliberately unauthenticated within the LAN (the viewer's trust model) and must never be
exposed beyond it. Read-only throughout; every turn is audited (`voice-web:<conversation>`
actor; headset turns use an `ar-` conversation prefix). `[voice]` keys (defaults shown):
`web_idle_minutes = 30`, `web_max_seconds = 90`. Other clients can integrate via the
documented turn contract
(specs/024-voice-web/contracts/voice-turn-api.md (feature 024)).

## Autonomous watcher

`obi watch` gives obi a first slice of autonomy: instead of waiting to be asked, it watches the
syslog store on its own. Schedule `obi watch run` right after the syslog rsync; each cycle reads
only the **new** log lines since last time, asks a cloud model what looks wrong, optionally does a
little read-only investigation, and writes a per-finding report (JSON + markdown) under the state
dir. It is strictly read-only and never changes device config. `obi watch` is a cloud-reasoning
feature (a sibling of `obi chat`, under the constitution's Principle VI carve-out): with no
connectivity a cycle is a clean no-op. It analyzes only RouterOS devices in your inventory
(`routeros_only`, default on) and can mute noisy topics via `ignore_topics`, or exclude a whole
log collector with `ignore_collectors` (feature 039) — e.g. `ignore_collectors = ["unifi"]` drops
every UniFi-sourced row from what the model judges. A collector's rows carry many varied topics, so
this is the exclusion `ignore_topics` cannot express as one rule; matching is case-insensitive on
the event's collector, and **ingest is untouched** so the data stays collected and queryable in
`obi chat` / `obi logs query`. Repeated alerts are
deduped (alert once per ongoing problem, re-alerting after a TTL), and the watermark is the
monotonic ingest id, so a device with a skewed clock can't blind it. There is no alert delivery in v1 — review findings on demand from the CLI.
Enable it with a `[watch]` section (`enabled = true`); see the
watcher quickstart (feature 014).

```bash
uv run obi watch run --since-now     # first enable: baseline the cursor forward from now
uv run obi watch run                 # one cycle (the cron target); no-op when no new logs
uv run obi watch reports --since 1d  # review recent findings (newest first)
uv run obi watch reports --show <id> # print one finding's full markdown report
uv run obi watch status              # last-run health + token usage (last run / 24h)
```

Schedule it on the same host as the syslog rsync, right after it:

```cron
*/5 * * * *  /path/to/rsync-syslog.sh && obi watch run >> ~/.local/state/obi/watch/cron.log 2>&1
```

`obi watch run` exits `0` even on handled degradations (store locked, cloud down — recorded, not
fatal), so cron stays quiet, and skips the model entirely when there are no new logs.

### Watching metrics for climbing errors

The same cycle can also watch the **metrics** store (feature 021): when an interface's error counter
climbs, a link is usually going bad. Unlike log-watching, this detector is a **deterministic rule**
(did the counter increase past a threshold over the window) — so it needs **no cloud model** and runs
even offline, and its findings ride the same dedup → report → Slack path. Opt in with a
`[watch.metrics]` subsection:

```toml
[watch]
enabled      = true
slack_notify = true          # DM findings to the operator (feature 015)

[watch.metrics]              # feature 027 — presence + enabled opts in
enabled   = true
window    = "1h"             # look back one hour each cycle
threshold = 0                # alert on ANY new error (raise it if a link trickles benignly)
# counters = ["ifInErrors", "ifOutErrors"]      # default; add "ifInDiscards","ifOutDiscards" to widen
# ignore_interfaces = ["wireguard"]             # default; interface-name substrings to skip
```

By default it **skips WireGuard tunnels** (`ignore_interfaces = ["wireguard"]`) — their out-errors
are usually benign no-route-to-peer drops, not a failing physical link; add more substrings, or set
it to `[]` to watch every interface.

It rides the existing `obi watch run` cron (nothing new to schedule) and reads `metrics.db`
read-only. A finding reads like `cedar-gw sfp-sfpplus5: +1,432 ifInErrors in the last 1h`;
alert-once keeps an ongoing condition to a single alert (per interface+counter) until a clean window
clears it, and a counter reset (reboot) is never mistaken for a change. Try it with
`obi watch run --dry-run` (evaluates and prints, delivers nothing). See the
watch-metrics quickstart (feature 027).

### Watching for a device going dark

A climbing counter catches a link going *bad*; the same cycle can also catch a device going
**silent** — one that stops responding to SNMP entirely and just drops out of the metrics store. This
is another **deterministic, offline** rule over `metrics.db`: a device is flagged when its newest
reading has aged past `stale_after_cycles` × its own observed collection cadence. A **single** missed
cycle is always tolerated (a transient timeout is not an outage); two or more consecutive misses
alert. Opt in with a `[watch.reachability]` subsection:

```toml
[watch.reachability]         # feature 030 — presence + enabled opts in
enabled            = true
stale_after_cycles = 2       # miss 2 consecutive cycles (~10m at a 5m cadence) ⇒ alert; 1 is tolerated
# mass_outage_ratio = 0.8    # if ≥80% of watched devices are silent at once, coalesce to ONE finding
# ignore_devices    = []     # device identities to never flag (e.g. a known-off gateway)
```

A finding reads like `core-gw-1: no SNMP metrics for ~11m (2 missed cycles); last reading 15:41`, and
rides the same dedup → report → Slack path (alert-once per device, silent clear + re-alert on a later
outage). When the collector or host itself stops — so *most* devices go silent at once — the pass
coalesces them into a single `Metrics collection appears to have stopped: 11/12 devices silent`
finding instead of a per-gateway storm. It reads the store read-only, needs no cloud, and rides the
existing `obi watch run` cron. See the
watch-device-down quickstart (feature 030).

### Watching wireless links (UISP)

The same cycle can watch the **UISP wireless links** (feature 034): a deterministic pass reads
the controller's link snapshot (two read-only GETs) and compares it against the previous
observation it keeps (`uisp-links-baseline.json` in the watch dir). A link that was active and no
longer is raises a *link down* finding; a link whose signal fell more than `signal_drop_db`
below the baseline raises a *degradation* finding. While degraded the baseline is **held** (not
re-based), so an ongoing sag alerts once and re-arms only after recovery. Opt in with a
`[watch.uisp_links]` subsection (requires `[uisp]`; validated at load):

```toml
[watch.uisp_links]           # feature 034 — presence + enabled opts in
enabled        = true
# signal_drop_db = 6.0       # alert when link signal falls > this many dB vs the held baseline
# ignore_links   = []        # substrings matched against either endpoint name or the link label
```

Findings read like `birch-gw ↔ birch-ptp-60: link signal fell 9.0 dB (−62 → −71 dBm)` and ride
the same dedup → report → Slack path (alert-once per link per condition; a link that disappears
from the controller is dropped silently — decommission, not an outage). The first cycle seeds the
baseline silently; an unreachable controller degrades the run (`uisp_links_error`) without
touching the baseline or the other passes. Detection needs no cloud model — only LAN reach to the
controller. See the uisp-links quickstart (feature 034).

To silence a whole out-of-scope site or device across *both* UISP watch passes (this link pass
**and** the log/outage findings from `obi uisp logs`), set `[uisp] ignore_names` (feature 035) —
see [UISP → Ignoring out-of-scope parts of the controller](#uisp-second-vendor). It is unioned
with `ignore_links` here, and unlike `ignore_links` it also excludes the entity from the CLI/chat
listings, while never affecting log ingest.

### Watch on config changes (feature 070)

The watcher can also notice when a **device's stored config changes** and DM you the diff. It reads
the config-backup git store (feature 026 — see [RouterOS config backup](#routeros-config-backup-obi-configs))
read-only: on each
cycle it compares every device's current committed config version against a per-device baseline it
keeps (`config-baseline.json` in the watch dir). A device whose config advanced to a new committed
version raises a finding carrying the **redacted diff** of what changed. It never fetches configs
itself (the `obi configs ingest` cron does that) and needs no cloud model. Opt in with a
`[watch.config_changes]` subsection:

```toml
[watch.config_changes]       # feature 070 — presence + enabled opts in
enabled        = true
# severity      = "warning"  # info | warning | critical (default crosses the Slack floor)
# max_diff_lines = 120       # bound the diff in an alert; the rest is one `obi configs diff` away
# ignore_devices = []        # case-insensitive substrings matched against the device identity
# repo_path     = "…"        # default = the feature-026 config store
```

Findings read like `core-gw-1: config changed (+3/−1 lines)` with the redacted diff inline, and ride
the same dedup → report → Slack path (alert-once per committed change; a subsequent change re-alerts).
The **first cycle after enabling seeds the baseline silently** — no backlog flood from pre-existing
history; only changes made afterward alert. A device newly appearing in the store is baselined, not
alerted; one removed is dropped silently. The diff is the already-redacted stored content (feature
026 scrubs secrets before commit), so an alert never carries a cleartext secret. Detection is
deterministic and fully offline. See the
watch-config quickstart (feature 070).

### Watching device health (feature 096)

The watcher can also alert on **hardware health**: overheat, a PSU or fan status leaving its
healthy value, SFP optics rx power under the floor (or rx-loss/tx-fault asserting), sustained
CPU/memory pressure, and a collector VM filesystem filling up. It reads the feature-021 metrics
store's health series (the `health`/`optics` SNMP profiles for the gateways plus the
`obi health probe` Linux collector readings) read-only and compares each series against fixed
config thresholds — no cloud model. Alerts are **transition-only** against a per-series baseline
(`health-baseline.json` in the watch dir): the first cycle seeds silently (a chronically
unhealthy sensor never alerts until it recovers and breaks again), recovery is a silent clear,
and a re-breach re-alerts. A device with **no fresh sample is skipped** — staleness stays the
reachability passes' business — and CPU/memory need `sustained_samples` consecutive hot readings
(a one-cycle spike never pages). Many series breaching in one cycle (a site heat event) coalesce
into a single finding. Opt in with a `[watch.health]` subsection:

```toml
[watch.health]               # feature 096 — presence + enabled opts in
enabled = true
# temperature_max_c     = 75.0   # board/CPU/switch sensors
# sfp_temperature_max_c = 70.0   # optics + device sfp-temperature sensors
# cpu_max_percent       = 90.0   # sustained (see sustained_samples)
# memory_max_percent    = 90.0   # sustained
# sustained_samples     = 3      # consecutive readings CPU/memory must breach
# disk_max_percent      = 85.0   # Linux collector filesystems
# rx_power_min_dbm      = -24.0  # optics floor (the platform exposes no vendor DDM levels)
# status_healthy_value  = 0      # what a healthy *-state sensor reads (0 on live evidence)
# severity              = "warning"
# mass_outage_ratio     = 0.80   # tracked-series breach fraction that coalesces
# ignore_devices        = []     # case-insensitive identity substrings
# ignore_sensors        = []     # case-insensitive sensor-name/metric substrings
# db_path               = "…"    # default = the feature-021 metrics store
```

Findings read like `birch-gw: board-temperature2 at 81 °C (limit > 75 °C)` and ride the same
dedup → report → Slack path. `obi health status` shows the same thresholds' verdicts worst-first
(the CLI and the pass share one evaluation — they cannot disagree). See the
096 quickstart (feature 096).
## Status dashboard (`obi dash`)

One green/yellow/red board answering **"is everything OK right now?"** — the fast, deterministic
counterpart to the watcher's deep hourly analysis (feature 095). A 1-minute cycle collects and
judges five signals: **fleet** reachability (every RouterOS device + backhaul radio, with RTT;
UISP maintenance-flagged devices show yellow without tainting the rollup), per-router
**netwatch** state (live table reads — up/down plus loss derived from the test counters; a
probe upgraded to `type=icmp` contributes real latency automatically), **ping** reference
latency (to 8.8.8.8/1.1.1.1 *from* the border router), **internet** uplink utilization in both
directions vs provisioned capacity (the 5-minute metrics store, honestly labeled), and
**pipeline** health
(are obi's own collectors writing?). Every read is read-only; no LLM anywhere. `stale` is a
first-class state that outranks green and never pages — a dead feed can't leave a green wall.

```toml
[dash]                          # presence + enabled opts in; see the 095 quickstart for the rest
enabled = true

[[dash.internet_links]]         # capacity is operator knowledge — no defaults
device = "aspen-internet-gw"
interface = "sfp-sfpplus5-uplink"
label = "uplink"
capacity_mbps = 2500

[dash.pager]
enabled = false                 # flip after a day of green board (needs the [slack] bot token)

[dash.mesh]                     # feature 121, optional — display-only, never pages
# heard_yellow_after = "2h"     # tracked mesh node quiet this long -> yellow
# heard_red_after = "12h"       # ... this long -> red
# fix_yellow_after = "6h"       # newest recorded position older than this -> yellow
# read_interval = "5m"          # how often the SERIAL read happens (see below)

[dash.lighting]                 # feature 121, optional — display-only, never pages
# timeout = 5.0                 # per-controller read deadline; defaults to [wled] timeout
```

```cron
* * * * * /path/to/.venv/bin/obi dash run >> ~/.local/state/obi/dash.log 2>&1
```

The dash cycle **absorbs the ICMP collector cron** — it runs the same sweep in-process and writes
the same store — so installing its line replaces the old `1-59/2 … obi icmp run` entry
(`obi icmp run`/`status` stay for manual use). Read the board anywhere: `obi dash show` in the
terminal, the **DASH** widget in the web workspace (60 s self-refresh; a board older than
3 minutes renders stale everywhere), or `?w=dash` as a full-screen wall display. The opt-in
pager DMs red transitions within ~a minute — one line per item, a recovery line when it clears,
mass events coalesced into a single message — and while it is enabled `obi watch run`
automatically skips its hourly ICMP pass (same store, paged an hour earlier; the pass's baseline
stays fresh so turning the pager off resumes it cleanly). Full enablement walk-through:
specs/095-status-dashboard/quickstart.md (feature 095).

Two signals are **display-only** (feature 121): **mesh** (is the mesh radio attached, how long
since each tracked node was heard, how old the newest recorded fix is) and **lighting** (each
allowlisted WLED controller's reachability and current state — reachable-but-off is green,
because a dark portal is a decision, not a fault; an unreachable one is yellow, never red, so
decorative lighting cannot make `dash_status` report the network as red). Neither can alert: they are absent from the
pager's signal list, and naming one in `[dash.pager] signals` is a config error rather than a
way to opt in. The mesh tile also never transmits — it reads the node table passively and the
position history from disk, never the locate path — and its serial read runs on
`read_interval` (5 m) rather than every cycle, because the mesh port is exclusive-access and
`obi mesh locate` needs it to answer. Each tile is omitted entirely when its subsystem is not
configured. See `specs/121-mesh-hardening/contracts/dash-signals.md`.

### Maintenance mode (feature 135)

Things that are **deliberately off** — torn-down sites after the burn, a switched-off radio
station, the portal lights — stay visible but stop alarming. Two sources are unioned into one
effective set that every alarm path honors (DASH tiles and overall state, `dash_status`, the
pager, the status band, the watcher's passes and log window, and the radio station's DMs):

1. **UISP's own flag** — mark a device *Maintenance* in the UISP controller and obi picks it
   up from the snapshot on the next cycle (cached through snapshot gaps). The flag also covers
   the router-side netwatch probe of the same link (`aspen-ptp-cedar-11` ⇒ `aspen-cedar-11`),
   but never spreads to the rest of the site.
2. **The config declaration** — for everything UISP doesn't manage (gateways, APs, netwatch,
   whole subsystems):

```toml
[maintenance]
reason     = "post-season teardown"
sites      = ["aspen", "oak"]                       # whole name tokens: aspen-gw, birch-ptp-oak-60,
                                                    # cedar-gw→cedar-oak-11 — never oakmont-gw
uisp_sites = ["Valley - Aspen", "Valley - Oak"]     # UISP outage events are keyed by SITE name
signals    = ["radio", "mesh", "lighting"]           # radio = the walkie-talkie station's DMs
# devices  = ["dogwood-gw"]                         # exact identities, for one-offs
```

A matched item is labeled `maintenance` and shown, but it is excluded from its tile's rollup,
the board's overall state, the status band and the pager. When an item that was already paged
enters maintenance, its incident is closed without a recovery message, and the closure is
audited. The watcher skips matched items at the same point its `ignore_*` lists apply.

There is **no expiry**, so obi keeps it in sight. `obi dash show`, the web DASH widget and every
`dash_status` answer carry a `MAINT:` line naming the declaration. Any entry that matches nothing
(a typo like `shorline`) is called out, and every change to the effective set is audited.

To leave maintenance, delete the entry. The next cycle re-evaluates the item normally, so
something still down pages once, as a fresh outage. No restart is needed: dash and watch re-read
config every run, and the radio station re-reads `[maintenance]` each time it would send a DM.
The section is parsed strictly, so an unknown key or signal is a config error (exit 2) rather than
a silent no-op. If the radio station can't read the section, it sends the DM anyway.

## Audit trail (`obi audit`)

obi records **everything it does** into a local, queryable audit event store — so "what did obi
do, and why?" is always answerable after the fact:

- every **LLM communication** (the full prompt and response, model, token usage) across chat,
  voice, Slack, and the watcher's judge;
- every **tool call** the model makes (name, arguments, result, duration);
- every **command against a device or vendor system** — RouterOS API operations, the SSH
  `/export` and Siklu `show` commands, SNMP gets/walks, UISP/UniFi/NetBox HTTP reads;
- every **human communication**: chat/voice/Slack turns in both directions and watch reports.

Events are grouped by **session** (a chat conversation, a Slack thread, one watch cycle, one CLI
invocation), so a whole interaction reconstructs in causal order: question → model call → tool
call → device commands → answer. Capture is on by default (constitution Principle V), strictly
local (`~/.local/state/obi/audit.db`, SQLite), fail-open (a recording problem can never break the
operation being recorded), self-pruning, and redaction-backstopped — every configured secret
(passwords, tokens, SNMP communities, API keys) is scrubbed from every payload before it is
stored. Inspecting the trail is never itself audited. The pre-042 intent log
(`audit.jsonl`) keeps working unchanged; the event store is the full-payload, queryable half.

```toml
[audit]                       # optional — capture is ON by default
# enabled = true              # set false to turn capture off
# db_path = "~/.local/state/obi/audit.db"
# retention_days = 90         # self-pruning age
# max_payload_bytes = 500000  # per payload field; beyond it: explicit truncation marker
```

```
obi audit status                       # store health: size, counts, oldest/newest
obi audit list --since 1h              # recent events (newest first)
obi audit list --kind llm_call --source watch --since 1d
obi audit list --text core-gw-1 --outcome error
obi audit show 123                     # one event's complete payload (full prompt/response…)
obi audit trace chat-1a2b3c4d5e6f      # a whole conversation, causally ordered
obi audit stats --since 7d             # volumes by kind/source, token usage, top tools/targets
obi audit list --json | jq .           # every view has --json
```

In the web terminal, the **`AUD`** board is the same trail interactively: a live filterable
event list, click-through full detail (LLM calls render as the conversation), session-trace
navigation, and an analytics view (token usage over time, volumes, top tools/targets).

## Claude Desktop & Claude Code (`obi mcp`)

obi's whole toolset — inventory, live status, metrics history, syslog, configs,
UISP/Siklu health, live ping/traceroute, the audit trail (`audit_query`, which since
feature 105 is served *only* here: the trail is operator-only disclosure, so the
conversational surfaces don't carry it), and the portal lighting tools
(`wled_status`/`wled_set`/`wled_revert`, constitution 2.0.0) — is
available inside **Claude Desktop** and **Claude Code** on the same machine, over a local
[MCP](https://modelcontextprotocol.io) stdio server. Ask Claude "which devices are
unreachable?" or "what did obi do overnight?" and it answers from obi's real data.

```bash
obi mcp install            # register with BOTH clients (idempotent; --dry-run to preview)
obi mcp serve              # the stdio server itself (clients launch this; rarely run by hand)
```

Restart Claude Desktop after the first install (it reads its config at launch); Claude Code
picks the server up on the next session (`claude mcp list` shows `obi`, user scope).

The served tool list is **derived from the one operator toolset** (constitution 1.14.0,
retargeted by 2.0.0): a tool added to obi appears in both clients with zero extra work, and a
parity test fails the suite if the surfaces ever diverge. Network devices stay read-only with
per-tool bounds — the one write capability is the allowlisted, snapshot-revertible portal
lighting — results carry the same redaction chat gets, and every call lands in the audit
trail as `source="mcp"`. The
server is stdio-only — nothing listens on a port, and nothing is reachable off this machine.
If obi's venv moves, re-run `obi mcp install` (it reports `updated`). See
`specs/101-mcp-server/quickstart.md` (feature 101) for verification
steps and troubleshooting.

## Decorative lighting (`obi wled`)

obi can drive the portal's WLED LEDs on request — its **first and only device-write**
capability (constitution 1.15.0), deliberately fenced: writes reach only devices named in the
`[wled.devices]` allowlist (network gear gains nothing), the look being overwritten is
captured before every change and restorable with `revert`, `--dry-run` previews, and every
write lands in the audit trail with its pre-change snapshot.

```toml
[wled.devices]
portal = "https://lights.example.net"            # name → base URL; the COMPLETE allowlist
# [wled] timeout = 5.0 / verify_tls = true       # optional tuning
```

```bash
obi wled status                          # read-only: power, brightness, look (incl. palette,
                                         #   speed/intensity), live-stream flag
obi wled set --color purple              # or --effect "Fire 2012" / --brightness 60 / --off
obi wled set --palette Icefire           # color palette from the device catalog (feature 107)
obi wled set --effect Fireworks --speed 30 --intensity 80   # effect settings, percent
obi wled set --color red --color2 white  # background/tertiary color slots (--color3)
obi wled set --color red --dry-run       # preview exactly what would be sent; writes nothing
obi wled revert                          # put back the look from before obi's last change
obi audit list --text wled_set           # every write, with what it overwrote
```

In chat/Slack/voice: "make the portal purple", "run a fire effect", "set the palette to ice
fire", "fireworks at half speed", "portal brightness 50%", "what's the portal doing?",
"portal revert". The same tools are served to Claude
Desktop/Code over MCP and to the **radio** (constitution 2.0.0, operator decision: ANY
caller on channel can set or revert — FM has no authentication, and the class is low-risk,
one revert from undone, audited per claimed handle). The write tools are never on the web
tool route or the autonomous watcher (standing contract guards), and obi never changes the
lights unprompted.

**Etiquette on a shared strip**: the controller is unauthenticated, last-writer-wins hardware
shared with other users (the WLED app, UDP-sync remotes, realtime streams). obi never changes
it unprompted — the ONE scoped exception is the status-band reconciler below (constitution
2.3.0) — and when a realtime show (LedFx/E1.31) is in
control it says so instead of pretending a write stuck. Revert exists precisely so anything
obi overwrote — including someone else's look — is one request away from restored.

### Status bands (feature 128)

The portal's strips are split per guy-wire leg into a small **status band** at the top and
the **main pattern area** below (the physical layout is operator-declared, never assumed):

```toml
[wled.segments.portal]          # feature 128 — declares roles; absent = uniform writes
status = [0, 2, 4]              # segment ids of the top-of-leg status bands
main   = [1, 3, 5]              # the pattern area operator looks are scoped to

[dash.status_band]              # the second enable for the autonomous reconciler
enabled = true                  # default false; enabling without a layout fails at load
```

With a layout declared, **every** `wled_set`/`wled_revert` (all surfaces — chat, Slack,
voice, radio, MCP, CLI) targets the MAIN segments only: "make the portal purple" changes
the pattern area and can never stomp — or, via revert, resurrect — a status color. The
bands themselves reflect **pageable network health**, reconciled by the per-minute dash
cycle (obi's first autonomous device write, constitution 2.3.0): solid pleasant green =
all clear, solid amber = degraded, **blinking red** = an incident someone would be paged
about (display-only tiles like mesh/lighting can never redden the tower, and a device in
UISP maintenance is ignored while down — planned work never alerts). Edge-triggered:
steady state writes nothing; a color changed out from under obi (the WLED app, another
camper) is healed within a minute, audited as `wled_status_set` / `actor="dash"`. A
lighting failure never affects the board, pager, or alerting, and the reasoning model has
no path to the bands on any surface (pinned by negative tests). Manual/field-test path:

```bash
obi wled status-band red --dry-run       # preview: blinking red on the declared status ids
obi wled status-band green               # the other sanctioned caller besides the dash
obi audit list --text wled_status_set    # every reconcile write, heal, and refusal
```

## Mesh node location (`obi mesh`)

obi can answer **"where is Tumble?"** from the Meshtastic radio attached over USB (feature
111): it resolves a node by name against the radio's mesh database and answers with a
**city address** (when `[brc]` is configured) — "6:15 & A" when the fix sits on a street whose letter is
certain, otherwise "6:15 and 2,670 feet from the Man" — plus the position's age, how the
node was last heard (direct/hops, signal), and battery. Passive-first: a fresh stored
position transmits **nothing**; a missing or stale one may send **one** position request
over the mesh, rate-limited per node and by a global hourly cap (durable across processes),
on a caller's question only — the autonomous watcher can never reach it (a contract test
pins that). The street translation reuses the feature-098 survey cache; requires the `mesh`
extra (`uv sync --extra mesh`) and a `[meshtastic]` section.

**City addresses are optional and configured.** With no `[brc]` section a position is answered
as plain coordinates. Give obi an event city plan — its survey origin, edition year and,
optionally, where the published GIS files live — and mesh answers, camp/art lookups
(`playa_locate`, which also needs `[playa]` for `obi playa fetch`) and the XR city grid all use
clock-and-distance addresses:

```toml
[brc]
origin_lat = 37.16          # the plan's survey origin (the golden spike)
origin_lon = -118.27
year = 2030                 # plan edition (also names the local cache dir)
# source_url = "https://gis.example.net/{year}/GeoJSON/{name}"   # unset ⇒ local cache only
# az12_deg = 45.0           # 12:00 axis fallback when the plan has no plazas

[playa]
base_url = "https://api.example.net"   # the event's public camp/art API
```

```toml
[meshtastic]                       # presence = the opt-in; empty section = all defaults
# port = "/dev/cu.usbmodem111301"  # optional — USB discovery finds the node without it
# snapshot_timeout_s = 10          # node-table read deadline
# request_timeout_s = 30           # position-request deadline
# fresh_within_s = 300             # stored position younger than this answers passively
# node_rate_limit_s = 60           # min spacing between requests to the same node
# hourly_tx_cap = 30               # global rolling-hour cap on mesh transmissions

[meshtastic.aliases]               # the names people actually use → node id or mesh name
"art car" = "!1234abcd"            # "where's the art car" resolves before any fuzzy match
```

**Worst-case answer time is `snapshot_timeout_s + request_timeout_s`** — the node-table read
and the position request run in series. Before feature 121 the read's deadline was a
hardcoded default that no caller passed, so tuning `request_timeout_s` alone understated the
real bound.

`fresh_within_s` defaults to **300** (feature 121, was 900). obi was answering from fixes up
to ~14 minutes old — for a moving tracker that can be a long way stale, and it is exactly
when people ask — while spending about 1 of its 30 permitted transmissions per hour. Set it
back explicitly if you prefer the old behaviour.

obi will **refuse to open a serial device belonging to the radio station**, whether it came
from USB discovery or from `port`. Opening that device asserts DTR and RTS, both of which key
the transmitter, and the mesh path has no de-key step. Discovery is filtered to Meshtastic
vendor ids rather than delegated to the vendor library, whose helper returns every
non-blocklisted serial port when it finds no mesh radio. See
`specs/121-mesh-hardening/contracts/mesh-port-safety.md`.

```bash
obi mesh locate tumble              # same answer the mesh_locate chat tool gives
obi audit list --text mesh_position_request   # every transmission (and withheld request)
```

## Demo dataset (`obi demo`)

`obi demo snapshot` turns this host's stores into a self-contained dataset of a fictional network
(**Example Valley Net**). Everything public is built from that dataset: the demo workspace, the
docs, and the videos. It does four things:

- remaps every name, address, MAC, serial and person consistently;
- removes secrets entirely;
- moves the whole network rigidly to a different valley;
- shifts time by whole weeks.

An independent leak gate refuses to publish if any real identifier survives. The command is
read-only toward devices and live stores.

```bash
uv run obi demo snapshot --dry-run --out ~/obi-demo/dataset   # preview; writes nothing
uv run obi demo snapshot --out ~/obi-demo/dataset             # stage → gate → publish
uv run obi demo gate ~/obi-demo/dataset                       # re-check any time
```

Needs a private `~/.config/obi/demo-denylist.txt` (chmod 600). Details, the `[demo]` profile and
the exit codes are in [docs/development/demo-dataset.md](docs/development/demo-dataset.md).

The **static public demo** is built from that dataset. It is the real workspace running with no
server, plus a replay of operator-approved past chats. It is hosted on GitHub Pages or any
static host:

```bash
uv run obi demo chats list --since 90d    # curate the chat preview (show / approve / revoke)
uv run obi demo export                    # render → gate → ~/obi-demo/bundle
uv run obi web serve --demo               # preview at http://127.0.0.1:8765/obi/
uv run obi demo publish --pages --vm      # only the exact gated bundle leaves the laptop
```

See [docs/development/static-demo.md](docs/development/static-demo.md).

## Develop

```bash
uv run pytest                            # unit + contract + integration (offline fixtures)
uv run ruff check . && uv run mypy src   # lint + types ("done" gate)
```

Tests run fully offline against a fake driver and recorded read-only fixtures; no test
performs a state-changing operation against a real device. To capture new fixtures from a
real device (read-only):

```bash
uv run python tests/record_fixtures.py --host <router-ip> --user <user> --name <site>-gw-01
```

## Architecture

A layered, vendor-neutral design (see plans for
001 (feature 001) and
002 (feature 002)):

- `src/obi/core/` — interface-agnostic engine: models, `scope`, `discovery`, `crawl`,
  `inventory`, `engine`, config, audit, logging. Depends only on the `VendorDriver` contract.
- `src/obi/core/drivers/` — `base.py` defines four sibling source contracts: `VendorDriver`
  (`probe`, `fetch_status`, `fetch_neighbors`) for device-addressable vendors, `InventorySource`
  (`fetch`) for controller-style vendors (live health), `DocumentationSource` (`fetch`) for
  source-of-truth vendors (documented inventory), and `LogSource` (`query`) for ingested device
  syslog. `mikrotik.py` implements the first (the only module importing `librouteros`); `uisp.py`
  and `netbox.py` implement the next two (the only modules importing `httpx`); `sqlite_logs.py`
  implements the last over the local store in `logstore.py` (the only module importing `sqlite3`).
  `siklu.py` adds the first **SSH-CLI** vendor (feature 020) — device-addressable like Mikrotik but
  with a richer per-radio payload (RF link + Ethernet ports), read over an interactive SSH shell (the
  only module importing `paramiko`); the pure `pairing.py` correlates each point-to-point hop's two
  ends.
- `src/obi/cli/` — a thin Typer/Rich CLI adapter over the core (`discover`, `status`, `siklu`,
  `netbox`, `chat`, `logs`).
- `src/obi/chat/` — the read-only conversational agent (feature 005): a Messages-API tool-use
  loop (`agent`) whose read-only tools (`tools`) wrap the core, behind an injectable LLM-client
  seam (`llm`). A second interface over the one core; cloud-dependent (constitution v1.3.0
  carve-out), with the `anthropic` SDK imported lazily so the offline core never pulls it in.

New sources are added as new adapters behind one of these contracts, with no core change:
device-addressable vendors implement `VendorDriver`; controller/NMS-style vendors implement
`InventorySource`; source-of-truth vendors implement `DocumentationSource`; a log store implements
`LogSource` (so the SQLite store could be swapped for e.g. VictoriaLogs without touching chat).
