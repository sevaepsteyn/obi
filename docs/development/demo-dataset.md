# Demo dataset (`obi demo`)

`obi demo snapshot` turns this host's own stores into a self-contained dataset of a fictional
ISP, **Example Valley Net**, on `example.net`. The demo web workspace (feature 135), the static
GitHub Pages demo (feature 136), the docs and the demo videos are all built from that dataset,
never from live data.

The command is read-only toward every device and every live store. It writes only the dataset,
one audit record, and the private mapping.

## Quick start

```bash
# 1. Private denylist — one term per line, never committed.
#    "term" = warning, "!term" = fatal (people, handles).
$EDITOR ~/.config/obi/demo-denylist.txt && chmod 600 ~/.config/obi/demo-denylist.txt

# 2. Preview: window, sources, estimated size. Writes nothing.
uv run obi demo snapshot --dry-run --out ~/obi-demo/dataset

# 3. Produce it (staged → gated → renamed into place).
uv run obi demo snapshot --out ~/obi-demo/dataset

# 4. Re-check before publishing anything built from it.
uv run obi demo gate ~/obi-demo/dataset

# 5. Trace a demo value back to reality (local only).
uv run obi demo lookup birch-gw            # fake → real
uv run obi demo lookup <real-word> --reverse  # real → fake
```

## What happens to the data

| Real | In the dataset |
|---|---|
| Site and place words (`northfield`, `Southgate`) | Tree words (`birch`, `Cedar`). Role tokens (`gw`, `ap`, `core`, `internet`), digits and separators are kept, so `northfield-gw-01` becomes `birch-gw-01` and the UISP site `Northfield` becomes `Birch`. |
| Private IPv4 | Each real `/16` gets its own `/16` in `172.16.0.0/12`. The third octet is XORed with a seeded mask, which keeps every `/17`–`/24` subnet intact. The host octet is kept. |
| CGNAT `100.64/10` (overlay or carrier) | Mapped host by host into `172.31.0.0/16`. |
| Public IPv4 / IPv6 | The `192.0.2/24`, `198.51.100/24` and `203.0.113/24` documentation ranges, then `198.18/15`. IPv6 goes to `2001:db8::/32`. Link-local, loopback, multicast and netmasks are left unchanged. |
| MACs, serials | Locally administered `02:…` MACs (each keeps its original spelling style) and 12-hex pseudo-serials. |
| People, Slack IDs, mesh nodes, SSIDs | `admin` / `user-N`, `node-NN`, `evn-net-NN`. Every label is built from generic tokens, so it can never collide with a real word. |
| Secrets, emails, phones | `<redacted-by-obi>`, `<email>`, `<phone>`. These are never pseudonymized. |
| Coordinates | The whole network is rotated as one rigid shape to Owens Valley, CA, so every distance and relative bearing is exact. Ground elevation, terrain, Fresnel verdicts, hillshade and peaks are recomputed there. |
| Time | Shifted by whole weeks into the week of `[demo] epoch_start` (default 2025-06-02). Weekday, local time of day and every duration are preserved. |
| Event vocabulary | Generic wording ("playa" becomes "valley floor", "Esplanade" becomes "main road"). Any other denylist term becomes "landmark". |

Ordinary English words that are also real names (`office`, `hotel`, `mountain`) are only
rewritten where they act as an identifier: hostname-shaped tokens, `host=`/`name=` values and
name fields. Ordinary prose like "the back office printer" is left alone.

**Thinned for size:**
- Metrics are decimated per series.
- Routine successful `device_command` audit rows are sampled; every error is kept.
- The logs' never-served `raw_line` ships empty.

Each is counted in `excluded_records`.

**Excluded entirely.** The manifest lists each of these as excluded, with the reason:
- radio and chat transcripts, and every conversational audit row
- gate traffic, camp and art listings, and the city grid
- paging rosters and watcher reports
- lighting snapshots
- daemon logs and host state
- TLS keys

## The leak gate

The gate is independent of the rewriter: it compiles its own matchers from the harvested real
identifiers. It scans every byte of the staged dataset:
- every SQLite cell, plus each FTS vocabulary
- every JSON key and value
- every object in every git repository, reachable or not
- every other file

Any **FATAL** finding stops publication. The run exits with code 3 and removes the staging
directory. Every check below is fatal unless marked otherwise:
- **Real identifiers:** any harvested place, person, serial, secret or node.
- **Stray addresses:** any IP outside the fictional ranges, or any globally administered MAC.
- **Location:** any coordinate within 200 km of the real network.
- **Credentials and contacts:** any secret-shaped token, or any email outside `example.*`.
- **Near-misses:** a word within one edit of a distinctive real name.
- **Dictionary-word names:** fatal in an identifier context (`depot-gw`), a warning in prose.
- **Event terms:** **warnings** by default. They become fatal per term with a `!` prefix in the
  denylist, or for every term with `--strict-event-terms`. The operator decided on 2026-10-14 that
  it is acceptable for the source event to be recognisable.

The report shows the category, check, file, locator and offset, and never the token itself, so
it is safe to paste anywhere.

**If the gate fails:** add a mapping rule, plus a regression case in
`tests/integration/test_demo_rewriter_coverage.py`. Never weaken the gate.

## Private files: never commit them, never publish them

| File | What it is |
|---|---|
| `~/.config/obi/demo-denylist.txt` (0600) | Event terms and people. A public copy would itself reveal the source. |
| `$XDG_STATE_HOME/obi/demo/mapping.json` (0600) | The full real→fake table, plus the seed, centroid, anchor, rotation and time shift. Later snapshots reuse it so the fictional names stay stable. |
| `$XDG_CACHE_HOME/obi/demo-terrain/` | Elevation, tile and summit caches **for the destination only**. It is never obi's live web directory. |

## The `[demo]` profile (`config.toml`)

All keys are optional:

```toml
[demo]
output_dir = "~/obi-demo/dataset"
# since = "2026-08-25T00:00:00-07:00"   # default: the busiest 48 h by log volume
# until = "2026-08-28T00:00:00-07:00"
metrics_decimate = 6          # keep every Nth sample per series (30-minute grid)
audit_command_sample = 10     # keep 1 in N routine device_command rows per (surface, target)
max_dataset_mb = 200          # over budget = exit 4 unless --allow-oversize
audit_payload_max_bytes = 4096
destination_lat = 37.16       # PRIVATE: never written to the dataset
destination_lon = -118.27
rotation_deg = 0.0            # tune so the links stay Fresnel-clear at the destination
epoch_start = "2025-06-02"    # a Monday, same DST period as the source window
```

## Output layout

The dataset is laid out the way obi's own readers expect, so feature 135 runs it with
`XDG_STATE_HOME=<dataset>/state --config <dataset>/config.toml`:

```text
<dataset>/
  manifest.json      # public: sources, counts, exclusions, target window, gate summary
  config.toml        # fictional; every credential is "demo"
  state/obi/...      # the same relative paths as ~/.local/state/obi
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Published, or a dry run completed. |
| 2 | Profile or config error: denylist missing or readable by others, an output path that already exists, or a real address inside a target pool. |
| 3 | The leak gate found something. |
| 4 | Over the size budget. |
| 5 | A source could not be read, or the destination terrain could not be fetched. |

## Next: the static public demo

The bundle built from this dataset, the chat-preview curation and publishing are covered in
[static-demo.md](static-demo.md).
