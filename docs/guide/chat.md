# Asking obi: terminal, Slack, Claude Desktop/Code

obi answers plain-language questions about the whole network — Mikrotik, UISP, Siklu, and what is documented in NetBox — using read-only tools. The same toolset is available from the terminal (`obi chat`), from Slack DMs (`obi slack`), and inside Claude Desktop / Claude Code (`obi mcp`); for spoken questions see [voice.md](./voice.md).

## Terminal (`obi chat`)

```bash
uv run obi chat                           # interactive session
uv run obi chat "what's down right now?"  # one-shot (scriptable), spans all vendors
uv run obi chat "what UISP devices are offline?"
uv run obi chat "are any Siklu links down?"
uv run obi chat "what is core-gw-1 supposed to be?"                      # NetBox (documented)
uv run obi chat "is anything on the network not documented in NetBox?"   # documented vs discovered
uv run obi chat "show me the interface traffic on core-gw-1 over SNMP"   # live SNMP
uv run obi chat "what errors did birch-gw log in the last hour?"         # device syslog
uv run obi chat "show the ip routes on core-gw-1 for 198.51.100.0/24"    # any live RouterOS read
uv run obi chat "can core-gw-1 reach 8.8.8.8?"                           # ping/traceroute from a device
uv run obi chat "what's the wind doing tonight?"                         # weather + NWS alerts
uv run obi chat "how long is the gate wait time?"                        # gate travel time, as published
uv run obi chat --json "summarize the network" | jq .
```

Exit codes add `3` (model unreachable / no key) and `4` (no inventory — run `obi routeros discover` first).

### What it can answer from

| Source / tool | What it covers |
|---|---|
| Mikrotik inventory | Persisted discovery inventory plus live reads ([routeros.md](./routeros.md)). |
| `routeros_query`, `routeros_ping`, `routeros_traceroute` | A `print` of any RouterOS menu (`interface`, `ip/route`, `ip/firewall/filter`, wireless registrations, …) with filtering/field selection; bounded diagnostics run **from** a device. |
| `uisp_status`, `uisp_links`, `siklu_status` | Live UISP controller read (one read covers the fleet; link state, signal, scores, frequency, distance — no cached roster, topology or rescan) and Siklu radio/link health ([vendors.md](./vendors.md)). |
| NetBox | **Documented** (intended) state: sites, roles, models, status, primary IPs. Documented status is never presented as reachability. |
| `metrics_history` | Traffic **rates** (bps, average and peak) over any window, plus trends of interface errors, CPU, memory, temperature. See [metrics.md](./metrics.md). |
| `snmp_metrics` | Live spot read: present-moment port state, and devices the collector doesn't target. |
| `query_health` | Hardware health (temperature, PSU, optics, disk) from the device-health store. |
| `logs_query` | Ingested device syslog, filtered by host, severity, text and time window. See [logs.md](./logs.md). |
| `config_query` | Stored device configs. See [config-backup.md](./config-backup.md). |
| `mesh_locate` | Meshtastic node positions as Black Rock City addresses. See [mesh.md](./mesh.md). |
| `weather_forecast`, `gate_traffic` | Local weather and gate-traffic stores. See [weather-traffic.md](./weather-traffic.md). |
| `backbone_rf_health`, `weather_observed`, `victoria_query`, `victoria_discover` | An external VictoriaMetrics stack, when `[victoria]` is configured. |

Because chat sees both the discovered/live network and NetBox, it can **reconcile** them in one conversation: devices present-but-undocumented, documented-but-absent, or documented incorrectly. Whole-fleet questions consult every source and label each device.

### Behaviour notes

- **Traffic = history, not the live read.** A rate needs two readings over time; `snmp_metrics` returns cumulative counters from one moment and can never express utilization. `metrics_history` answers throughput questions.
- **Local stores work offline.** `metrics_history`, `query_health`, `weather_forecast` and `gate_traffic` read local stores and contact no device, so they answer with the network down. An empty history result is reported as *missing data*, never as zero traffic.
- **Health verdicts match alerts.** `query_health` uses the same thresholds the health watcher alerts on and the HEALTH board shows; answers stay bounded, so it works on every surface down to the radio.
- **Mesh locate** is passive-first (at most one rate-limited position request when the stored one is missing or stale), reports age and how the node was last heard, and needs a `[meshtastic]` section (empty = all-defaults opt-in).
- **Weather** answers carry the snapshot's age; **gate traffic** is served verbatim, never predicted, and pre-event says nothing is published yet.
- **VictoriaMetrics is second-tier.** It is reached read-only over HTTP. Questions about devices obi collects itself still route to `metrics_history` / `snmp_metrics` / `query_health`; the victoria tools cover what obi doesn't collect and cross-check what it does. `weather_observed` wind numbers gate tower climbs.
- **Read-only by construction.** `routeros_query` only ever issues `print`; a static denylist refuses `set`/`add`/`remove`/`reboot`/`fetch`/… before any device contact. Every result is **redacted** (passwords, keys, SNMP communities never reach the model) and replies are size-capped. Per-call RouterOS caps live under `[routeros.live]`.
- Chat **declines** any change ("obi is read-only") and audits every tool call, with your question as the intent. See [audit.md](./audit.md).
- Chat runs with **any source combination** — Mikrotik, UISP, NetBox and/or logs, including a logs-only host.

### Use by the watcher

The autonomous watcher (`obi watch`, see [watcher.md](./watcher.md)) can ground its findings in the same read-only tools: the three RouterOS live tools, `uisp_status`, `uisp_links`, `siklu_status`, `config_query`, `metrics_history`, and the four VictoriaMetrics tools — each gated by its section being configured.

- Live device/controller reads (RouterOS trio, UISP, Siklu, victoria) are capped per run by `[watch].investigation_live_call_budget` (default `8`; `0` disables live tools for the watcher).
- `config_query` and `metrics_history` read only local stores and are not budgeted.

### Cloud dependency and configuration

Chat is the one **cloud-dependent** feature: it needs network access to a reasoning model and an API key (`ANTHROPIC_API_KEY` or `[chat].api_key`). It **degrades cleanly** when offline while `discover`/`status` keep working.

```toml
# optional, in ~/.config/obi/config.toml
[chat]
# api_key = "sk-ant-..."                      # or set ANTHROPIC_API_KEY
# model = "claude-sonnet-5"                   # default
# max_tokens = 1024                           # default
# context_file = "~/.config/obi/network.md"   # optional operator network context
```

### Network context (optional)

Point `[chat].context_file` at a free-form document describing your network — site names, device-naming grammar, address planes, topology. Chat reads it once at startup to **interpret your terminology** and pick the right tools (so "the cedar gateway" resolves to `cedar-gw`).

- It is reference for interpretation only: every fact is still grounded in tool output, never answered from the document.
- Keep it under **16 KiB** and put **no credentials** in it — it is sent to the cloud model as background.
- A missing, oversized or unreadable file is skipped with a warning (chat still runs). With no `context_file`, chat is unchanged.

## Slack (`obi slack`)

`obi slack` runs a long-lived listener so authorized operators can ask the same read-only questions from Slack, with no shell access to the host. It is the same chat brain over a second interface.

```bash
uv run obi slack run    # connect to Slack and answer DMs until Ctrl-C
```

To keep it running unattended, supervise it with launchd (`obi slack start`) — see [services.md](./services.md).

- **DMs only.** obi never posts network data into a channel; an @-mention just gets a "please DM me" nudge.
- **Allowlist.** Only Slack user IDs in `[slack].allowlist` may use obi; others get a brief refusal and the attempt is audited. Every answer is audited with the asker's user ID.
- **Thread = session.** Replies in a DM thread continue one conversation; a new top-level DM starts fresh.
- **Reply to a report.** Replying under a watch report (or any thread obi has no session for, e.g. after a restart) pulls that thread's earlier messages from Slack as context. Seeded context is bounded (newest 40 messages / 16k chars); if the thread can't be read, obi says so and answers without it.
- **Outbound only.** Connects via Socket Mode — no public endpoint or port-forward. Read-only and cloud-dependent like `obi chat`; the offline core is unaffected whether or not the listener runs.

```toml
# in ~/.config/obi/config.toml (reuses [chat] for the model)
[slack]
bot_token = "xoxb-..."          # Bot User OAuth Token
app_token = "xapp-..."          # App-Level Token (Socket Mode)
allowlist = ["U0XXXXXXXXX"]     # authorized Slack user IDs
```

## Claude Desktop & Claude Code (`obi mcp`)

obi's whole toolset is available inside **Claude Desktop** and **Claude Code** on the same machine, over a local [MCP](https://modelcontextprotocol.io) stdio server. Ask Claude "which devices are unreachable?" or "what did obi do overnight?" and it answers from obi's real data.

```bash
obi mcp install   # register with BOTH clients (idempotent; --dry-run to preview)
obi mcp serve     # the stdio server itself (clients launch this; rarely run by hand)
```

- Restart Claude Desktop after the first install (it reads its config at launch). Claude Code picks the server up on the next session (`claude mcp list` shows `obi`, user scope).
- If obi's venv moves, re-run `obi mcp install` (it reports `updated`).

What it serves:

- The tool list is **derived from the one operator toolset**: a tool added to obi appears in both clients with no extra work, and a parity test fails the suite if the surfaces diverge.
- It includes the audit trail tool `audit_query`, which is served **only** here — the trail is operator-only disclosure, so the conversational surfaces don't carry it.
- It includes the portal lighting tools `wled_status` / `wled_set` / `wled_revert` (see [lighting.md](./lighting.md)). That allowlisted, snapshot-revertible lighting is the one write capability; network devices stay read-only with per-tool bounds.
- Results carry the same redaction chat gets, and every call lands in the audit trail as `source="mcp"`.
- The server is stdio-only: nothing listens on a port and nothing is reachable off this machine.
