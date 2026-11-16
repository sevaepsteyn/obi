"""Synthetic private inputs for the identity tooling tests (feature 137, T003).

Every "real" value here is FICTIONAL, borrowed from the 134 fixtures: ``quillon`` (a fake person),
``northfield`` (a fake site word), ``10.99.0.0/16`` (the fixture network), a 45.0/-100.0 centroid,
and ``3c:22:fb`` MACs (a globally-administered, unassigned-looking OUI). Never put a value from the
live deployment in this file — the in-suite identity guard would flag it.

Shape plants (a tailnet host, a chat ID, a globally-administered MAC) are ASSEMBLED at run time so
this tracked file does not itself match the guard's shape checks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.core.demo.mapping import PseudonymMap

FIXTURE_TOML = Path(__file__).resolve().parent / "demo" / "fixture.toml"

TAILNET = "northfield-hub." + "ts" + ".net"
CHAT_ID = "U0" + "QZXW12345"
GLOBAL_MAC = ":".join(["3c", "22", "fb", "00", "00", "01"])

#: Extra fictional lines inserted under existing fixture.toml tables (TOML forbids re-declaring a
#: table), plus wholly new tables appended at the end.
INSERTS = {
    "[radio]\n": 'operator_handles = ["Quillon"]\nknown_callers = ["Zephyrine", "Marlowe"]\n',
    "[meshtastic.names]\n": '"!0fa4e002" = "Tumbleweed 4411"\n',
}
APPEND = """
[paging.slack]
recipients = ["{chat_id}"]

[wled.devices]
gate = "https://lights.{tailnet}"
"""


def _write_private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def fake_denylist(tmp_path: Path) -> Path:
    return _write_private(
        tmp_path / "private" / "denylist.txt", "# fictional\nplaya\n!quillon\n@northfield\n"
    )


@pytest.fixture
def fake_mapping(tmp_path: Path) -> Path:
    mapping = PseudonymMap(b"\x05" * 32)
    mapping.place("northfield")
    mapping.person("quillon mae")
    mapping.mac(GLOBAL_MAC)
    mapping.serial("QX12345678")
    mapping.ip.map_ip("10.99.4.1")
    mapping.geo = {"centroid": [45.0, -100.0], "anchor": [37.16, -118.27], "rotation_deg": 0.0}
    path = tmp_path / "private" / "mapping.json"
    mapping.save(path)
    return path


@pytest.fixture
def fake_config(tmp_path: Path) -> Path:
    text = FIXTURE_TOML.read_text(encoding="utf-8")
    for header, lines in INSERTS.items():
        text = text.replace(header, header + lines, 1)
    text = text.replace('known_callers = ["Quillon"]\n', "", 1)  # superseded by the insert
    text += APPEND.format(chat_id=CHAT_ID, tailnet=TAILNET)
    return _write_private(tmp_path / "private" / "config.toml", text)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def tree(tmp_path: Path):
    """``tree({"rel/path": "text"})`` → a committed tmp git repo (with this repo's guard config)."""

    def make(files: dict[str, str], *, guard_config: bool = True) -> Path:
        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        git(root, "init", "-q")
        git(root, "config", "user.email", "t@example.net")
        git(root, "config", "user.name", "t")
        if guard_config:
            shutil.copy(Path(__file__).resolve().parents[2] / "identity-guard.toml", root)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "fixture")
        return root

    return make
