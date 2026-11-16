"""Shared harness for the demo-sanitizer tests (feature 134): offline terrain, a synthetic
environment, the CLI runner, and an INDEPENDENT full-text dump of a dataset (SC-002 — it shares
no code with the gate)."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from obi.cli.main import app
from tests.fixtures.ar.terrarium_helpers import flat_tile_bytes
from tests.fixtures.demo.build_state import SyntheticTruth, build_state, write_config

GROUND_M = 1201.0


def offline_terrain(monkeypatch: pytest.MonkeyPatch, *, dem_ok: bool = True) -> None:
    """Replace the three network fetchers (tiles, EPQS, GNIS) with offline fakes."""

    tile = flat_tile_bytes(GROUND_M - 10)
    monkeypatch.setattr(
        "obi.core.terrain._default_tile_fetcher",
        lambda timeout: (lambda z, x, y: tile) if dem_ok else (lambda z, x, y: None),
    )
    monkeypatch.setattr(
        "obi.core.elevation._default_fetcher",
        lambda timeout: (lambda lat, lon: GROUND_M) if dem_ok else (lambda lat, lon: None),
    )
    monkeypatch.setattr("obi.core.gnis._default_fetcher", lambda timeout: lambda state: None)


@dataclass
class DemoEnv:
    root: Path
    state_root: Path
    config: Path
    output: Path
    truth: SyntheticTruth

    @property
    def state(self) -> Path:
        return self.state_root / "obi"

    @property
    def mapping(self) -> Path:
        return self.state / "demo" / "mapping.json"


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoEnv:
    state_root = tmp_path / "xdg-state"
    truth = build_state(state_root)
    output = tmp_path / "out" / "dataset"
    config = write_config(tmp_path / "cfg" / "config.toml", output_dir=output)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    offline_terrain(monkeypatch)
    return DemoEnv(tmp_path, state_root, config, output, truth)


def run_cli(*args: str) -> Result:
    return CliRunner().invoke(app, list(args), catch_exceptions=False)


def dump_text(root: Path) -> str:
    """Every byte of text a dataset holds: SQL dumps, every git object, every other file."""

    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        with path.open("rb") as handle:
            head = handle.read(16)
        if head.startswith(b"SQLite format 3"):
            conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
            parts.extend(conn.iterdump())
            conn.close()
        else:
            parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
    for git_dir in sorted(root.rglob(".git")):
        objects = subprocess.run(
            ["git", "-C", str(git_dir.parent), "cat-file", "--batch-all-objects", "--batch"],
            check=True,
            capture_output=True,
        ).stdout
        parts.append(objects.decode("utf-8", errors="ignore"))
        for path in git_dir.rglob("*"):
            if path.is_file() and "objects" not in path.relative_to(git_dir).parts:
                parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def tree_state(root: Path) -> dict[str, int]:
    """File → mtime (ns) for a live-state-untouched comparison."""

    return {
        str(p.relative_to(root)): p.stat().st_mtime_ns
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def build_dataset(root: Path) -> tuple[Path, Path, SyntheticTruth]:
    """Run the 134 snapshot over the synthetic state → (dataset dir, config path, truth).

    Isolated with its own MonkeyPatch so module-scoped fixtures can call it.
    """

    import tomllib

    from obi.core.demo.profile import load_profile
    from obi.core.demo.snapshot import run_snapshot

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_STATE_HOME", str(root / "xdg"))
        mp.setenv("XDG_CACHE_HOME", str(root / "cache"))
        offline_terrain(mp)
        truth = build_state(root / "xdg")
        dataset = root / "out" / "dataset"
        config = write_config(root / "cfg" / "config.toml", output_dir=dataset)
        raw = tomllib.loads(config.read_text())
        run_snapshot(raw, load_profile(raw, config_path=config))
    return dataset, config, truth
