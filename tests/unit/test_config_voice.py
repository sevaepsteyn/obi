"""Unit tests: the optional ``[voice]`` config section (feature 023, T003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, VoiceSettings, load_config


def _write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("[logs]\n" + body)  # [logs] satisfies the >=1-source gate
    config.chmod(0o600)
    return config


def test_voice_defaults_when_section_absent(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, ""))
    assert config.voice == VoiceSettings()
    assert config.voice.stt_model == "mlx-community/whisper-large-v3-turbo"
    assert config.voice.tts_voice is None
    assert config.voice.tts_rate is None
    assert config.voice.rms_threshold == 0.01


def test_voice_defaults_when_section_empty(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, "[voice]\n"))
    assert config.voice == VoiceSettings()


def test_voice_every_override(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            "\n".join(
                [
                    "[voice]",
                    'stt_model = "mlx-community/whisper-small-mlx"',
                    'tts_voice = "Samantha"',
                    "tts_rate = 190",
                    "rms_threshold = 0.02",
                ]
            ),
        )
    )
    assert config.voice.stt_model == "mlx-community/whisper-small-mlx"
    assert config.voice.tts_voice == "Samantha"
    assert config.voice.tts_rate == 190
    assert config.voice.rms_threshold == 0.02


def test_voice_non_positive_tts_rate_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="tts_rate"):
        load_config(_write_config(tmp_path, "[voice]\ntts_rate = 0\n"))


def test_voice_rms_threshold_out_of_range_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="rms_threshold"):
        load_config(_write_config(tmp_path, "[voice]\nrms_threshold = 1.5\n"))
    with pytest.raises(ConfigError, match="rms_threshold"):
        load_config(_write_config(tmp_path, "[voice]\nrms_threshold = 0.0\n"))


def test_voice_empty_stt_model_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="stt_model"):
        load_config(_write_config(tmp_path, '[voice]\nstt_model = ""\n'))


def test_voice_web_engine_defaults(tmp_path: Path) -> None:
    """024/033: the voice-engine web keys default sensibly when absent. (The 024 hosting
    keys web_port/web_bind were retired by 033 — see test_config_web.py.)"""

    config = load_config(_write_config(tmp_path, "[voice]\n"))
    assert config.voice.web_idle_minutes == 30
    assert config.voice.web_max_seconds == 90


def test_voice_web_engine_every_override(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            "\n".join(
                [
                    "[voice]",
                    "web_idle_minutes = 5",
                    "web_max_seconds = 30",
                ]
            ),
        )
    )
    assert config.voice.web_idle_minutes == 5
    assert config.voice.web_max_seconds == 30


def test_voice_web_idle_minutes_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="web_idle_minutes"):
        load_config(_write_config(tmp_path, "[voice]\nweb_idle_minutes = 0\n"))


def test_voice_web_max_seconds_bounds(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="web_max_seconds"):
        load_config(_write_config(tmp_path, "[voice]\nweb_max_seconds = 0\n"))
    with pytest.raises(ConfigError, match="web_max_seconds"):
        load_config(_write_config(tmp_path, "[voice]\nweb_max_seconds = 601\n"))


def test_voice_section_does_not_satisfy_the_source_gate(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[voice]\ntts_voice = "Samantha"\n')  # no source section at all
    config.chmod(0o600)
    with pytest.raises(ConfigError):
        load_config(config)
