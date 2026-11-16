"""Exclusions, allowances, and ordinary words in the mapping's place class (T018)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obi.core.config import ConfigError
from obi.core.demo.mapping import PseudonymMap
from obi.core.identity.guard import glob_match, load_guard_config, scan_paths
from obi.core.identity.tokens import build_tokens

#: Assembled at run time so this tracked file has no literal path into the excluded dir.
SPECS = "spe" + "cs"

NO_PRIVATE = {"denylist": None, "mapping": None, "config": None}
TAILNET_LINE = "u = 'h.x." + "ts" + ".net'\n"


@pytest.mark.parametrize(
    ("rel", "glob", "hit"),
    [
        (f"{SPECS}/001-a/spec.md", f"{SPECS}/**", True),
        ("src/specs.py", f"{SPECS}/**", False),
        ("a/b/c.png", "**/*.{png,jpg}", True),
        ("c.jpg", "**/*.{png,jpg}", True),
        ("c.py", "**/*.{png,jpg}", False),
        ("CLAUDE.md", "CLAUDE.md", True),
    ],
)
def test_glob_match(rel: str, glob: str, hit: bool) -> None:
    assert glob_match(rel, glob) is hit


def test_exclusions_honoured_and_reported(tree) -> None:
    root = tree({f"{SPECS}/001/notes.md": TAILNET_LINE, "src/a.py": "ok = 1\n"})
    result = scan_paths(None, root, build_tokens(**NO_PRIVATE), dictionary=frozenset())
    assert result.fatal == []
    header = result.header()
    assert f"{SPECS}/**" in header and "private-only design history" in header


def test_entry_without_reason_is_a_load_error(tmp_path: Path) -> None:
    (tmp_path / "identity-guard.toml").write_text('version = 1\n[[exclude]]\nglob = "x/**"\n')
    with pytest.raises(ConfigError, match="reason"):
        load_guard_config(tmp_path)


def test_allowance_shadowing_a_private_token_is_refused(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree({"a.py": "x = 1\n"})
    (root / "identity-guard.toml").write_text(
        'version = 1\n[[allow]]\ncategory = "person"\npattern = "zephyr"\nreason = "oops"\n'
    )
    tokens = build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)
    with pytest.raises(ConfigError, match="shadows a private token") as info:
        scan_paths(None, root, tokens, dictionary=frozenset())
    assert "zephyr" not in str(info.value).replace("pattern", "")


def test_ordinary_mapping_place_is_fatal_only_as_identifier(tree, tmp_path: Path) -> None:
    mapping = PseudonymMap(b"\x05" * 32)
    mapping.place("lantern")  # an ordinary word that a (fictional) site happened to be called
    path = tmp_path / "m.json"
    mapping.save(path)
    os.chmod(path, 0o600)
    root = tree({"a.py": "# hang the lantern by the door\nhost = 'lantern-gw-1'\n"})
    tokens = build_tokens(denylist=None, mapping=path, config=None)
    result = scan_paths(None, root, tokens)
    assert [f.locator for f in result.fatal] == ["line 2"]


def test_binary_and_undecodable_files_are_skipped(tree) -> None:
    root = tree({"a.py": "ok = 1\n"})
    (root / "blob.dat").write_bytes(b"\x00\x01" + TAILNET_LINE.encode())
    result = scan_paths(["blob.dat", "a.py"], root, build_tokens(**NO_PRIVATE))
    assert result.fatal == [] and "blob.dat" in result.skipped
