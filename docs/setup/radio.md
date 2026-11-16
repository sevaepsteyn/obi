# Radio station (`obi radio`)

obi can sit on a half-duplex operations channel as a **station**: it transcribes every
transmission locally, and, behind a hard gate that is off by default, answers calls addressed to
it by name. Speech recognition and synthesis run on the operator's machine. Only transcript text
reaches the model.

## Hardware

A USB audio interface (the codec) plus an FTDI USB-serial adapter that keys the radio's PTT
line. Install the extra: `uv sync --extra radio`.

## Configure

```toml
[radio]
ftdi_serial = "<your PTT cable's FTDI serial>"   # or: serial_port = "/dev/cu.usbserial-XXXX"
audio_name = "USB PnP Sound Device"              # the codec's device name
channel_name = "the operations radio channel"    # how the persona names its channel
known_callers = ["Kestrel", "Wren"]              # the channel's regular callsigns
operator_handles = ["Quill"]                     # your own handle(s)
tx_enabled = false                               # THE transmit gate; leave off until tested

[radio.caller_aliases]       # known mis-hearings of a callsign → the roster name
quilt = "Quill"

[radio.pronunciations]       # how the synthesizer should say a site or term
GW = "gee double-you"

[radio.speech_aliases]       # inbound: a heard word → the canonical term
"gee dub" = "GW"
```

`obi radio` refuses to start without `ftdi_serial` or `serial_port`: there is no built-in
cable.

## Run

```sh
uv run obi radio status          # both hardware halves, launchd state, last activity
uv run obi radio run             # foreground: capture → transcribe → store
uv run obi radio start           # supervised launchd agent
uv run obi radio tail            # recent transmissions (read-only)
uv run obi radio say "radio check, over"   # on-air test (needs tx_enabled)
```

Start with `tx_enabled = false` and `shadow_tx = true`. obi then drafts replies to Slack without
ever keying the radio. Turn transmit on only after the drafts look right.
