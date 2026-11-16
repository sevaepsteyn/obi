"""`obi demo snapshot | gate | lookup` end to end (feature 134, T043/T050/T051).

Covers the contracts/cli.md exit codes, dry-run purity, SC-002 (an independent grep for every
planted token), SC-004 (topology counts), SC-005 (byte-identical re-runs), FR-002 (the live state
tree untouched but for the audit append and the private mapping), and time fidelity.
"""

from __future__ import annotations

import filecmp
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.demo.harness import (
    DemoEnv,
    dump_text,
    make_env,
    offline_terrain,
    run_cli,
    tree_state,
)

ALLOWED_LIVE_CHANGES = re.compile(r"^(audit\.db.*|audit\.jsonl|demo/mapping\.json)$")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoEnv:
    return make_env(tmp_path, monkeypatch)


def snapshot(env: DemoEnv, *extra: str) -> object:
    return run_cli("demo", "snapshot", "--config", str(env.config), *extra)


def _dircmp_equal(a: Path, b: Path) -> list[str]:
    diffs: list[str] = []

    def walk(cmp: filecmp.dircmp[str], prefix: str) -> None:
        diffs.extend(f"{prefix}{n}" for n in cmp.left_only + cmp.right_only + cmp.funny_files)
        for name in cmp.common_files:
            if not filecmp.cmp(Path(cmp.left) / name, Path(cmp.right) / name, shallow=False):
                diffs.append(f"{prefix}{name}")
        for name, sub in cmp.subdirs.items():
            walk(sub, f"{prefix}{name}/")

    walk(filecmp.dircmp(a, b), "")
    return diffs


def test_dry_run_writes_nothing_and_prints_no_token(env: DemoEnv) -> None:
    before = tree_state(env.state)
    result = snapshot(env, "--dry-run")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert not env.output.exists() and not env.mapping.exists()
    assert tree_state(env.state) == before
    for token in env.truth.all_fatal_tokens():
        assert token not in result.output  # type: ignore[attr-defined]
    assert "2025-06-0" in result.output  # type: ignore[attr-defined]


def test_full_run_publishes_a_clean_dataset(env: DemoEnv) -> None:
    before = tree_state(env.state)
    result = snapshot(env)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    text = dump_text(env.output)
    for token in env.truth.all_fatal_tokens():  # SC-002: independent of the gate
        assert token not in text
    changed = {
        name
        for name in set(before) | set(tree_state(env.state))
        if before.get(name) != tree_state(env.state).get(name)
    }
    assert all(ALLOWED_LIVE_CHANGES.match(name) for name in changed), sorted(changed)
    assert env.mapping.exists() and (env.mapping.stat().st_mode & 0o077) == 0
    assert not list(env.output.parent.glob("*.staging-*"))


def test_topology_counts_preserved(env: DemoEnv) -> None:
    assert snapshot(env).exit_code == 0  # type: ignore[attr-defined]
    live = json.loads((env.state / "ar" / "snapshots" / "ospf.json").read_text())["data"]
    out = json.loads((env.output / "state" / "obi" / "ar" / "snapshots" / "ospf.json").read_text())[
        "data"
    ]
    assert len(out["edges"]) == len(live["edges"]) and len(out["nodes"]) == len(live["nodes"])
    live_geo = json.loads((env.state / "ar" / "links.json").read_text())
    out_geo = json.loads((env.output / "state" / "obi" / "ar" / "links.json").read_text())
    assert len(out_geo["links"]) == len(live_geo["links"])
    assert len(out_geo["sites"]) == len(live_geo["sites"])


def test_two_runs_are_byte_identical(env: DemoEnv, tmp_path: Path) -> None:
    assert snapshot(env).exit_code == 0  # type: ignore[attr-defined]
    second = tmp_path / "out" / "second"
    assert snapshot(env, "--out", str(second)).exit_code == 0  # type: ignore[attr-defined]
    assert _dircmp_equal(env.output, second) == []


def test_existing_output_is_refused(env: DemoEnv) -> None:
    env.output.mkdir(parents=True)
    assert snapshot(env).exit_code == 2  # type: ignore[attr-defined]


def test_exit_2_missing_denylist(env: DemoEnv) -> None:
    (env.config.parent / "demo-denylist.txt").unlink()
    assert snapshot(env).exit_code == 2  # type: ignore[attr-defined]
    assert not env.output.exists()


def test_exit_2_pool_collision(env: DemoEnv) -> None:
    text = env.config.read_text().replace(
        '[siklu]\nhosts = ["10.99.3.40"]', '[siklu]\nhosts = ["10.99.3.40", "172.20.0.9"]'
    )
    env.config.write_text(text)
    assert snapshot(env).exit_code == 2  # type: ignore[attr-defined]
    assert not env.output.exists() and not list(env.output.parent.glob("*.staging-*"))


def test_exit_4_over_budget_then_allow_oversize(env: DemoEnv) -> None:
    env.config.write_text(
        env.config.read_text().replace("[demo]\n", "[demo]\nmax_dataset_mb = 1\n")
    )
    assert snapshot(env).exit_code == 4  # type: ignore[attr-defined]
    assert not env.output.exists() and not list(env.output.parent.glob("*.staging-*"))
    assert snapshot(env, "--allow-oversize").exit_code == 0  # type: ignore[attr-defined]


def test_exit_5_dem_unavailable(env: DemoEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    offline_terrain(monkeypatch, dem_ok=False)
    assert snapshot(env).exit_code == 5  # type: ignore[attr-defined]
    assert not env.output.exists() and not list(env.output.parent.glob("*.staging-*"))


def test_failed_run_is_audited_by_state(env: DemoEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    offline_terrain(monkeypatch, dem_ok=False)
    snapshot(env)
    records = [json.loads(line) for line in (env.state / "audit.jsonl").read_text().splitlines()]
    mine = [r for r in records if r["action"] == "demo_snapshot"]
    assert mine and mine[-1]["outcome"] == "error"
    assert mine[-1]["params"]["state"].startswith("failed:")


def test_gate_command(env: DemoEnv) -> None:
    assert snapshot(env).exit_code == 0  # type: ignore[attr-defined]
    ok = run_cli("demo", "gate", str(env.output), "--config", str(env.config))
    assert ok.exit_code == 0 and "0 fatal" in ok.output
    conn = sqlite3.connect(env.output / "state" / "obi" / "logs.db")
    conn.execute("UPDATE logs SET message = 'peer 10.99.0.1' WHERE id = 1")
    conn.commit()
    conn.close()
    bad = run_cli("demo", "gate", str(env.output), "--config", str(env.config))
    assert bad.exit_code == 3 and "10.99.0.1" not in bad.output


def test_lookup_both_directions_and_never_audits_the_value(env: DemoEnv) -> None:
    assert snapshot(env).exit_code == 0  # type: ignore[attr-defined]
    forward = run_cli("demo", "lookup", "northfield", "--reverse", "--config", str(env.config))
    assert forward.exit_code == 0
    fake = forward.output.split("place: ")[1].split()[0]
    back = run_cli("demo", "lookup", fake, "--config", str(env.config))
    assert back.exit_code == 0 and "northfield" in back.output
    audit = (env.state / "audit.jsonl").read_text()
    assert "demo_lookup" in audit and fake not in audit and "northfield" not in audit


def test_time_fidelity(env: DemoEnv) -> None:
    assert snapshot(env).exit_code == 0  # type: ignore[attr-defined]
    tz = ZoneInfo("America/Los_Angeles")
    live = sqlite3.connect(env.state / "logs.db")
    out = sqlite3.connect(env.output / "state" / "obi" / "logs.db")
    query = "SELECT min(ts_us), max(ts_us) FROM logs WHERE host LIKE '%-ap'"
    (a0, a1), (b0, b1) = live.execute(query).fetchone(), out.execute(query).fetchone()
    assert b1 - b0 == a1 - a0  # span preserved
    real = datetime.fromtimestamp(a0 / 1e6, tz)
    fake = datetime.fromtimestamp(b0 / 1e6, tz)
    assert (fake.weekday(), fake.hour, fake.minute) == (real.weekday(), real.hour, real.minute)
    assert fake.year == 2025 and fake.month == 6
