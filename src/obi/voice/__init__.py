"""Voice interface (feature 023): push-to-talk spoken front-end to the chat engine.

A thin spoken transport over the one core (constitution III, amended 1.6.0): capture →
on-device transcription → the same read-only chat turn as ``obi chat`` → on-device speech
synthesis. Raw audio never leaves the host and is never retained; only the transcribed text
reaches the model (Principle VI carve-out, voice-strict variant).

The heavy audio libraries (``sounddevice``, ``mlx_whisper``) are imported ONLY inside this
package, lazily, behind :class:`~obi.voice.errors.VoiceUnavailable` guards — the base install
works without the ``voice`` extra.
"""
