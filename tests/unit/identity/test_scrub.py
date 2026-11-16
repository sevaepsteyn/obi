"""The token-exact scrubber (feature 137, T052) on FICTIONAL private inputs."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from obi.core.demo.mapping import PseudonymMap
from obi.core.identity.scrub import ScrubError, archive_dir, init_fixture_offset, scrub_paths
from obi.core.identity.tokens import build_tokens
from tests.fixtures.identity_env import GLOBAL_MAC

DLON = 5.0


def _tokens(fake_denylist: Path, fake_mapping: Path, fake_config: Path, *, curate: bool = True):
    mapping = PseudonymMap.load_or_create(fake_mapping)
    mapping.fixture_dlon = DLON
    if curate:
        mapping.set_callsign("Zephyrine", "Kestrel")
        mapping.set_callsign("Marlowe", "Harlowe")
        mapping.set_callsign("Quillon", "Wrenna")
        mapping.set_callsign("Tumbleweed 4411", "Brambles")
    mapping.save(fake_mapping)
    return build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)


FIXTURE = {
    "fx/router.json": (
        '{\n  "identity": "northfield-gw-01",\n  "iface": "vlan203-peer-northfield-gw-01",\n'
        '  "address": "10.99.4.7/24", "other": "10.99.4.70",\n'
        f'  "mac": "{GLOBAL_MAC}",\n  "site": "Northfield",\n'
        '  "latitude": 45.0123,\n  "longitude": -100.0456\n}\n'
    ),
    "tests/test_radio.py": 'CALLERS = ["Zephyrine", "zephyrine"]  # Zephyrine on air\n',
}


def test_scrub_replaces_every_occurrence_and_archives(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree(FIXTURE)
    tokens = _tokens(fake_denylist, fake_mapping, fake_config)
    result = scrub_paths(["fx", "tests"], root, tokens, mapping_path=fake_mapping)
    assert result.failed == [], result.report()
    data = json.loads((root / "fx/router.json").read_text())
    fake_site = PseudonymMap.load_or_create(fake_mapping).peek_place("northfield")
    assert data["identity"] == f"{fake_site}-gw-01"
    assert data["iface"] == f"vlan203-peer-{fake_site}-gw-01"
    assert data["site"] == fake_site.capitalize()
    assert data["address"].endswith(".4.7/24") or data["address"].split(".")[3] == "7/24"
    assert data["other"].endswith(".70") and not data["other"].startswith("10.99.")
    assert data["mac"].startswith("02:")
    assert data["latitude"] == 45.0123 and data["longitude"] == pytest.approx(-100.0456 + DLON)
    radio = (root / "tests/test_radio.py").read_text()
    assert radio == 'CALLERS = ["Kestrel", "kestrel"]  # Kestrel on air\n'
    archived = archive_dir() / "fx/router.json"
    assert archived.read_text() == FIXTURE["fx/router.json"]
    assert stat.S_IMODE(archived.stat().st_mode) == 0o600
    text = result.report()
    for needle in ("northfield", "10.99.4.7", "Zephyrine", "45.0123"):
        assert needle not in text


def test_second_run_never_overwrites_the_archive(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree(FIXTURE)
    tokens = _tokens(fake_denylist, fake_mapping, fake_config)
    scrub_paths(["fx"], root, tokens)
    (root / "fx/router.json").write_text(FIXTURE["fx/router.json"].replace("4.7", "4.8"))
    scrub_paths(["fx"], root, tokens)
    assert (archive_dir() / "fx/router.json").read_text() == FIXTURE["fx/router.json"]


def test_dry_run_writes_nothing(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree(FIXTURE)
    tokens = _tokens(fake_denylist, fake_mapping, fake_config)
    result = scrub_paths(["fx"], root, tokens, dry_run=True)
    assert (root / "fx/router.json").read_text() == FIXTURE["fx/router.json"]
    assert result.files[0].replaced and not (archive_dir() / "fx/router.json").exists()


def test_uncurated_callsign_fails_the_file(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree({"tests/test_radio.py": FIXTURE["tests/test_radio.py"]})
    tokens = _tokens(fake_denylist, fake_mapping, fake_config, curate=False)
    result = scrub_paths(["tests"], root, tokens)
    assert result.failed and result.failed[0].uncurated_callsigns == 1
    assert "callsign set" in result.report() and "Zephyrine" not in result.report()


def test_no_mapping_is_an_error(tree) -> None:
    root = tree({"a.txt": "x\n"})
    tokens = build_tokens(denylist=None, mapping=None, config=None)
    with pytest.raises(ScrubError):
        scrub_paths(["a.txt"], root, tokens)


def test_fixture_offset_is_assigned_once() -> None:
    mapping = PseudonymMap(b"\x05" * 32)
    first = init_fixture_offset(mapping)
    assert 3.0 <= abs(first) <= 10.0
    assert init_fixture_offset(mapping) == first


def test_archive_dirs_are_owner_only(
    tree, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    root = tree(FIXTURE)
    scrub_paths(["fx"], root, _tokens(fake_denylist, fake_mapping, fake_config))
    assert stat.S_IMODE(os.stat(archive_dir() / "fx").st_mode) == 0o700
