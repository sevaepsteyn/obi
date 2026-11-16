"""Load an operator-provided network-context document for obi chat (feature 008).

The operator names a free-form file via ``[chat] context_file``; chat reads it once at session
start and injects it into the agent's system prompt as *interpretation-only* background (it never
becomes a source of asserted facts — see :mod:`obi.core.prompt`). Loading is best-effort and
**total**: a missing, unreadable, empty, or oversized file yields a clear warning and no context,
never an exception, so chat always starts (FR-005/006; Constitution VI — field resilience).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from obi.core.config import ChatSettings

# Generous for a human network description (the example doc is ~2.7 KiB) while bounding the size
# of every cloud request. Over the bound ⇒ skip with a warning (never truncate). Not
# operator-configurable in v1 (YAGNI).
MAX_CONTEXT_BYTES = 16 * 1024  # 16 KiB


@dataclass
class ContextLoad:
    """The outcome of attempting to load the context file; always returned, never raised.

    ``text`` is the usable context (``None`` when there is none); ``warning`` is an actionable
    message for any non-happy case (``None`` on success or when no ``context_file`` is set).
    ``source``/``size_bytes`` describe what was read, for the startup log (FR-013).
    """

    text: str | None = None
    warning: str | None = None
    source: Path | None = None
    size_bytes: int = 0


def load_context(settings: ChatSettings) -> ContextLoad:
    """Load the configured context file. Total — returns a :class:`ContextLoad`, never raises."""

    path = settings.context_file
    if path is None:
        return ContextLoad()  # unset ⇒ a no-op, not a warning.

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ContextLoad(
            warning=f"context file not found: {path} — chat will run without it.", source=path
        )
    except OSError as exc:
        return ContextLoad(
            warning=f"could not read context file {path}: {exc} — chat will run without it.",
            source=path,
        )

    if size > MAX_CONTEXT_BYTES:
        return ContextLoad(
            warning=(
                f"context file {path} is {size} bytes (> {MAX_CONTEXT_BYTES // 1024} KiB) — "
                "skipping it; trim it to use it."
            ),
            source=path,
            size_bytes=size,
        )

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:  # is-a-directory, permission, bad encoding, …
        return ContextLoad(
            warning=f"could not read context file {path}: {exc} — chat will run without it.",
            source=path,
            size_bytes=size,
        )

    if not text.strip():
        return ContextLoad(
            warning=f"context file {path} is empty — ignoring it.", source=path, size_bytes=size
        )

    return ContextLoad(text=text, source=path, size_bytes=size)
