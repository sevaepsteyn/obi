# Autonomous watcher

`obi watch` watches your network on its own instead of waiting to be asked. Each cycle reads new syslog lines, asks a cloud model what looks wrong, and runs a set of optional deterministic passes over the metrics, UISP and config stores; it writes a per-finding report and never changes device config.

## Passes at a glance

| Pass | Config section | Needs cloud model | Triggers a finding when |
| --- | --- | --- | --- |
| Log analysis | `[watch]` | yes | the model judges new syslog lines as a problem |
| Climbing errors | `[watch.metrics]` | no | an interface error counter rises past `threshold` within `window` |
| Device going dark | `[watch.reachability]` | no | a device's newest SNMP reading is older than `stale_after_cycles` × its cadence |
| Wireless links (UISP) | `[watch.uisp_links]` | no | an active link goes down, or its signal drops more than `signal_drop_db` |
| Config changes | `[watch.config_changes]` | no | a device's committed config advances to a new version |
| Device health | `[watch.health]` | no | a health sensor crosses its threshold (temperature, PSU/fan, optics, CPU/memory, disk) |

Every pass shares the same dedup → report → Slack path: one alert per ongoing problem, re-alerting after a TTL or a later recurrence. All passes ride the one `obi watch run` cron; there is nothing extra to schedule.

## Log analysis

Each cycle reads only the syslog rows ingested since the last run (see [logs.md](./logs.md)), asks a cloud model what looks wrong, optionally does a little read-only investigation, and writes a JSON + markdown report per finding under the state dir.

- It is a cloud-reasoning feature, a sibling of [`obi chat`](./chat.md). With no connectivity a cycle is a clean no-op.
- The watermark is the monotonic ingest id, so a device with a skewed clock can't blind it.
- If there are no new logs, the model is skipped entirely.
- Findings are reviewed from the CLI; set `slack_notify = true` to also DM them to the operator.

```toml
[watch]
enabled      = true
slack_notify = true             # DM findings to the operator
# routeros_only     = true      # analyze only RouterOS devices in your inventory
# ignore_topics     = []        # mute noisy log topics
# ignore_collectors = []        # drop every row from a log collector, e.g. ["unifi"]
```

`ignore_collectors` matches the event's collector case-insensitively. It covers what `ignore_topics` can't express as one rule, since one collector's rows carry many topics. Ingest is untouched, so the rows stay queryable in `obi chat` and `obi logs query`.

```bash
uv run obi watch run --since-now     # first enable: baseline the cursor forward from now
uv run obi watch run                 # one cycle (the cron target); no-op when no new logs
uv run obi watch run --dry-run       # evaluate and print, deliver nothing
uv run obi watch reports --since 1d  # recent findings, newest first
uv run obi watch reports --show <id> # one finding's full markdown report
uv run obi watch status              # last-run health + token usage (last run / 24h)
```

Schedule it hourly on the same host as the log store, a few minutes after the collectors:

```cron
4 * * * *  obi watch run >> ~/.local/state/obi/watch/cron.log 2>&1
```

`obi watch run` exits `0` even on handled degradations (store locked, cloud down). They are recorded, not fatal, so cron stays quiet.

## Watching metrics for climbing errors

When an interface's error counter climbs, a link is usually going bad. This pass is a deterministic rule over the metrics store (see [metrics.md](./metrics.md)), read-only, and runs offline.

```toml
[watch.metrics]                  # presence + enabled opts in
enabled   = true
window    = "1h"                 # look-back each cycle
threshold = 0                    # alert on ANY new error; raise it if a link trickles benignly
# counters          = ["ifInErrors", "ifOutErrors"]  # add "ifInDiscards", "ifOutDiscards" to widen
# ignore_interfaces = ["wireguard"]                  # interface-name substrings to skip; [] = all
```

- WireGuard tunnels are skipped by default: their out-errors are usually benign no-route-to-peer drops, not a failing link.
- A finding reads like `cedar-gw sfp-sfpplus5: +1,432 ifInErrors in the last 1h`.
- One alert per interface+counter until a clean window clears it. A counter reset (reboot) is never mistaken for a change.

## Watching for a device going dark

This pass catches a device that stops answering SNMP and drops out of the metrics store. It is deterministic, offline and read-only over `metrics.db`.

```toml
[watch.reachability]             # presence + enabled opts in
enabled            = true
stale_after_cycles = 2           # 2 consecutive misses (~10m at a 5m cadence) => alert
# mass_outage_ratio = 0.8        # >= 80% of watched devices silent at once => ONE finding
# ignore_devices    = []         # device identities never to flag (e.g. a known-off gateway)
```

- Staleness is measured against each device's own observed collection cadence. A single missed cycle is always tolerated.
- A finding reads like `core-gw-1: no SNMP metrics for ~11m (2 missed cycles); last reading 15:41`.
- One alert per device; recovery clears silently, and a later outage re-alerts.
- If the collector or host itself stops, the silent devices coalesce into one finding such as `Metrics collection appears to have stopped: 11/12 devices silent` instead of a per-gateway storm.

## Watching wireless links (UISP)

This pass reads the UISP controller's link snapshot (two read-only GETs) and compares it with the previous observation, kept in `uisp-links-baseline.json` in the watch dir. It requires `[uisp]` (validated at load) and LAN reach to the controller; see [vendors.md](./vendors.md).

```toml
[watch.uisp_links]               # presence + enabled opts in
enabled = true
# signal_drop_db = 6.0           # alert when signal falls more than this many dB below the held baseline
# ignore_links   = []            # substrings matched against either endpoint name or the link label
```

- A link that was active and no longer is raises a *link down* finding. A signal drop raises a *degradation* finding, e.g. `birch-gw ↔ birch-ptp-60: link signal fell 9.0 dB (−62 → −71 dBm)`.
- While degraded, the baseline is held, so an ongoing sag alerts once and re-arms only after recovery.
- One alert per link per condition. A link that disappears from the controller is dropped silently (decommission, not an outage).
- The first cycle seeds the baseline silently. An unreachable controller degrades the run (`uisp_links_error`) without touching the baseline or the other passes.

To silence a whole out-of-scope site or device across both UISP passes (this one and the `obi uisp logs` log/outage findings), set `[uisp] ignore_names` (see [vendors.md](./vendors.md)). It is unioned with `ignore_links`, also hides the entity from CLI/chat listings, and never affects log ingest.

## Watching config changes

This pass notices when a device's stored config changes and DMs you the redacted diff. It reads the config-backup git store read-only (see [config-backup.md](./config-backup.md)) and compares each device's current committed version against a per-device baseline in `config-baseline.json` in the watch dir.

```toml
[watch.config_changes]           # presence + enabled opts in
enabled = true
# severity       = "warning"     # info | warning | critical (default crosses the Slack floor)
# max_diff_lines = 120           # bound the diff in an alert; the rest is one `obi configs diff` away
# ignore_devices = []            # case-insensitive substrings of the device identity
# repo_path      = "…"           # default = the config-backup store
```

- It never fetches configs itself (the `obi configs ingest` cron does) and is fully offline.
- A finding reads like `core-gw-1: config changed (+3/−1 lines)` with the diff inline. One alert per committed change; a later change re-alerts.
- The first cycle after enabling seeds the baseline silently, so pre-existing history never floods you. A device new to the store is baselined, not alerted; a removed one is dropped silently.
- The diff is the already-redacted stored content (secrets are scrubbed before commit), so an alert never carries a cleartext secret.

## Watching device health

This pass alerts on hardware health: overheating, a PSU or fan status leaving its healthy value, SFP optics rx power under the floor (or rx-loss/tx-fault asserting), sustained CPU/memory pressure, and a collector VM filesystem filling up. It reads the metrics store's health series (the `health`/`optics` SNMP profiles plus `obi health probe` Linux collector readings) read-only against fixed thresholds.

```toml
[watch.health]                   # presence + enabled opts in
enabled = true
# temperature_max_c     = 75.0   # board/CPU/switch sensors
# sfp_temperature_max_c = 70.0   # optics + device sfp-temperature sensors
# cpu_max_percent       = 90.0   # sustained (see sustained_samples)
# memory_max_percent    = 90.0   # sustained
# sustained_samples     = 3      # consecutive readings CPU/memory must breach
# disk_max_percent      = 85.0   # Linux collector filesystems
# rx_power_min_dbm      = -24.0  # optics floor (the platform exposes no vendor DDM levels)
# status_healthy_value  = 0      # what a healthy *-state sensor reads
# severity              = "warning"
# mass_outage_ratio     = 0.80   # tracked-series breach fraction that coalesces into one finding
# ignore_devices        = []     # case-insensitive identity substrings
# ignore_sensors        = []     # case-insensitive sensor-name/metric substrings
# db_path               = "…"    # default = the metrics store
```

- Alerts are transition-only against a per-series baseline in `health-baseline.json` in the watch dir. The first cycle seeds silently, so a chronically unhealthy sensor never alerts until it recovers and breaks again.
- Recovery clears silently; a re-breach re-alerts.
- A device with no fresh sample is skipped; staleness belongs to the device-going-dark pass.
- CPU/memory need `sustained_samples` consecutive hot readings, so a one-cycle spike never pages.
- Many series breaching in one cycle (e.g. a site heat event) coalesce into a single finding.
- A finding reads like `birch-gw: board-temperature2 at 81 °C (limit > 75 °C)`.

`obi health status` shows the same thresholds' verdicts, worst first. The CLI and the pass share one evaluation, so they cannot disagree.
