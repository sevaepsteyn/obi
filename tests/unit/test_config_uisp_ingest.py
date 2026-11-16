"""T004: [uisp] log-ingestion settings parse + opt-in/back-compat gating (feature 019)."""

from __future__ import annotations

from pathlib import Path

from obi.core.config import Secret, load_config


def _write(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(body)
    config.chmod(0o600)
    return config


def test_status_only_uisp_config_has_ingestion_disabled(tmp_path: Path) -> None:
    """A feature-003 [uisp] config (no ingest flags) keeps working with ingestion inactive."""

    cfg = load_config(
        _write(
            tmp_path,
            """
[uisp]
base_url = "https://uisp.example.com"
token = "TOK123"
""",
        )
    )
    assert cfg.uisp is not None
    assert cfg.uisp.ingest_enabled is False
    assert cfg.uisp.ingest_events is False
    assert cfg.uisp.ingest_outages is False
    assert cfg.uisp.min_level is None
    assert cfg.uisp.first_run_lookback == "30d"
    assert isinstance(cfg.uisp.token, Secret)


def test_ingestion_settings_parse_and_enable(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[uisp]
base_url = "https://uisp.example.com"
token = "TOK123"
ingest_events = true
ingest_outages = true
min_level = "WARNING"
first_run_lookback = "14d"
""",
        )
    )
    assert cfg.uisp is not None
    assert cfg.uisp.ingest_enabled is True
    assert cfg.uisp.ingest_events is True
    assert cfg.uisp.ingest_outages is True
    assert cfg.uisp.min_level == "warning"  # normalized to lower-case
    assert cfg.uisp.first_run_lookback == "14d"


def test_outages_only_is_enabled(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[uisp]
base_url = "https://uisp.example.com"
token = "TOK123"
ingest_outages = true
""",
        )
    )
    assert cfg.uisp is not None
    assert cfg.uisp.ingest_enabled is True
