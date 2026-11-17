# Other vendors: UISP, Siklu, NetBox

Beyond the Mikrotik roster, obi reads health from a UISP controller, from Siklu EtherHaul radios, and documented inventory from NetBox. Each is optional and independent: run any one alone or alongside the others.

All three share the same failure behaviour: an unreachable, slow, or credential-rejecting source is reported as a clean, guided result (no crash, no hang) and obi still exits `0`. Tokens and passwords never appear in output, the audit trail, or logs.

## UISP

obi reports health for devices managed by a **UISP controller** (Ubiquiti's ISP platform). `obi status` queries it **live**, pulling the whole managed fleet in one read-only call.

- UISP devices are not persisted, and are separate from the Mikrotik roster.
- `obi routeros discover` stays Mikrotik-only.

```toml
[uisp]
base_url     = "https://uisp.example.com"  # no trailing slash; obi appends /nms/api/v2.1
token        = "YOUR_UISP_API_TOKEN"       # Settings → Users → API Token; sent only in the request header
verify_tls   = true                        # set false for a self-signed controller
timeout      = 15                          # seconds; one call covers the whole fleet
name_filter  = "ptp"                       # optional include filter; --filter overrides
ignore_names = ["lab"]                     # optional exclude list (see below)
```

```bash
uv run obi status                  # UISP devices, vendor-labeled (offline ones still listed)
uv run obi status --filter ptp     # filter by device name (case-insensitive)
uv run obi status --json | jq .    # uisp-status-output schema
uv run obi uisp status             # only the UISP section (same flags)
```

### Ignoring out-of-scope entities (`ignore_names`)

A controller often reports sites and devices you don't manage. `ignore_names` is a case-insensitive substring list (a single string or a list; empty or absent excludes nothing) that marks matching UISP entities as out of scope for alerting and listing.

- Dropped from: `obi uisp links`, `obi uisp status`, the UISP section of `obi status`, the `uisp_links` chat tool, and the `obi watch` UISP passes (log/outage findings and link findings).
- Order: `name_filter` narrows first, then `ignore_names` removes matches. A name matching both is excluded.
- It **never gates ingest**: `obi uisp logs ingest` still collects every event and outage, so ignored rows stay queryable (`obi logs query --source uisp --contains lab`).
- It is UISP-scoped: a RouterOS or UniFi host sharing the substring is unaffected.

### Wireless link health (`obi uisp links`)

One row per wireless data-link: both endpoint radios and sites, state, link signal, SSID, frequency and distance. Each end is joined with its radio quality from the device list (link score, per-radio signal, airtime, theoretical capacity).

- Down links render in red; an end with link score below 0.5 is flagged as quality-degraded.
- Read-only (two GETs), never persisted.

```bash
uv run obi uisp links                  # every wireless link, RF health joined per end
uv run obi uisp links --filter aspen   # either-endpoint name filter (case-insensitive)
uv run obi uisp links --json | jq .counts
```

Chat and voice get the same view through the `uisp_links` tool (*"any weak links right now?"*). The watcher can consult it while investigating and alert on link changes; see [watcher.md](./watcher.md).

## Siklu

obi reports health for **Siklu EtherHaul** mmWave point-to-point radios (E-band backhaul hops). Siklu has no controller, so obi reads each radio **live over SSH**, using read-only `show` commands, from an operator-maintained roster.

- No auto-discovery: these L2 radios don't appear in the Mikrotik crawl.
- Separate from the Mikrotik roster and the UISP fleet.

```toml
[siklu]
hosts = [
  "aspen-ptp-birch-70.net.example.net",
  "birch-ptp-aspen-70.net.example.net",
]
username = "obi-readonly"    # a Tech-level (read) account
password = "your-password"   # plaintext; protect the 0600 config file
timeout  = 10                # per-radio SSH timeout (s); radios are queried concurrently
# pairs  = [["odd-name-a", "odd-name-b"]]  # only for radios not named <site>-ptp-<far>-<band>
```

```bash
uv run obi siklu status                   # per radio: identity, model, firmware, uptime, temp,
                                          #   RF link (up/down, RSSI, CINR, modulation, TX power,
                                          #   capacity), Ethernet-port state
uv run obi siklu links                    # each point-to-point hop as ONE logical link
uv run obi siklu status --filter aspen    # filter by radio name (case-insensitive)
uv run obi siklu status --json | jq .     # siklu-status-output schema
```

Siklu radios also get their own section in `obi status` and can be queried from `obi chat` (*"are any Siklu links down?"*).

Link pairing matches the two ends of each hop by the naming convention (or `pairs`) and flags only **hard** disagreement: one end up while the other is down, or an end unreachable. Per-direction modulation and capacity differences are normal for an adaptive E-band link and are not flagged.

The password is used only to open the SSH session.

## NetBox

obi reads your **documented** inventory from NetBox (DCIM): sites, devices, roles, models, documented status, and primary IPs. This is the network's *intended* state, not live health; use `obi status` for live device status.

`obi netbox inventory` reads live and read-only (GET only), following pagination to return the complete inventory.

```toml
[netbox]
base_url    = "http://netbox.example.com:8000"  # no trailing slash; obi appends /api/dcim/devices/
token       = "YOUR_NETBOX_API_TOKEN"            # Profile → API Tokens; sent only in the request header
verify_tls  = true                               # only applies to https:// instances
timeout     = 15                                 # seconds; one read covers the inventory (pages followed)
name_filter = "core"                             # optional default filter; --filter overrides
```

```bash
uv run obi netbox inventory                # documented devices (name, site, role, model, status, IP)
uv run obi netbox inventory --filter core  # filter by device name (case-insensitive)
uv run obi netbox inventory --json | jq .  # netbox-output schema
```
