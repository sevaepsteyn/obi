"""SC-003 part (a): the gate catches what the rewriter missed (feature 134, T044, analyze C4).

Planting a real token in a SOURCE store only tests the rewriter (it would map it). Here the token
is injected into the STAGED dataset after every source ran and before the gate — fault injection
through ``run_snapshot``'s post-source hook — so the only thing between it and publication is the
gate. Each case must: refuse (LeakGateError / exit 3), leave no output and no staging directory,
name the right file without printing the token, and audit the failure with its state.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from obi.core import configstore
from obi.core.audit import AuditTrail
from obi.core.demo import LeakGateError
from obi.core.demo.profile import load_profile
from obi.core.demo.snapshot import run_snapshot
from tests.fixtures.demo.harness import DemoEnv, make_env, run_cli

Plant = Callable[[Path], None]


def in_logs(token: str) -> Plant:
    def plant(staging: Path) -> None:
        conn = sqlite3.connect(staging / "state" / "obi" / "logs.db")
        conn.execute("UPDATE logs SET message = message || ? WHERE id = 7", (f" {token}",))
        conn.commit()
        conn.close()

    return plant


def in_json(token: str) -> Plant:
    def plant(staging: Path) -> None:
        path = staging / "state" / "obi" / "dash" / "board.json"
        data = json.loads(path.read_text())
        data["tiles"][0]["items"][0]["detail"] = token
        path.write_text(json.dumps(data))

    return plant


def in_old_commit(token: str) -> Plant:
    def plant(staging: Path) -> None:
        repo = staging / "state" / "obi" / "config-repo"
        when = datetime(2025, 6, 4, tzinfo=UTC)
        for content in (f"# {token}\n", "# clean again\n"):
            configstore.commit_snapshot(
                repo, {"note.rsc": content}, message="note", when=when,
                author_name="obi", author_email="obi@example.net",
            )  # fmt: skip

    return plant


CASES = [
    ("ip", "logs.db", in_logs("10.99.0.1")),
    ("ip", "board.json", in_json("8.8.4.1")),
    ("place", "logs.db", in_logs("northfield-gw")),
    ("place", "board.json", in_json("eastmoor-internet-gw")),
    ("serial", "logs.db", in_logs("HZ90A1B2C3D")),
    ("mac", "logs.db", in_logs("4C:5E:0C:11:22:01")),
    ("slack_id", "board.json", in_json("U0FAKE0001")),
    ("coord", "logs.db", in_logs("45.0123,-100.0456")),
    ("email", "board.json", in_json("pat@fictional-org.test")),
    ("person", "logs.db", in_logs("Quillon")),
    ("node", "board.json", in_json("!0fa4e001")),
    ("secret", "logs.db", in_logs("fakecommunity")),
    ("ip", "config-repo", in_old_commit("10.99.2.1")),
    ("place", "config-repo", in_old_commit("southgate-gw")),
]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoEnv:
    return make_env(tmp_path, monkeypatch)


@pytest.mark.parametrize(("category", "container", "plant"), CASES)
def test_gate_refuses_an_injected_leak(
    env: DemoEnv, category: str, container: str, plant: Plant
) -> None:
    raw = tomllib.loads(env.config.read_text())
    profile = load_profile(raw, config_path=env.config)
    with pytest.raises(LeakGateError) as caught:
        run_snapshot(
            raw, profile, audit=AuditTrail(env.state / "audit.jsonl"), post_source_hook=plant
        )
    report = str(caught.value)
    assert not env.output.exists()
    assert not list(env.output.parent.glob("*.staging-*"))
    assert any(category in line and container in line for line in report.splitlines()), report
    for token in env.truth.all_fatal_tokens():
        assert token not in report
    last = json.loads((env.state / "audit.jsonl").read_text().splitlines()[-1])
    assert last["action"] == "demo_snapshot" and last["params"]["state"] == "failed:budget_ok"


def test_cli_exit_code_3(env: DemoEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from obi.core.demo.sources import config_toml

    original = config_toml.run

    def leaky(ctx):  # type: ignore[no-untyped-def]
        report = original(ctx)
        in_logs("10.99.0.1")(ctx.out_state.parent.parent)
        return report

    monkeypatch.setattr("obi.core.demo.snapshot.config_toml.run", leaky)
    result = run_cli("demo", "snapshot", "--config", str(env.config))
    assert result.exit_code == 3
    assert "10.99.0.1" not in result.output and "ip/ip" in result.output
    assert not env.output.exists()
