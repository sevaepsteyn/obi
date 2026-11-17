# Voice

Ask obi out loud and hear the answer, from the terminal or from the web viewer (phone, laptop, Quest 3). Voice uses the same read-only tools as [chat](./chat.md); the two-way radio station is documented in [../setup/radio.md](../setup/radio.md).

## Terminal (`obi voice talk`)

Press Enter, ask "is the showers link up?", press Enter again. obi shows what it heard and speaks a short conversational answer.

```bash
uv sync --extra voice    # one-time: the local speech stack (Apple Silicon Mac)
uv run obi voice talk    # first run offers a one-time ~1.6 GB speech-model download
```

- Follow-ups keep context ("and its signal?"). Ctrl-C interrupts a recording, a slow lookup, or obi mid-sentence.
- The mic is live only between your two Enter presses.
- **Privacy:** capture, transcription (MLX Whisper) and synthesis (macOS `say`) run on the Mac. Audio is never written to disk, retained or transmitted; only the transcribed text goes to the reasoning model, as with typed chat.
- Silent or noise-only captures are gated locally and never reach the model.

```toml
[voice]   # optional; all fields have defaults
stt_model = "mlx-community/whisper-large-v3-turbo"  # or "mlx-community/whisper-small-mlx" (~0.5 GB)
tts_voice = "Samantha"                              # `say -v ?` lists installed voices
tts_rate  = 190                                     # words per minute
web_idle_minutes = 30                               # browser conversation idle reset
web_max_seconds = 90                                # browser max capture length
```

## In the browser

The [web viewer](./web.md) serves the same spoken conversation on its own origin, to any browser on the LAN.

```bash
uv run obi web serve   # voice + PING/TRACE on by default; set [web] voice = false to drop voice
```

**2D viewer**

- Hold the header mic button, speak, release; obi shows what it heard and speaks the answer. Or **type** in the header text box and press Enter — typed questions join the same conversation and need no mic.
- Context is per page; reloading (or ~30 idle minutes) starts fresh.
- Once per device: accept the self-signed-certificate warning (browsers grant the mic only to trusted HTTPS), then allow mic access on the first hold.
- With voice off on the server, the mic is grayed out and says how to enable it.

**3D / headset viewer**

- **Hold grip on either controller** — or pinch-hold the small talk button at the bottom of the view with bare hands — speak, release.
- A head-locked card shows listening → what obi heard → thinking (with the tools consulted) → the answer text, while the answer plays aloud.
- Once per headset: tap **Enable microphone** on the setup page *before* entering the immersive session (prompts can't render in-immersive; the grant persists per origin). Declining, or `[web] voice = false`, leaves every other viewer feature unchanged.
- Failures get distinct in-scene cards — obi unreachable, too-quiet capture (wind), busy with the previous question, model unavailable — and navigation keeps working.

**Privacy and trust**

- One turn engine on the viewer's origin: no second trust step, no second port.
- Speech crosses **only your LAN** to the Mac, is transcribed and synthesized there, never retained, never sent to a cloud speech service. Only the transcript goes to the reasoning model.
- The service is unauthenticated within the LAN (the viewer's trust model) and must **never** be exposed beyond it.
- Read-only. Every turn is audited as `voice-web:<conversation>`; headset turns use an `ar-` conversation prefix.
