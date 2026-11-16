"""T003: ChatSettings loads from [chat] TOML (with env fallback) and redacts the API key."""

from __future__ import annotations

from pathlib import Path

from obi.core.config import Secret, load_config


def _write(tmp_path: Path, chat_section: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[discovery]
seeds = ["172.25.110.1"]

[routeros]
username = "admin"
password = "fakesecret44028f"
{chat_section}
"""
    )
    config.chmod(0o600)
    return config


def test_chat_settings_loaded_from_toml(tmp_path: Path) -> None:
    section = '[chat]\napi_key = "sk-ant-xyz"\nmodel = "claude-opus-4-8"\nmax_tokens = 2048\n'
    config = load_config(_write(tmp_path, section))

    assert config.chat.model == "claude-opus-4-8"
    assert config.chat.max_tokens == 2048
    assert isinstance(config.chat.api_key, Secret)
    assert config.chat.api_key.reveal() == "sk-ant-xyz"
    # The key is never exposed in repr/str (no accidental logging).
    assert "sk-ant-xyz" not in repr(config.chat.api_key)
    assert "sk-ant-xyz" not in str(config.chat)


def test_chat_settings_defaults_without_section(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, ""))

    assert config.chat.api_key is None
    assert config.chat.model == "claude-sonnet-5"
    assert config.chat.max_tokens == 1024
    assert config.chat.context_file is None


def test_context_file_parsed_and_expanded(tmp_path: Path) -> None:
    section = '[chat]\ncontext_file = "~/net/burning-man-network.md"\n'
    config = load_config(_write(tmp_path, section))

    assert config.chat.context_file == Path("~/net/burning-man-network.md").expanduser()


def test_context_file_absent_is_none(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, '[chat]\nmodel = "claude-opus-4-8"\n'))

    assert config.chat.context_file is None


def test_context_file_missing_path_still_loads(tmp_path: Path) -> None:
    # Validity is decided at load time (chat/context.py), so a bad/missing path must NOT make
    # load_config raise — chat startup and the offline commands stay robust.
    section = '[chat]\ncontext_file = "/no/such/file.md"\n'
    config = load_config(_write(tmp_path, section))

    assert config.chat.context_file == Path("/no/such/file.md")
