"""US3 — the `obi watch` CLI: run / reports / status (feature 014, T021)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.watch import Evidence, WatchCursor, WatchFinding, WatchRun
from obi.core.watchstore import WatchStore
from tests.fakes import seed_logs

runner = CliRunner()
_OFFLINE = {"ANTHROPIC_API_KEY": ""}  # force the offline (no-judgement) path
NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'mirror_root = "{tmp_path / "mirror"}"\n'
        f'db_path = "{tmp_path / "logs.db"}"\n'
        'log_tz = "UTC"\n'
        "\n[watch]\n"
        "enabled = true\n"
        f'watch_dir = "{tmp_path / "watch"}"\n'
        "\n[audit]\n"
        f'path = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)
    return config


def _seed_finding(tmp_path: Path) -> WatchStore:
    """Write one finding + a run record, as a real cycle would, for the review commands."""

    # Stamp at "recent" (real now), not the fixed NOW, so the reports --since 1d filter and the
    # status last_24h window include it regardless of when the suite runs (avoids date-rot).
    recent = datetime.now(UTC)
    store = WatchStore(tmp_path / "watch")
    store.write_finding(
        WatchFinding(
            id="20260625T120000-core-1-1",
            created_at=recent,
            device="core-1",
            window_start=recent,
            window_end=recent,
            severity="critical",
            confidence="high",
            summary="ether1 flapping",
            description="ether1 keeps bouncing",
            evidence=[Evidence(kind="log", text="link ether1 down")],
            source="llm",
        )
    )
    store.save_state(
        WatchCursor(id=5),
        WatchRun(
            started_at=recent,
            finished_at=recent,
            new_event_count=3,
            outcome="findings",
            tokens_input=120,
            tokens_output=40,
            llm_calls=1,
            finding_ids=["20260625T120000-core-1-1"],
        ),
    )
    return store


def test_run_offline_is_clean(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    seed_logs(tmp_path / "mirror", tmp_path / "logs.db", [("core-1", NOW, "system,info", "ok")])
    result = runner.invoke(app, ["watch", "run", "--scope", str(config)], env=_OFFLINE)
    assert result.exit_code == 0, result.output
    assert "watch" in result.output


def test_reports_and_status(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _seed_finding(tmp_path)

    reports = runner.invoke(
        app, ["watch", "reports", "--scope", str(config), "--since", "1d", "--json"], env=_OFFLINE
    )
    assert reports.exit_code == 0, reports.output
    findings = json.loads(reports.output)
    assert any(f["device"] == "core-1" and f["severity"] == "critical" for f in findings)

    status = runner.invoke(
        app, ["watch", "status", "--scope", str(config), "--json"], env=_OFFLINE
    )
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["last_run"]["outcome"] == "findings"
    assert payload["last_24h"]["tokens_input"] == 120


def test_show_one_report(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _seed_finding(tmp_path)
    result = runner.invoke(
        app,
        ["watch", "reports", "--scope", str(config), "--show", "20260625T120000-core-1-1"],
        env=_OFFLINE,
    )
    assert result.exit_code == 0, result.output
    assert "ether1 flapping" in result.output


def test_run_disabled_is_a_noop(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'db_path = "{tmp_path / "logs.db"}"\n'
        "\n[watch]\n"
        "enabled = false\n"
        f'watch_dir = "{tmp_path / "watch"}"\n'
    )
    config.chmod(0o600)
    result = runner.invoke(app, ["watch", "run", "--scope", str(config)], env=_OFFLINE)
    assert result.exit_code == 0
    assert "disabled" in result.output
