# The radio audio chain: calibration and the TX loudness stage

obi's walkie-talkie station (feature 088) sits between a C-Media USB codec and a handheld radio.
Both directions run through more gain stages than are obvious, and the calibrated state is **not**
something obi can fully observe or restore on its own.

This document is obi's own record of that calibration. The measurements behind it were made in the
operator's private hardware test suite (`~/src/criticalrf`), which is **reference documentation
only** — no obi code path invokes it, and this file must stay useful if that suite disappears.

Measured 2026-08-19, recalibrated 2026-08-21.

## The chain

```
remote handset          our handset A              C-Media codec            obi
  mic gain,     --RF-->   speaker out     ------>   input gain     ------>  capture/STT
  deviation               VOLUME KNOB               (0 … +23.8 dB)
                          (in the path!)

  receives      <--RF--   mic input       <------   output gain    <------  TTS + loudnorm
                          (AGC, noise gate)         (0 … 0 dB)
```

The interface taps **post-volume speaker audio**, not discriminator audio. That is why handset A's
volume knob is part of the receive calibration rather than a personal listening preference.

## The calibrated state — three settings together

None of these is optional. Two obi can read; one it cannot.

| Stage | Setting | obi can see it? |
|---|---|---|
| Handset A volume knob | **1/10** | **No** — operator must verify by hand |
| Host input gain (RX) | **0.000** (+0 dB) | Yes — `obi radio gain` |
| Host output gain (TX) | **0.90** (−2.9 dB) | Yes — `obi radio gain` |

The knob's steps are nonuniform: 1→2 measured +5 dB, 2→3 nearly nothing, 3→7 +11.5 dB total.
**At 2/10 and above, normal speech clips.** There is no usable step below 1/10 (0 is mute), and the
host input gain is already at its floor, so the knob is the only remaining trim.

### Reference levels (knob 1/10, input gain 0)

| Condition | peak | rms |
|---|---|---|
| Unkeyed (squelched) | −66 dBFS | −78 dBFS |
| Keyed, room ambient only | −40 dBFS | −52 dBFS |
| Keyed, normal speech | median −12, loud syllables −4…−1 dBFS | −24.6 dBFS |
| PTT key-up click | **−0.2 dBFS** | — |

The loud-syllable ceiling near −1 dBFS is the remote's FM deviation limit landing just under full
scale — about 1 dB of headroom, and no flat-topping. **Leave the input gain at 0; any boost clips.**

The **PTT click reaches −0.2 dBFS**, roughly 14 dB above the loudest speech. It is real hardware,
not a meter artifact: it will be a loud pop in every recording, any level-triggered logic must
ignore it, and at any input gain above 0 it would hard-clip. This is why obi's pre-STT boost
(`radio/runner.py::normalize_for_stt`) references the 99.5th percentile rather than the peak — the
click clips instead of everything else staying quiet.

## The re-enumeration hazard

**Host gain settings do not survive USB re-enumeration.** Observed 2026-08-21: the interface came
back at a different USB location and both stages had silently reverted to power-on defaults —
input **0.504** (+12 dB, enough to clip reference speech) and output **1.000**.

After any replug, dock change, or reboot:

```sh
obi radio gain              # reports both scopes against the reference
obi radio gain --restore    # sets them back, then reads back to confirm
```

…and check handset A's knob is still at **1/10**. It survives USB events but is easy to bump.

obi warns about drift in `obi radio status`, in `obi radio devices`, and in the station log at
start-up. It never restores the gains on its own — obi does not silently change host audio state it
did not create.

## TX: why loudness, not gain

The naive fix for "obi sounds quiet on the channel" is more drive. It does not work, and the
measurement says why.

Handset A's mic path was characterised acoustically on 2026-08-21 (700 Hz tone bursts at known
drive levels, received loudness measured off the Mac's built-in mic with a Goertzel filter so room
noise could not pollute it):

- **A noise gate between −56 and −44 dB drive.** Below it the radio transmits a *silent carrier* —
  the tone vanished entirely rather than getting quiet.
- **Above the gate, ~2.5:1 compression.** 29 dB more drive produced only ~11 dB more received
  level. Mic AGC, in effect.

So digital level barely moves received loudness. **Received FM loudness is set by envelope density,
not peaks** — and density is the only audio-side lever left.

This overturns feature 088's original reasoning. The `tx_gain` ladder of 0.298 → 0.45 → 0.64 was
chasing an overdrive ceiling that does not exist: even 0.90 output gain with a −1 dBFS tone measured
in line with the compressed curve, with no harshness reported.

### What obi does now

| Stage | Setting |
|---|---|
| TTS render | `say -o`, 48 kHz |
| **Loudness** | **`loudnorm=I=-12:TP=-1:LRA=9`** (ffmpeg, single pass) |
| Digital gain | `[radio] tx_gain = 1.0` — none |
| Host output gain | 0.90 (−2.9 dB) |
| PTT / lead / tail | RTS / 1000 ms (`[radio] lead_ms`; repeater re-key rise — 300 ms sufficed on simplex) / 200 ms |

The original chain (`I=-10:LRA=5`) was chosen by a three-way A/B on the receiving end over
IT-direct simplex: plain TTS at the old 0.298 was noticeably quieter than human talkers;
loudness-normalized at 0.90 was judged best and comparable to them. **2026-08-27 repeater
retune**: on the repeated channel — which re-processes audio through the repeater's own
limiter — the operator judged that chain loud/harsh, and two by-ear steps replaced it:
`I=-12:LRA=7` (cooler, still harsh), then `I=-12:LRA=9` — `LRA` is the harshness knob.

`I` is deliberately hotter than broadcast norms (−16/−23) because the target is FM envelope
density, not a streaming loudness spec. **The `LRA` bound is the part that does the work** — it
compresses the ~14 dB crest factor of TTS speech, which is the measured difference between obi
and a human speaking through a handheld's mic AGC.

Keep effective drive above roughly **−40 dBFS**: quiet program audio near the gate threshold gets
chopped to a silent carrier, which on the air is indistinguishable from a dead key.

### ffmpeg

The loudness stage shells out to **ffmpeg**, which must be on PATH:

```sh
brew install ffmpeg
```

This is a deliberate, narrowly-scoped exception. obi's **voice** stack stays numpy-only (see the
comments in `voice/stt.py` and `voice/web/wav.py`) because its concerns were startup latency and
codec variance on operator-supplied audio — neither applies to a fixed filter over audio obi
rendered itself. The exception buys exact parity with the chain that was actually validated on air;
reimplementing EBU R128 plus range compression would ship an approximation of a measured result.

If ffmpeg is absent the stage degrades to pass-through: transmissions still go out, unprocessed, and
`obi radio status` reports `loudness: UNAVAILABLE`. Losing loudness is a degradation; losing the
transmission would be a failure.

### Not to be confused with playback normalization

`radio/tx.py::normalize_wav` is a **different stage for a different purpose**: it normalizes stored
clips for replay on the web board (RMS −14 dBFS, peak ceiling −1 dBFS), because received traffic is
recorded off the radio's speaker and arrives slammed while obi's own clips are TTS. It never runs on
the transmit path, and the TX loudness stage never runs on the replay path.

## Testing the chain

```sh
obi radio say "radio check, this is obi, over"
```

Transmits through the live station's real gate — clear-channel check, rate backstops, duration cap,
audit trail, retained clip. It counts against the rate backstops like any other transmission.

To repeat the A/B that chose the setting: have a human speak a sentence on the channel, transmit the
same sentence with `obi radio say`, and compare on a second radio at a **fixed** receiving volume.
The target is *comparable to human talkers*, not louder.

## The capture watchdog (feature 113)

Two ways the station has gone silently deaf in the field (both 2026-08-29), and the
in-process nets that now cover them — full design in
`specs/113-radio-capture-watchdog/` (research.md has the evidence, the contract has
the invariants W1–W9):

| Detector | Signal | Default | Action |
|---|---|---|---|
| flatline | a real codec never reads below its ~−85 dBFS noise floor; sustained blocks at/below **−100** = zombie stream after a USB re-enumeration | 120 s | reopen the capture source (RF-silent — the PTT half is untouched) |
| fade | healthy segment peaks pin near 0 dBFS (the PTT click rides in every pre-roll); a 20-segment window whose **max** peak is under **−10** = faded input path | 20 rx segments, min 10 | reopen; the post-reopen window logs whether reopening cured it |
| stuck guard | no run-loop progress (blocks consumed, probes, source transitions) for **300 s** = wedged process | 300 s | hard exit **70** — launchd relaunches |

Escalation: recoveries are spaced by `watchdog_floor_s` (600 s — never back-to-back,
the 2026-08-29 lesson), and a third fire inside `watchdog_escalation_s` (1 h) exits
cleanly with code **75** so launchd relaunches fresh. Exit codes: `0` stop, `70`
wedged hard-exit, `75` watchdog hand-back — visible in `launchctl print`'s last exit
status.

Every fire is a radio.log line + an audit event (`session_id="radio-watchdog"`, query
with `obi audit`) + a Slack DM. Tune or disable under `[radio]` (`watchdog = false`
restores pre-113 behavior exactly); all knobs in
`specs/113-radio-capture-watchdog/data-model.md`.

The interim scheduled restarts (`com.obi.radio-restart`, 04:20/16:20 kickstarts,
installed 2026-08-29) were **retired 2026-09-02** (operator decision, same day the
feature-122 preventive reopen went live): booted out, plist and
`~/.local/bin/obi-radio-restart.sh` deleted. Capture recovery is now entirely
in-process — the 122 reopen (below) plus, if re-enabled, these detectors. Keep
watching radio.db level trends (SC-006).

## The preventive capture reopen (feature 122)

The garble that developed over multi-hour capture uptimes (cured only by restarts) is
now bounded in-process: the runner closes and reopens the capture stream **after each
transmission** — armed when a foreign segment closes or obi's own TX unkeys, executed
at the first quiet gap — plus a **max-uptime backstop** for silent nights. Full design
in `specs/122-capture-reopen/` (contract invariants R1–R10); cadence is the operator's
2026-09-01 decision (per-transmission, with the churn-theory instrumentation below).

The quiet gate (every condition failing defers, nothing is ever forced): spacing floor
since the last reopen of any cause → no call open → no TX in flight → no pending
operator TX → channel quiet for `capture_reopen_quiet_ms` on the same LevelMonitor the
TX gate trusts. The reopen is RF-silent (the PTT half is untouched), whisper stays
loaded (~1 s deaf vs ~30 s for a process restart), self-TX exclusion windows carry
into the fresh segmenter, the TX-gate monitor restarts empty (no keying on pre-gap
evidence), and `PORTAUDIO_GUARD` makes a PortAudio reset under a live output stream
impossible. Preventive reopens never enter the watchdog escalation ladder.

| `[radio]` key | Default | Meaning |
|---|---|---|
| `capture_reopen_after_tx` | `true` | arm a reopen after each transmission |
| `capture_reopen_floor_s` | `60` | min spacing between reopens, any cause |
| `capture_reopen_quiet_ms` | `2000` | continuous quiet required to execute |
| `capture_reopen_max_uptime_s` | `7200` | backstop; `0` disables |

Every reopen is a radio.log line + an audit action (`capture reopen: preventive`,
`session_id="radio-watchdog"`) carrying the stream uptime, the trigger, and the
**measured deaf window** of the previous cycle; the host-gain verdict is re-logged
after every reopen. That pair is the tripwire for the open/close-churn theory: a
power-on gain revert after a reopen means churn really does provoke USB
re-enumeration — the rollback is `capture_reopen_after_tx = false` (backstop-only,
the conservative time-based design) and a larger floor. Off entirely:
`after_tx = false` + `max_uptime_s = 0` (pre-122 behavior).

With the backstop live, the twice-daily `com.obi.radio-restart` kickstarts became
redundant and were retired on 2026-09-02 (operator decision, ahead of the proving
week; first live reopen had just measured 332 ms deaf with gains calibrated). The
122 reopen is now the only automatic capture recovery while `watchdog = false`.

## The garble-triggered reopen (feature 133)

The 122 reopen bounds stream age but only runs **between** exchanges (its gate defers
while a call is open) — so a call that hit a sick stream still died: the garbled request
drew "say again" on the same stream, the repeat garbled too, and the call closed with
"unable to copy, out". Feature 133 makes a garbled in-call transmission *device
evidence*: the runner reopens the capture stream at the same block-loop seam
**immediately**, and holds obi's "say again" until the fresh stream's first block, so the
caller's repeat lands on new ears. Full design in `specs/133-radio-garble-reopen/`
(contract G1–G10); operator decisions of 2026-09-07 are recorded in its spec.

**The judgement** (`obi/radio/garble.py`, pure, always on — not behind the switch): a
transcript is garbled when it carries a hallucination signature — `repetition` (the
088 loop rule, unit widened to 12 chars), `non_latin` (three or more letters from
another script: "Within Earth,струструст…"), or `foreign_orthography` (Latin letters
English never uses, beyond one accented word: "Ó, píða, og fór í hana." — rx #6095,
2026-09-07 21:00:51 PT, accepted as intelligible at the time). Garbled rows are stored
`unintelligible`, so the say-again budget, the on-air close and low-confidence
attribution apply unchanged; the replay corpus is the false-positive guard.

**The trigger** is machine-derived: an `unintelligible` row the call machine answered
with `say_again` or `garble_close`. Nothing else arms it — idle-channel garble stays
silent (122 covers the device at the next quiet gap). **Gates**, cheapest first, each
failure *suppresses* (never defers — a reopen after the phrase has no value for this
exchange): enabled → one per call → stream age ≥ `garble_reopen_min_age_s` → the shared
`capture_reopen_floor_s` since any reopen → fewer than `garble_reopen_max_per_hour`
executed in the rolling hour → no other transmission in flight. A garble reopen stamps
the shared floor, so 122's next preventive reopen waits too.

| `[radio]` key | Default | Meaning |
|---|---|---|
| `garble_reopen` | `true` | arm an immediate reopen on an addressed in-call garble |
| `garble_reopen_min_age_s` | `60` | a younger stream is not suspected (garble on it is RF/caller trouble) |
| `garble_reopen_max_per_hour` | `6` | executed garble reopens per rolling hour; beyond it suppressed + one Slack note per hour |

**Ordering by deferral, not waiting**: the triggering outcome's transmit effects are
held in the runner and released at the fresh source's first block (or on `run()`'s
degrade path if the fresh source died before its first block — the caller is waiting on
the air; dropped only when the station is stopping). Keying earlier could not make
speech earlier: playback is serialized behind the capture open by `PORTAUDIO_GUARD`.
The "say again" then runs its own listen-before-transmit window on the reset monitor
(122 R7), so the added delay is the reopen gap plus `clear_window_ms`.

**Nothing silent**: every executed reopen logs `garble reopen: closing capture after
{age}s (call #C, {signature}, '…')` and audits `capture reopen: garble`; every
suppressed candidate logs and audits `capture reopen suppressed: garble` with its
reason (`per_call | stream_fresh | floor | hourly_cap | tx_busy`); and the field
question — does a reopen cure garble? — is answered per reopen by
`garble reopen outcome: repeat_ok | repeat_garbled | no_repeat` carrying that reopen's
measured deaf gap. All under `session_id="radio-watchdog"`:
`obi audit --source radio --session radio-watchdog`. The hourly cap tripping is the one
operator note (once per hour, independent of `slack_call_notes`); there is **no
automatic process restart** — the manual `obi radio restart` remains the last resort
until the proving week says otherwise. Rollback: `garble_reopen = false` (the judgement
stays).
