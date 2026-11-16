"""`obi identity migrate-check` (T047): states per moved key, snippets, never a value."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app


def _run(tmp_path: Path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    path.chmod(0o600)
    return CliRunner().invoke(app, ["identity", "migrate-check", "--config", str(path)])


def test_unset_key_in_present_section_exits_1_with_snippet(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "[dash]\nenabled = true\n[weather]\nlatitude = 45.25\nlongitude = -100.5\n"
    )
    assert result.exit_code == 1
    assert "UNSET  dash.ping_router" in result.output
    assert 'ping_router = "<value>"' in result.output
    assert "SET  weather.latitude" in result.output
    assert "45.25" not in result.output and "-100.5" not in result.output


def test_absent_event_sections_block_because_defaults_used_to_apply(tmp_path: Path) -> None:
    # Before 137, weather/traffic/the city grid ran on built-in defaults with NO section:
    # their absence now silently turns them off, so the migration must decide explicitly.
    result = _run(tmp_path, '[logs]\ndb_path = "x.db"\n')
    assert result.exit_code == 1
    assert "SECTION ABSENT  weather.latitude" in result.output
    assert "[weather]\nlatitude = <value>" in result.output


def test_absent_optional_sections_do_not_block(tmp_path: Path) -> None:
    body = (Path(__file__).parents[1] / "unit" / "identity" / "moved_full.toml").read_text()
    body = body.replace("@TMP@", str(tmp_path))
    # drop the dash section: an absent dash was never implicitly on
    body = body.replace('[dash]\nping_router = "northfield-gw"\n', "")
    result = _run(tmp_path, body)
    assert result.exit_code == 0, result.output
    assert "SECTION ABSENT  dash.ping_router" in result.output
