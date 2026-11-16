"""The ``[service]`` config section (feature 081): additive, all-default, forgiving."""

from __future__ import annotations

from pathlib import Path

from obi.core.config import ServiceSettings, _parse_service, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_absent_section_is_all_default() -> None:
    settings = _parse_service(None)
    assert settings == ServiceSettings()
    assert settings.run_at_login is True
    assert settings.environment == {}


def test_run_at_login_false() -> None:
    assert _parse_service({"run_at_login": False}).run_at_login is False


def test_environment_values_coerced_to_strings() -> None:
    settings = _parse_service({"environment": {"PATH": "/x", "N": 5}})
    assert settings.environment == {"PATH": "/x", "N": "5"}


def test_non_mapping_environment_is_ignored_not_an_error() -> None:
    assert _parse_service({"environment": "nope"}).environment == {}
    assert _parse_service("garbage").run_at_login is True


def test_load_config_default_service_when_absent(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "[logs]\n"))
    assert config.service == ServiceSettings()


def test_load_config_wires_service_section(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            '[logs]\n[service]\nrun_at_login = false\nenvironment = { ANTHROPIC_API_KEY = "x" }\n',
        )
    )
    assert config.service.run_at_login is False
    assert config.service.environment == {"ANTHROPIC_API_KEY": "x"}
