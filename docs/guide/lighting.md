# Decorative lighting (`obi wled`)

obi can change WLED LED strips on request and, optionally, show network health on a small status band. This is obi's only device-write capability, and it is fenced.

## Safeguards

- Only devices in the `[wled.devices]` allowlist can be written; network gear never can.
- The current look is captured before every change; `revert` restores it. `--dry-run` previews.
- Every write is audited with its pre-change snapshot (see [audit.md](./audit.md)).
- obi never changes the lights unprompted, except for the status-band reconciler below.

```toml
[wled.devices]
portal = "https://lights.example.net"   # name → base URL; the COMPLETE allowlist

[wled]
# timeout = 5.0
# verify_tls = true
```

## Commands

```bash
obi wled status                          # read-only: power, brightness, look, live-stream flag
obi wled set --color purple              # or --effect "Fire 2012" / --brightness 60 / --off
obi wled set --palette Icefire           # palette from the device catalog
obi wled set --effect Fireworks --speed 30 --intensity 80   # percent
obi wled set --color red --color2 white  # secondary/tertiary slots (--color3)
obi wled set --color red --dry-run       # preview; writes nothing
obi wled revert                          # restore the look before obi's last change
obi audit list --text wled_set           # every write, with what it overwrote
```

In chat, Slack or voice: "make the portal purple", "fireworks at half speed", "portal brightness 50%", "what's the portal doing?", "portal revert".

- The same tools are exposed over MCP to Claude Desktop/Code.
- On the [radio](../setup/radio.md), ANY caller can set or revert: FM has no authentication, and a write is low-risk, one revert from undone, and audited per claimed handle.
- The write tools are never on the web tool route or available to the watcher.

## Shared-strip etiquette

The WLED controller is unauthenticated and last-writer-wins, shared with the WLED app, UDP-sync remotes and realtime streams. When a realtime show (LedFx/E1.31) is in control, obi says so rather than pretending a write stuck. `revert` restores whatever obi overwrote, including someone else's look.

## Status bands

Each strip can be split into a small **status band** and a **main pattern area**. The layout is declared by the operator, never assumed.

```toml
[wled.segments.portal]   # absent = writes apply to the whole strip
status = [0, 2, 4]       # segment ids of the status bands
main   = [1, 3, 5]       # pattern area for operator looks

[dash.status_band]       # second enable for the reconciler
enabled = true           # default false; enabling without a segment layout fails at load
```

With a layout declared, every `wled_set` / `wled_revert` on every surface targets the MAIN segments only, so a look or revert can never touch a status color.

The per-minute dash cycle ([dashboard.md](./dashboard.md)) sets the bands from **pageable network health**: solid green = all clear, solid amber = degraded, **blinking red** = an incident someone would be paged about.

- Display-only tiles (mesh, lighting) never turn it red. A device in UISP maintenance is ignored while down.
- Edge-triggered: steady state writes nothing. A color changed by someone else is healed within a minute, audited as `wled_status_set` / `actor="dash"`.
- A lighting failure never affects the board, pager or alerts. The reasoning model has no path to the bands.

```bash
obi wled status-band red --dry-run       # preview blinking red on the status segments
obi wled status-band green               # the only sanctioned caller besides the dash
obi audit list --text wled_status_set    # every reconcile write, heal and refusal
```
