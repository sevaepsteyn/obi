# Development and architecture

How to run obi's test and lint gates, capture fixtures, and where things live in the layered, vendor-neutral source tree.

## Develop

```bash
uv run pytest                            # unit + contract + integration (offline fixtures)
uv run pytest -m 'not slow'              # inner loop: skip the demo snapshot/export pipeline
uv run pytest -n0 tests/unit/test_x.py   # serial (debugging, pdb, print output)
uv run ruff check . && uv run mypy src   # lint + types ("done" gate)
```

The suite runs in parallel by default (pytest-xdist, `-n auto --dist worksteal` in
`pyproject.toml`), so tests must not share fixed paths, ports, or the system temp dir: use
`tmp_path` and port 0. Tests that take seconds — anything that runs the demo snapshot/export
pipeline or drives a headless browser — carry `pytest.mark.slow`. The demo harness's
`build_dataset()` snapshots once per worker and hands each caller a private copy; use it
instead of calling `run_snapshot` directly unless the snapshot itself is under test.

Tests run fully offline against a fake driver and recorded read-only fixtures. No test performs a state-changing operation against a real device.

To capture new fixtures from a real device (read-only):

```bash
uv run python tests/record_fixtures.py --host <router-ip> --user <user> --name <site>-gw-01
```

## Layout

- **`src/obi/core/`** — interface-agnostic engine: models, `scope`, `discovery`, `crawl`, `inventory`, `engine`, config, audit, logging. Depends only on the `VendorDriver` contract.
- **`src/obi/core/drivers/`** — source contracts in `base.py` and their adapters (table below).
- **`src/obi/cli/`** — a thin Typer/Rich CLI adapter over the core (`discover`, `status`, `siklu`, `netbox`, `chat`, `logs`, …).
- **`src/obi/chat/`** — the read-only conversational agent: a Messages-API tool-use loop (`agent`) whose read-only tools (`tools`) wrap the core, behind an injectable LLM-client seam (`llm`). It is a second interface over the same core and needs the cloud; the `anthropic` SDK is imported lazily so the offline core never pulls it in.

## Source contracts

`base.py` defines four sibling contracts. Each client library is confined to its adapter module(s).

| Contract | Methods | For | Implementations |
|---|---|---|---|
| `VendorDriver` | `probe`, `fetch_status`, `fetch_neighbors` | device-addressable vendors | `mikrotik.py` (only importer of `librouteros`); `siklu.py` (SSH CLI over an interactive shell, only importer of `paramiko`) |
| `InventorySource` | `fetch` | controller-style vendors (live health) | `uisp.py` (with `netbox.py`, the only importers of `httpx`) |
| `DocumentationSource` | `fetch` | source-of-truth vendors (documented inventory) | `netbox.py` |
| `LogSource` | `query` | ingested device syslog | `sqlite_logs.py` over `logstore.py` (only importer of `sqlite3`) |

- `siklu.py` returns a richer per-radio payload (RF link + Ethernet ports). The pure `pairing.py` correlates each point-to-point hop's two ends.

## Adding a source

New sources are added as adapters behind one of these contracts, with no core change:

- device-addressable vendor → `VendorDriver`
- controller/NMS-style vendor → `InventorySource`
- source-of-truth vendor → `DocumentationSource`
- log store → `LogSource` (so the SQLite store could be swapped for, e.g., VictoriaLogs without touching chat)

Vendor details are in [../guide/vendors.md](../guide/vendors.md).
