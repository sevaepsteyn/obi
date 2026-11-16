"""Unit (feature 013): a logs-only config builds a chat runtime (logs is a first-class source)."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import build_runtime
from obi.core.config import ChatSettings, Config, Secret
from obi.core.drivers.sqlite_logs import SqliteLogSource
from tests.fakes import logs_settings


def _config(tmp_path: Path, **sources: object) -> Config:
    return Config(
        chat=ChatSettings(api_key=Secret("sk-test")),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
        **sources,  # type: ignore[arg-type]
    )


def test_logs_only_builds_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, logs=logs_settings()))
    assert runtime.ctx.driver is None
    assert runtime.ctx.uisp_source is None
    assert runtime.ctx.netbox_source is None
    assert isinstance(runtime.ctx.log_source, SqliteLogSource)


def test_logs_alongside_mikrotik(tmp_path: Path) -> None:
    from tests.fakes import settings as mikrotik_settings

    runtime = build_runtime(_config(tmp_path, routeros=mikrotik_settings(), logs=logs_settings()))
    assert runtime.ctx.driver is not None
    assert isinstance(runtime.ctx.log_source, SqliteLogSource)
