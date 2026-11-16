"""T022 (feature 011) → feature 105: `unifi_status` is DISABLED registry-wide.

Operator decision 2026-08-26: playa wifi is another team's system, so the live-UniFi read is
offered on NO surface for now. The handler (plus the engine read, `obi unifi` CLI, UNIFI board,
and UniFi log ingest) remains; this contract pins the disable and keeps the handler's
unconfigured path honest so a future re-enable starts from a working tool.
"""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.toolset import ToolContext, _unifi_status, build_tools
from tests.fakes import chat_config


def test_unifi_status_is_not_registered() -> None:
    assert "unifi_status" not in {tool.name for tool in build_tools()}


def test_dormant_handler_returns_no_config_string_when_unconfigured(tmp_path: Path) -> None:
    # No [unifi] section and no source → a guiding string, never an exception (unchanged
    # since 011; exercised directly because no dispatch carries the tool any more).
    config = chat_config(tmp_path, routeros=True)  # mikrotik-only host, no unifi
    ctx = ToolContext(config=config, driver=None, audit=AuditTrail(config.audit_path))
    out = _unifi_status(ctx, {})
    assert "No UniFi Network console is configured" in out
