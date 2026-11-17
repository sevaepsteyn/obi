# Weather and event gate traffic

Two optional feeds obi polls into local stores: a weather snapshot for one location, and an event's published gate travel times. Their chat tools answer only from those stores, so they work on every surface (chat, Slack, voice, web, MCP, radio) even with the uplink down; neither has a watch pass or widget, and neither ever alerts or pages.

## Weather (`obi weather`)

Each snapshot holds current conditions and the next 24 hours' raw hourly forecast from **Open-Meteo** (keyless; HRRR-blended, imperial units, site-local times), plus active **NWS alerts** when `nws_zone` is set. `weather_forecast` returns numbers, units and the snapshot's age — no verdicts — and history answers *"what was the wind at 2am?"* from the nearest snapshot.

Weather is **off until you configure a location** (no built-in one); without `[weather]` the tools are not offered to the model.

```bash
uv run obi weather run       # one snapshot: forecast + alerts (the cron entrypoint)
uv run obi weather status    # snapshot age, per-source outcome, history coverage
```

```cron
# hourly at :17 (clear of the :00–:04 collector stagger); the log feeds the dash pipeline tile
17 * * * * cd ~/src/obi && uv run obi weather run >> ~/.local/state/obi/weather.log 2>&1
```

```toml
[weather]
latitude = 37.16                  # required
longitude = -118.27               # required
place_name = "Example Valley"     # optional: how answers name the place
# nws_zone = "ABZ001"             # optional: NWS forecast zone for alerts
# db_path = "~/.local/state/obi/weather.db"
# timeout_s = 20.0
# retention_days = 120

[site]
timezone = "America/Los_Angeles"  # local times in answers (default UTC)
```

A fetch failure is a **recorded outcome**, never a crash: it shows in `obi weather status` and the dash `pipeline` tile ([dashboard.md](./dashboard.md)), and the tool answers *"unknown as of …"*. Sources fail independently — NWS down never costs the forecast, or vice versa.

## Event gate traffic (`obi traffic`)

Every 5 minutes obi polls (with conditional requests) an event gate dashboard's **Travel Time Log** (*"Gate: 90 min"*) and **ingress/egress gate status**. `gate_traffic` serves the newest report verbatim with time and age, recent reports for trend, and lane state + advisory; obi **never computes or predicts a wait**.

Traffic is **off until you configure `base_url`**; without `[traffic]` the tool is not offered to the model.

```bash
uv run obi traffic run       # one poll: feed + gate status (the cron entrypoint)
uv run obi traffic status    # newest entry, gate status, poll outcomes, coverage
```

```cron
# every 5 minutes; the log feeds the dash pipeline tile
*/5 * * * * cd ~/src/obi && uv run obi traffic run >> ~/.local/state/obi/traffic.log 2>&1
```

```toml
[traffic]
base_url = "https://traffic.example.net"   # required: the event dashboard to poll
# timeout_s = 20.0
# retention_days = 120
# db_path = "~/.local/state/obi/traffic.db"
```

- **Pre-event 403** is a recorded outcome, answered as *"the dashboard hasn't published travel times yet"*.
- **Defensive parsing:** the entry schema is unverified until the event, so recognizable fields are used, every entry's raw JSON is kept verbatim, and an unparseable entry is still served as text with its capture time.
