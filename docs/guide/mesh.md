# Mesh node location (`obi mesh`)

obi answers "where is the art car?" using a Meshtastic radio on USB. It finds the node by name in the radio's mesh database and replies with an address, the fix's age, how it was last heard (direct/hops, signal) and its battery.

With `[brc]` configured the address reads "6:15 & A" when the street letter is certain, else "6:15 and 2,670 feet from the Man". Without it, plain coordinates.

## Transmission rules

- A fresh stored position transmits **nothing**.
- A missing or stale one may send **one** position request, only on a caller's question.
- Requests are rate-limited per node and by a global hourly cap that persists across processes.
- The autonomous watcher can never trigger a request.

## Setup

```bash
uv sync --extra mesh
```

```toml
[meshtastic]                    # presence = opt-in; empty section = all defaults
# port = "/dev/cu.usbmodemXXXX" # optional; USB discovery finds the node
# snapshot_timeout_s = 10       # node-table read deadline
# request_timeout_s = 30        # position-request deadline
# fresh_within_s = 300          # younger stored positions answer passively
# node_rate_limit_s = 60        # min spacing between requests to one node
# hourly_tx_cap = 30            # global rolling-hour cap on transmissions

[meshtastic.aliases]            # everyday names → node id or mesh name
"art car" = "!1234abcd"         # checked before fuzzy matching
```

- Worst-case answer time is `snapshot_timeout_s + request_timeout_s`; the two steps run in series.
- `fresh_within_s` was 900 in older releases. Stale fixes mislead for moving trackers; raise it explicitly if you prefer.

**Port safety**: obi refuses to open a serial device belonging to the [radio station](../setup/radio.md), whether discovered or set in `port`. Opening it asserts DTR and RTS, which key the transmitter, and the mesh path cannot de-key. Discovery therefore matches Meshtastic vendor ids itself, because the vendor library's helper returns any non-blocklisted port when no mesh radio is found.

```bash
obi mesh locate <name>                        # same answer as the mesh_locate chat tool
obi audit list --text mesh_position_request   # every transmission and withheld request
```

## City addresses (`[brc]`)

Optional. Given an event city plan, mesh answers, camp/art lookups and the XR city grid ([web.md](./web.md)) all use clock-and-distance addresses, sharing one survey cache.

```toml
[brc]
origin_lat = 37.16          # the plan's survey origin (the golden spike)
origin_lon = -118.27
year = 2030                 # plan edition; also names the local cache dir
# source_url = "https://gis.example.net/{year}/GeoJSON/{name}"   # unset ⇒ local cache only
# az12_deg = 45.0           # 12:00 axis fallback when the plan has no plazas

[playa]
base_url = "https://api.example.net"   # the event's public camp/art API
```

Camp/art lookups (`playa_locate`) also need `[playa]`, populated with `obi playa fetch`.
