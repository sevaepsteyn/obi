"""T024 (US2): one-shot mode answers once and exits; the CLI emits the documented --json shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.chat.session import run_once
from obi.cli.main import app
from obi.core.config import Config
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import chat_config, chat_runtime, save_inventory, text_response, tool_response

runner = CliRunner()


def test_run_once_returns_answer(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config,
        responses=[tool_response("inventory_summary", {}), text_response("1 device total.")],
    )

    result = run_once(runtime, "summarize the network")

    assert result.answer == "1 device total."
    assert result.tools_used == ["inventory_summary"]


def test_cli_oneshot_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inv_path = tmp_path / "inventory.json"
    save_inventory(inv_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)])
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[discovery]
seeds = ["172.25.110.1"]

[routeros]
username = "admin"
password = "x"

[audit]
path = "{tmp_path / 'audit.jsonl'}"

[inventory]
path = "{inv_path}"

[chat]
api_key = "sk-test"
"""
    )
    config_file.chmod(0o600)

    def fake_build_runtime(config: Config) -> object:
        return chat_runtime(
            config,
            responses=[tool_response("inventory_summary", {}), text_response("1 device total.")],
        )

    monkeypatch.setattr("obi.chat.session.build_runtime", fake_build_runtime)

    result = runner.invoke(app, ["chat", "summarize", "--scope", str(config_file), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["question"] == "summarize"
    assert payload["answer"] == "1 device total."
    assert payload["tools_used"] == ["inventory_summary"]
