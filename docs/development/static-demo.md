# Static public demo (`obi demo export | chats | publish`)

The public demo is the real web workspace running with **no server**. Every board reads
pre-rendered JSON from a folder of static files, so the demo can be hosted on GitHub Pages or
any web server, and the laptop is never needed at view time. When a visitor opens the chat
window, a **recorded** real conversation plays back through the real chat UI.

It is built from the sanitized 134 dataset ([demo-dataset.md](demo-dataset.md)), never from
live stores. The only live input is the operator's own past chats, and those enter only after
the operator approves each one.

## Quick start

```bash
# 0. The dataset (feature 134), if not already built.
uv run obi demo snapshot --out ~/obi-demo/dataset

# 1. Curate the chat preview (optional — the demo works without recordings).
uv run obi demo chats list --since 90d          # your own conversations, rewritten
uv run obi demo chats show rec-1a2b3c4d          # exactly what would ship
uv run obi demo chats approve rec-1a2b3c4d       # gate + entity check; pins its hash

# 2. Build the bundle (render → slices → recordings → assets → budget → gate → rename).
uv run obi demo export --out ~/obi-demo/bundle

# 3. Preview it exactly as a static host serves it.
uv run obi web serve --demo                      # http://127.0.0.1:8765/obi/

# 4. Publish (refuses unless the bundle is byte-identical to the last gated build).
uv run obi demo publish --pages --vm --dry-run
uv run obi demo publish --vm
uv run obi demo publish --pages                  # once the public repository exists
```

## Curating the chat preview

`obi demo chats list` offers only the **operator's own** conversations from `audit.db`:

- `chat` (terminal) and `voice` (web chat) sessions;
- `slack` sessions whose peer is in `[slack] allowlist`.

These are **never offered**: other people's Slack chats, radio calls, MCP sessions, the
watcher, and CLI commands.

Each candidate is rewritten through 134's private mapping, the same mapping the dataset used, so
device names and addresses match the boards. The rewrite clips tool results to
`tool_result_clip_bytes` and drops every system prompt. It then goes through two checks:

| Status | Meaning |
|---|---|
| `new` | Clean. It can be approved. |
| `refused` | The leak gate found a real identifier. The reason names only the category (`ip/ip`, `host/…`), never the token. Fix the rewriter or harvest; never weaken the gate. |
| `needs_review` | The recording names a device or address that is not in the dataset, so it would contradict the boards. |
| `approved` | Approved, and the fresh rewrite hashes the same. |
| `stale` | Approved, but the rewrite changed since (new mapping, new code). Re-`show` and re-`approve`. |

Approvals live in `$XDG_STATE_HOME/obi/demo/chat-approvals.json` (mode 0600; a group-readable
file is refused). They are keyed by session and pinned to the sha256 of the rewritten recording.
`obi demo export` ships only `approved` recordings and lists the others as left out. Every approve
and revoke is audited with the fictional `rec_id` only.

**Read every recording before approving it.** The gate catches identifiers. It cannot judge
whether a conversation is something you want public.

## What the bundle is

```text
bundle/
  index.html           # the live page; its boot line swapped for demo/boot.js
  *.js, widgets/, …    # byte-identical copies of src/obi/web/static
  demo/                # boot.js, api.js, clock.js, replay.js (demo-only; never served live)
  data/manifest.json   # dataset, demo_now, routes, slices, recordings, shipped snapshots
  data/api/<route>/<key>.json   # pre-rendered /api answers (key = sha1 of the canonical query)
  data/logs-slice.json, data/audit-slice.json
  data/snapshots/      # the cron snapshot files the boards read
  data/chat/           # index.json + one rec-….json per approved recording
  .nojekyll
```

- **Rendering.** Each `/api/*` answer is rendered by calling the live `web/data_api.py` handler
  in-process, so the demo shows exactly what the live server would.
- **Frozen clock.** Everything is rendered "as of" the dataset's window end (`demo_now`). In the
  browser, `clock.js` makes `Date` agree, so "5 min ago" stays true forever.
- **Slices.** LOGS and AUD search a capped slice client-side (`logs_slice_rows`,
  `audit_slice_rows`, the most recent rows). Their filter logic is parity-tested against the
  Python handlers.
- **Unavailable boards.** Live-device routes (PHY, ping/traceroute, live router reads, the chat
  backend, voice) answer a visible "not part of the static demo" state. The router window shows a
  snapshot built from the sanitized config export.
- **Sub-path.** Any root-absolute URL is re-anchored to the bundle root, so the bundle works under
  a GitHub Pages project path (`/obi/`) or any other prefix.

Before the bundle is renamed into place, the whole thing is re-scanned by the 134 leak gate. Its
tree digest is then recorded privately at `$XDG_STATE_HOME/obi/demo/bundle-digest.json`.

## Configuration

All optional, in `config.toml`:

```toml
[demo.export]
dataset = "~/obi-demo/dataset"     # default: [demo] output_dir
bundle_dir = "~/obi-demo/bundle"   # must not exist; never inside state/config/dataset
base_path = "/obi/"
logs_slice_rows = 20000
audit_slice_rows = 5000
tool_result_clip_bytes = 2048
max_bundle_mb = 500                # GitHub Pages: 1 GB site; no file may exceed 95 MB

[demo.publish]
pages_remote = "git@github.com:<you>/obi.git"   # the PUBLIC repository
pages_branch = "gh-pages"
vm_target = "you@vm.example:/var/www/obi-demo/"
```

## Publishing

`obi demo publish` first recomputes the bundle's tree digest. Unless it equals the last gated
build's digest, publishing refuses with exit 3 before anything runs. Any edit to the bundle
after export, even one byte, means re-export.

- **`--pages`** builds a **single-commit orphan** `pages_branch` from the bundle and force-pushes
  only that ref. The source repository's history never touches it. Set the repository's Pages
  source to that branch, root folder.
- **`--vm`** runs `rsync -a --delete <bundle>/ <vm_target>`. Serve it as plain static files.
- **`--dry-run`** prints the file count, size and destinations, and sends nothing.

Each target writes one audit record.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done. |
| 2 | Configuration: missing dataset, output already exists, `pages_remote`/`vm_target` not set, a recording id that is not found, or a push/rsync failure. |
| 3 | The leak gate found something, a recording was refused, or the bundle changed since the gate. |
| 4 | Over `max_bundle_mb` (or a file over 95 MB) without `--allow-oversize`. |
| 5 | A dataset source could not be read. |

## Troubleshooting

- **A board shows "not part of the static demo".** It is a live-device route, which is
  intentional. Any other board in that state is missing from the render. Check the manifest's
  `routes` counts.
- **The preview works but Pages shows 404s.** The repository's Pages source must be the
  `gh-pages` branch at root. The bundle contains `.nojekyll`, so files under `_`-prefixed
  paths are served.
- **The chat window plays nothing** (and typing answers with the demo notice). No recording was
  approved, or every approved one is `stale` and was left out. The export summary lists them;
  run `obi demo chats list`.
- **A recording is `needs_review`.** The conversation mentions a device that is not in the
  dataset's window. Pick a conversation from inside the dataset window, or re-snapshot a wider
  window.
- **Headless screenshots of `?w=<canvas widget>` are blank.** This is a pre-existing headless
  Chrome limitation, and the live server does the same. Check in a real browser.
