"""Bundle assets (feature 136, T020)."""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

from obi.core.demo.export import DEMO_BOOT, LIVE_BOOT, copy_assets, load_dataset
from obi.web.server import STATIC_DIR
from tests.fixtures.demo.harness import build_dataset

# Runs the demo snapshot/export pipeline (seconds per test) — skip with -m 'not slow'.
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("assets")
    dataset, _config, _truth = build_dataset(root)
    out = root / "bundle"
    copy_assets(load_dataset(dataset), out)
    return out


def test_static_files_are_byte_identical_except_index(bundle: Path) -> None:
    for src in STATIC_DIR.rglob("*"):
        if src.is_file() and "__pycache__" not in src.parts and src.name != "index.html":
            rel = src.relative_to(STATIC_DIR)
            assert filecmp.cmp(src, bundle / rel, shallow=False), rel
    assert (bundle / "demo" / "boot.js").exists()


def test_index_swaps_exactly_the_boot_block(bundle: Path) -> None:
    live = (STATIC_DIR / "index.html").read_text()
    demo = (bundle / "index.html").read_text()
    assert LIVE_BOOT not in demo and demo.count(DEMO_BOOT) == 1
    assert demo == live.replace(LIVE_BOOT, DEMO_BOOT)


def test_dataset_artifacts_and_nojekyll(bundle: Path) -> None:
    assert (bundle / "links.json").exists() and (bundle / "terrain.json").exists()
    assert (bundle / "data" / "snapshots" / "ospf.json").exists()
    assert (bundle / ".nojekyll").exists()
    assert not (bundle / "brc.json").exists()
