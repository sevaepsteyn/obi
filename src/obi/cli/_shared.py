"""Shared CLI plumbing (feature 066): the ambient-audit-scope helper every command opens through."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obi.core.config import Config


def _audit_capture(config: Config, source: str) -> AbstractContextManager[None]:
    """One CLI invocation's audit capture scope (feature 042).

    Opens the ambient scope every instrumented seam below records through: engine intent
    records (the AuditTrail tee), LLM calls, tool calls, and driver device commands. A
    no-op when ``[audit] enabled = false``. Read-only inspection commands (``obi audit``,
    ``logs query``, ``watch reports`` …) deliberately never open one (FR-012).
    """

    import uuid

    from obi.core.audit import audit_scope, recorder_from_config

    suffix = uuid.uuid4().hex[:12]
    session = (
        f"chat-{suffix}"
        if source in ("chat", "voice")
        else f"cli-{source.removeprefix('cli:')}-{suffix}"
    )
    return audit_scope(recorder_from_config(config), source, session)
