"""Unit (feature 026): the `config_query` chat handler — gates on [configs], renders current /
history / changes, degrades cleanly when unconfigured, and is a read-only tool. Feature 036 adds it
to the watcher's investigation loop as an unbudgeted (local-read) tool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import configstore
from obi.core.audit import AuditTrail
from obi.core.toolset import (
    BUDGETED_LIVE_TOOL_NAMES,
    ConfigBackupSource,
    ToolContext,
    build_dispatch,
    build_tools,
    build_watch_tools,
)
from tests.fakes import chat_config, config_backup_settings

_W = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
_EXPORT = (
    "# 2026-07-05 12:00:00 by RouterOS 7.15\n/ip firewall filter\nadd chain=input action=accept\n"
)
_EXPORT2 = (
    "# 2026-07-07 09:00:00 by RouterOS 7.15\n/ip firewall filter\nadd chain=input action=drop\n"
)


def _ctx(tmp_path: Path, *, configured: bool = True) -> ToolContext:
    settings = config_backup_settings(tmp_path) if configured else None
    config = chat_config(tmp_path, configs=settings)
    source = ConfigBackupSource(settings) if settings else None
    return ToolContext(
        config=config,
        driver=None,
        config_source=source,
        audit=AuditTrail(config.audit_path),
        question="show the gateway config",
    )


def _run(ctx: ToolContext, args: dict[str, object]) -> str:
    return build_dispatch(ctx, build_tools())["config_query"](args)


def _seed(ctx: ToolContext, *, two_versions: bool = False) -> None:
    repo = ctx.config.configs.repo_path
    configstore.record_version(
        repo, "core-gw-1", _EXPORT, address="172.25.110.1", router_version="7.15", when=_W
    )
    if two_versions:
        configstore.record_version(
            repo,
            "core-gw-1",
            _EXPORT2,
            address="172.25.110.1",
            router_version="7.15",
            when=_W + timedelta(days=2),
        )


def test_not_configured_returns_message(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, configured=False), {"device": "core-gw-1"})
    assert "config backup is configured" in out.lower() or "not configured" in out.lower()


def test_current_config(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    out = _run(ctx, {"device": "core-gw-1"})
    assert "/ip firewall filter" in out
    assert "action=accept" in out


def test_current_with_contains_filter(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    out = _run(ctx, {"device": "core-gw-1", "mode": "current", "contains": "accept"})
    assert "action=accept" in out
    assert "/ip firewall filter" not in out  # only matching lines


def test_history_mode(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, two_versions=True)
    out = _run(ctx, {"device": "core-gw-1", "mode": "history"})
    assert "change history" in out.lower()
    assert out.count("\n- ") == 2 or out.count("- ") >= 2


def test_changes_mode(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx, two_versions=True)
    # An ISO date between the two fixed commit dates makes the baseline deterministic (V1).
    out = _run(ctx, {"device": "core-gw-1", "mode": "changes", "since": "2026-07-06"})
    assert "action=drop" in out
    assert "-add chain=input action=accept" in out or "action=accept" in out


def test_unknown_device(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    _seed(ctx)
    out = _run(ctx, {"device": "ghost"})
    assert "no config backup" in out.lower()


def test_query_config_is_read_only_and_in_watch() -> None:
    # feature 036: the watcher's investigation loop may now read stored config. It is a local git
    # read (no device contact), so it is read-only and NOT part of the live-call budget.
    tool = next(t for t in build_tools() if t.name == "config_query")
    assert tool.risk_class == "read_only"
    assert tool.mutating is False
    assert "config_query" in {t.name for t in build_watch_tools()}
    assert "config_query" not in BUDGETED_LIVE_TOOL_NAMES
