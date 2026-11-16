"""Unit (feature 042): [audit] config parsing, defaults, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, default_audit_db_path, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_defaults_when_section_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "[logs]\n"))
    assert config.audit.enabled is True
    assert config.audit.db_path == default_audit_db_path()
    assert config.audit.retention_days == 90
    assert config.audit.max_payload_bytes == 500_000


def test_section_parsed(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            "[logs]\n"
            "[audit]\n"
            "enabled = false\n"
            'db_path = "/var/obi/audit.db"\n'
            "retention_days = 30\n"
            "max_payload_bytes = 8192\n",
        )
    )
    assert config.audit.enabled is False
    assert config.audit.db_path == Path("/var/obi/audit.db")
    assert config.audit.retention_days == 30
    assert config.audit.max_payload_bytes == 8192


def test_legacy_jsonl_path_key_still_works_alongside(tmp_path: Path) -> None:
    # Pre-042 the [audit] section held only the JSONL trail `path`; both must coexist.
    config = load_config(
        _write(tmp_path, "[logs]\n[audit]\npath = \"/var/obi/audit.jsonl\"\nretention_days = 7\n")
    )
    assert config.audit_path == Path("/var/obi/audit.jsonl")
    assert config.audit.retention_days == 7
    assert config.audit.enabled is True


def test_retention_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(_write(tmp_path, "[logs]\n[audit]\nretention_days = 0\n"))


def test_payload_bound_floor(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_payload_bytes"):
        load_config(_write(tmp_path, "[logs]\n[audit]\nmax_payload_bytes = 100\n"))
