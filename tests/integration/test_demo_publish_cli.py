"""`obi demo publish` end to end against a local bare repository (feature 136, T041)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.fixtures.demo.harness import build_dataset, offline_terrain, run_cli


def test_export_then_publish_pages_and_refuse_after_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset, config, _truth = build_dataset(tmp_path)
    bare = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    config.write_text(config.read_text() + f'\n[demo.publish]\npages_remote = "{bare}"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    offline_terrain(monkeypatch)
    bundle = tmp_path / "bundle"
    common = ["--config", str(config)]
    built = run_cli("demo", "export", *common, "--dataset", str(dataset), "--out", str(bundle))
    assert built.exit_code == 0, built.output

    dry = run_cli("demo", "publish", "--pages", "--dry-run", "--bundle", str(bundle), *common)
    assert dry.exit_code == 0 and "would send" in dry.output
    assert subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "-q", "gh-pages"],
                          capture_output=True).returncode != 0  # fmt: skip

    sent = run_cli("demo", "publish", "--pages", "--bundle", str(bundle), *common)
    assert sent.exit_code == 0, sent.output
    files = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "gh-pages"],
                           capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    assert "index.html" in files and "data/manifest.json" in files and ".nojekyll" in files

    assert run_cli("demo", "publish", "--vm", "--bundle", str(bundle), *common).exit_code == 2
    (bundle / "index.html").write_text("tampered")
    refused = run_cli("demo", "publish", "--pages", "--bundle", str(bundle), *common)
    assert refused.exit_code == 3
