"""`obi identity scrub` + `obi identity callsign` (T054/T056): private files via XDG, no values."""

from __future__ import annotations

import os
import shutil

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.demo.mapping import PseudonymMap
from tests.fixtures.identity_env import (  # noqa: F401 - pytest fixtures
    fake_config,
    fake_denylist,
    fake_mapping,
    tree,
)


@pytest.fixture
def private_env(tmp_path, monkeypatch, fake_denylist, fake_mapping, fake_config):  # noqa: F811
    cfg = tmp_path / "xdg-config" / "obi"
    state = tmp_path / "xdg-state2" / "obi" / "demo"
    cfg.mkdir(parents=True)
    state.mkdir(parents=True)
    for src, dst in ((fake_denylist, cfg / "demo-denylist.txt"), (fake_config, cfg / "config.toml"),
                     (fake_mapping, state / "mapping.json")):  # fmt: skip
        shutil.copy(src, dst)
        os.chmod(dst, 0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(state.parent.parent))
    return state / "mapping.json"


def run(*args: str):
    return CliRunner().invoke(app, ["identity", *args])


def test_scrub_rewrites_and_reports_without_values(tree, private_env, monkeypatch) -> None:  # noqa: F811
    root = tree({"fx/a.json": '{"identity": "northfield-gw-01", "address": "10.99.4.7"}\n'})
    monkeypatch.chdir(root)
    dry = run("scrub", "fx", "--dry-run")
    assert dry.exit_code == 0 and "northfield" not in dry.output
    assert "northfield" in (root / "fx/a.json").read_text()
    result = run("scrub", "fx")
    assert result.exit_code == 0, result.output
    assert "northfield" not in (root / "fx/a.json").read_text()
    assert "replaced" in result.output and "10.99" not in result.output


def test_init_geo_assigns_once(tree, private_env, monkeypatch) -> None:  # noqa: F811
    monkeypatch.chdir(tree({"a.txt": "x\n"}))
    assert "assigned" in run("scrub", "--init-geo").output
    first = PseudonymMap.load_or_create(private_env).fixture_dlon
    assert "already set" in run("scrub", "--init-geo").output
    assert PseudonymMap.load_or_create(private_env).fixture_dlon == first


def test_callsign_list_set_check(tree, private_env, monkeypatch) -> None:  # noqa: F811
    monkeypatch.chdir(tree({"a.txt": "x\n"}))
    listing = run("callsign", "list")
    assert listing.exit_code == 0
    assert "Zephyrine" not in listing.output and "zephyrine" not in listing.output
    refs = [line.split()[0] for line in listing.output.splitlines() if line.strip()]
    assert run("callsign", "check").exit_code == 1  # nothing curated yet
    fakes = iter(["Kestrel", "Harlowe", "Wrenna", "Brambles", "Corvid", "Larchly", "Fennick"])
    for ref in refs:
        assert run("callsign", "set", ref, next(fakes)).exit_code == 0
    check = run("callsign", "check")
    assert "0 not yet" in check.output
