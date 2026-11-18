"""The export pipeline: budget, whole-bundle gate, digest, audit (feature 136, T022)."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from obi.core.audit import AuditTrail
from obi.core.demo import BudgetError, LeakGateError, ProfileError
from obi.core.demo import export as export_mod
from obi.core.demo.export import bundle_digest, digest_path, load_export_profile, run_export
from tests.fixtures.demo.harness import build_dataset, offline_terrain

# Runs the demo snapshot/export pipeline (seconds per test) — skip with -m 'not slow'.
pytestmark = pytest.mark.slow


@dataclass
class Env:
    root: Path
    dataset: Path
    config: Path
    raw: dict  # type: ignore[type-arg]

    def profile(self, **export: object):  # type: ignore[no-untyped-def]
        table = {"bundle_dir": str(self.root / "bundle"), **export}
        return load_export_profile(
            {"demo": {"output_dir": str(self.dataset), "export": table}}, config_path=self.config
        )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    dataset, config, _truth = build_dataset(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    offline_terrain(monkeypatch)
    return Env(tmp_path, dataset, config, tomllib.loads(config.read_text()))


def _audit(env: Env) -> list[dict]:  # type: ignore[type-arg]
    lines = (env.root / "xdg" / "obi" / "audit.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines if '"demo_export"' in x]


def test_happy_path_publishes_a_gated_bundle_with_private_digest(env: Env) -> None:
    trail = AuditTrail(env.root / "xdg" / "obi" / "audit.jsonl")
    result = run_export(env.raw, env.config, env.profile(), audit=trail)
    bundle = env.root / "bundle"
    assert result.state == "audited" and bundle.is_dir()
    assert result.gate is not None and result.gate.fatal == []
    assert not list(env.root.glob("bundle.staging-*"))
    record = json.loads(digest_path().read_text())
    assert record["digest"] == bundle_digest(bundle)
    assert oct(digest_path().stat().st_mode)[-3:] == "600"
    assert _audit(env)[-1]["outcome"] == "ok"
    manifest = json.loads((bundle / "data" / "manifest.json").read_text())
    assert manifest["demo_now"].startswith("2025-06-0")  # the dataset's shifted window end
    assert manifest["routes"]["phy"] == "unavailable"


def test_injected_leak_is_refused_and_nothing_remains(env: Env) -> None:
    def plant(staging: Path) -> None:
        path = staging / "data" / "api" / "dash" / "index.json"
        path.write_text(path.read_text().replace("}", ', "x": "10.99.0.1"}', 1))

    trail = AuditTrail(env.root / "xdg" / "obi" / "audit.jsonl")
    with pytest.raises(LeakGateError) as caught:
        run_export(env.raw, env.config, env.profile(), audit=trail, post_build_hook=plant)
    assert "10.99.0.1" not in str(caught.value) and "ip/ip" in str(caught.value)
    assert not (env.root / "bundle").exists() and not list(env.root.glob("bundle.staging-*"))
    assert _audit(env)[-1]["params"]["state"] == "failed:budget_ok"


def test_budget_and_per_file_limits(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(BudgetError):
        run_export(env.raw, env.config, env.profile(max_bundle_mb=1))
    assert not (env.root / "bundle").exists()
    assert run_export(env.raw, env.config, env.profile(max_bundle_mb=1),
                      allow_oversize=True).state == "audited"  # fmt: skip
    monkeypatch.setattr(export_mod, "MAX_FILE_BYTES", 10_000)
    with pytest.raises(BudgetError):
        run_export(env.raw, env.config, env.profile(bundle_dir=str(env.root / "b2")),
                   allow_oversize=True)  # fmt: skip


def test_existing_bundle_is_refused(env: Env) -> None:
    (env.root / "bundle").mkdir()
    with pytest.raises(ProfileError):
        run_export(env.raw, env.config, env.profile())
