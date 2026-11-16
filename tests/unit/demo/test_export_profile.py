"""ExportProfile (feature 136, T008)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.demo import ProfileError
from obi.core.demo.export import load_export_profile


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = tmp_path / "cfg" / "config.toml"
    path.parent.mkdir()
    path.write_text("")
    return path


def test_defaults_follow_the_134_dataset(cfg: Path, tmp_path: Path) -> None:
    raw = {"demo": {"output_dir": str(tmp_path / "ds"),
                    "export": {"bundle_dir": str(tmp_path / "bundle")}}}  # fmt: skip
    p = load_export_profile(raw, config_path=cfg)
    assert p.dataset == tmp_path / "ds"
    assert (p.base_path, p.logs_slice_rows, p.audit_slice_rows, p.max_bundle_mb) == (
        "/obi/",
        20_000,
        5_000,
        500,
    )
    assert p.pages_remote is None and p.pages_branch == "gh-pages"


def test_publish_targets(cfg: Path, tmp_path: Path) -> None:
    raw = {
        "demo": {
            "export": {"bundle_dir": str(tmp_path / "b")},
            "publish": {"pages_remote": "git@example.net:obi.git", "vm_target": "u@h:/x/"},
        }
    }
    p = load_export_profile(raw, config_path=cfg)  # fmt: skip
    assert p.pages_remote == "git@example.net:obi.git" and p.vm_target == "u@h:/x/"


@pytest.mark.parametrize("inside", ["state", "config", "dataset"])
def test_bundle_dir_must_not_sit_inside_private_dirs(
    cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: str
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    roots = {"state": tmp_path / "xdg" / "obi", "config": cfg.parent, "dataset": tmp_path / "ds"}
    raw = {"demo": {"output_dir": str(tmp_path / "ds"),
                    "export": {"bundle_dir": str(roots[inside] / "bundle")}}}  # fmt: skip
    with pytest.raises(ProfileError):
        load_export_profile(raw, config_path=cfg)


@pytest.mark.parametrize("base", ["obi/", "/obi", "//", "/a//b/"])
def test_base_path_shape(cfg: Path, tmp_path: Path, base: str) -> None:
    raw = {"demo": {"export": {"bundle_dir": str(tmp_path / "b"), "base_path": base}}}
    with pytest.raises(ProfileError):
        load_export_profile(raw, config_path=cfg)
