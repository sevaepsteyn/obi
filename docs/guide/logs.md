# Logs: syslog, UniFi and UISP

obi keeps device syslog, UniFi controller events and UISP controller logs in one local SQLite store and answers questions over it with `obi logs query` or the `logs_query` chat tool. Everything is read-only toward the devices and controllers.

## Device syslog

obi reads the RouterOS log lines your existing `syslog-ng` collectors keep; it doesn't change the collectors.

1. A scheduled **rsync** (an ops task) mirrors the collectors' per-host daily files locally.
2. `obi logs ingest` parses the mirror into the store: incremental, idempotent, and deduped across collectors.
3. `obi logs query` (or `logs_query` in chat) reads it.

```toml
[logs]
mirror_root = "~/.local/state/obi/logs"     # where rsync lands; obi only reads it
db_path     = "~/.local/state/obi/logs.db"  # the store obi ingests and queries
log_tz      = "UTC"                         # the collectors' RouterOS timezone
```

```bash
uv run obi logs ingest                                   # parse the mirror → local store
uv run obi logs query --host birch-gw --severity error --since 1h
uv run obi logs query --contains "login failure" --since 2026-06-03 --until 2026-06-04
uv run obi logs query --source syslog --since 1h         # device syslog only
uv run obi logs query --host core-gw --json | jq .
```

### Query filters

All filters are optional. Results are newest-first and bounded.

| Flag | Meaning |
|---|---|
| `--host` | Host substring |
| `--severity` | Severity level |
| `--contains` | Full-text match |
| `--since` / `--until` | Relative lookback (`1h`, `2d`) or ISO date/time |
| `--source` | A family (`syslog`, `unifi`, `uisp`) or a collector hostname/substring; the latter still disambiguates a UniFi site from a like-named RouterOS host |
| `--limit` | Maximum rows |

- A missing or unreadable store gives a clean, guided result (no crash).
- Every query writes one audit record (question, filters, match count), never the log text itself. See [audit.md](./audit.md).

## UniFi controller events

UniFi's cloud-hosted console doesn't ship logs to `syslog-ng`, and its official API key exposes inventory only. `obi unifi logs ingest` instead **pulls** the controller's event log into the `[logs]` store through the reverse-engineered controller API, using a console-**local** admin account (read-only).

- Ingested events answer the same `obi logs query` / `logs_query` questions as syslog and are judged by `obi watch` ([watcher.md](./watcher.md)).
- Client connect/disconnect/roam churn (`CLIENT_DEVICES`) is excluded by default.
- The local store keeps history long after the controller's short retention ages it out.

UniFi config has three sections: `[unifi]` (coordinates common to the console), `[unifi-network]` (official API-key inventory), and `[unifi-internal]` (the local-admin credentials used here). An existing `[unifi-network]`-only config keeps working unchanged.

```toml
[unifi]
base_url = "https://<console-id>.unifi-hosting.ui.com"
sites    = ["Aspen", "Birch"]   # omit for all sites

[unifi-internal]
username = "<local-admin>"      # a Restrict-to-local-access admin (MFA-exempt)
password = "<password>"
```

```bash
uv run obi unifi logs ingest                    # poll each site → [logs] store (first run backfills 7d)
uv run obi unifi logs status                    # per-site ingest watermark
uv run obi logs query --host Aspen --since 1d   # query UniFi events like any other log
```

Schedule `obi unifi logs ingest` on the same `*/5` cron as the syslog rsync. A transient unreachable or auth failure is recorded and never crashes the cycle; the next run resumes from the last watermark.

## UISP controller logs

The UISP controller ([vendors.md](./vendors.md#uisp)) keeps an event log and device-outage history, but only for about a month. `obi uisp logs ingest` **pulls** both into the `[logs]` store over the read-only REST API, reusing the `[uisp]` token.

- Sources: the event log (`GET /logs`) and device up/down records (`GET /outages`).
- Ingested rows answer the same `obi logs query` / `logs_query` questions and are judged by `obi watch`.
- An outage **onset** is stored at `warning` (so the watcher alerts) and its **clearance** at `info`, so down duration is recoverable.
- History outlives the controller's ~1-month retention.

Ingestion is opt-in via flags on the existing `[uisp]` section; a status-only config keeps working unchanged.

```toml
[uisp]
base_url = "https://uisp.example.com"
token    = "<api-token>"          # same token as UISP status
ingest_events      = true         # GET /logs
ingest_outages     = true         # GET /outages
first_run_lookback = "30d"        # events backfill + outages per-cycle rescan window
```

```bash
uv run obi uisp logs ingest                      # poll the controller → [logs] store (first run backfills)
uv run obi uisp logs status                      # per-source (logs / outages) ingest watermark
uv run obi logs query --source uisp --since 1d   # query UISP events + outages
```

Schedule `obi uisp logs ingest` on cron next to the other ingest jobs.

- Events are pulled incrementally from a watermark.
- Outages are re-scanned over the retained window each cycle and deduped, so a long-open outage still clears without an incremental cursor.
- A transient controller failure is recorded, advances nothing, and never crashes the cycle.
