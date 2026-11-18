"""Fictional names are stable across snapshots (feature 134, T049, US5-2, FR-014)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from obi.core.demo.mapping import PseudonymMap
from tests.fixtures.demo.harness import DemoEnv, make_env, run_cli

# Runs the demo snapshot/export pipeline (seconds per test) — skip with -m 'not slow'.
pytestmark = pytest.mark.slow


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoEnv:
    return make_env(tmp_path, monkeypatch)


def test_second_snapshot_keeps_every_assignment_and_adds_new(env: DemoEnv, tmp_path: Path) -> None:
    assert run_cli("demo", "snapshot", "--config", str(env.config)).exit_code == 0
    first = PseudonymMap.load_or_create(env.mapping).to_dict()

    inventory = env.state / "inventory.json"
    data = json.loads(inventory.read_text())
    newcomer = dict(data["entries"][0], identity="westbrook-gw", address="10.99.9.1",
                    serial="HZ90A1B2C99", alt_addresses=[])  # fmt: skip
    data["entries"].append(newcomer)
    inventory.write_text(json.dumps(data))

    second_out = tmp_path / "out" / "second"
    result = run_cli("demo", "snapshot", "--config", str(env.config), "--out", str(second_out))
    assert result.exit_code == 0, result.output
    second = PseudonymMap.load_or_create(env.mapping).to_dict()

    assert second["seed"] == first["seed"]
    for cls, table in first["classes"].items():
        for real, fake in table.items():
            assert second["classes"][cls][real] == fake
    assert "westbrook" in second["classes"]["place"]
    assert second["classes"]["place"]["westbrook"] not in first["classes"]["place"].values()

    mode = stat.S_IMODE(env.mapping.stat().st_mode)
    assert mode == 0o600
    for out in (env.output, second_out):
        assert env.mapping.resolve().is_relative_to(out.resolve()) is False
        assert not list(out.rglob("mapping.json"))
