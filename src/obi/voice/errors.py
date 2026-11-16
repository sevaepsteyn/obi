"""Voice-surface errors."""

from __future__ import annotations


class VoiceUnavailable(Exception):
    """Voice mode cannot run; the message says why and how to fix it (CLI exit code 3).

    Mirrors :class:`obi.core.llm.ChatUnavailable`: raised for a missing voice extra, a missing
    or permission-denied microphone, or a declined model download — never for a transient
    mid-session problem.
    """
