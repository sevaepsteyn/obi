"""Unit (feature 013): [logs] config parsing, defaults, and first-class-source behavior."""

from __future__ import annotations

from pathlib import Path

from obi.core.config import (
    default_logs_db_path,
    default_logs_mirror_root,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_logs_section_parsed(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            "[logs]\n"
            'mirror_root = "/var/obi/logs"\n'
            'db_path = "/var/obi/logs.db"\n'
            "default_limit = 50\n"
            "max_limit = 500\n"
            "timeout = 9\n"
            'log_tz = "America/Los_Angeles"\n'
            'collectors = ["core-vms-1", "chestnut-vms-1"]\n',
        )
    )
    assert config.logs is not None
    assert config.logs.mirror_root == Path("/var/obi/logs")
    assert config.logs.db_path == Path("/var/obi/logs.db")
    assert config.logs.default_limit == 50
    assert config.logs.max_limit == 500
    assert config.logs.timeout == 9.0
    assert config.logs.log_tz == "America/Los_Angeles"
    assert config.logs.collectors == ["core-vms-1", "chestnut-vms-1"]
    assert config.logs.tzinfo().key == "America/Los_Angeles"  # type: ignore[attr-defined]


def test_bare_logs_section_uses_defaults_and_is_a_source(tmp_path: Path) -> None:
    # A logs-only config (no [routeros]/[uisp]/[netbox]) must LOAD — [logs] is a first-class source.
    config = load_config(_write(tmp_path, "[logs]\n"))
    assert config.logs is not None
    assert config.routeros is None and config.uisp is None and config.netbox is None
    assert config.logs.mirror_root == default_logs_mirror_root()
    assert config.logs.db_path == default_logs_db_path()
    assert config.logs.default_limit == 200 and config.logs.max_limit == 2000


def test_logs_tz_falls_back_to_utc_when_unknown(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "[logs]\n" 'log_tz = "Not/AZone"\n'))
    assert config.logs is not None
    # An invalid IANA name must not crash — tzinfo() degrades to UTC.
    assert config.logs.tzinfo().utcoffset(None).total_seconds() == 0  # type: ignore[union-attr]
