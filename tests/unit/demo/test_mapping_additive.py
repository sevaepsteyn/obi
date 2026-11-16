"""Additive 137 mapping keys: curated callsigns, the fixture longitude offset, collision pairs."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obi.core.demo.mapping import PseudonymMap


def test_new_keys_round_trip(tmp_path: Path) -> None:
    m = PseudonymMap(b"\x05" * 32)
    m.set_callsign("Zephyrine", "Kestrel")
    m.fixture_dlon = 4.25
    m.pairs.append(["northfield", "northfieldcom"])
    path = tmp_path / "mapping.json"
    m.save(path)
    loaded = PseudonymMap.load_or_create(path)
    assert loaded.callsign("zephyrine") == "Kestrel"
    assert loaded.fixture_dlon == 4.25
    assert loaded.pairs == [["northfield", "northfieldcom"]]


def test_old_mapping_without_new_keys_loads(tmp_path: Path) -> None:
    m = PseudonymMap(b"\x05" * 32)
    m.place("northfield")
    data = m.to_dict()
    data["classes"].pop("callsign")
    data.pop("fixtures")
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    loaded = PseudonymMap.load_or_create(path)
    assert loaded.peek_place("northfield") == m.peek_place("northfield")
    assert loaded.fixture_dlon is None and loaded.pairs == []


def test_callsigns_are_curated_never_generated() -> None:
    m = PseudonymMap(b"\x05" * 32)
    with pytest.raises(KeyError):
        m.callsign("Marlowe")
    assert m.assignments("callsign") == {}


def test_callsign_fake_must_be_one_token_and_unique() -> None:
    m = PseudonymMap(b"\x05" * 32)
    m.set_callsign("Zephyrine", "Kestrel")
    with pytest.raises(ValueError):
        m.set_callsign("Marlowe", "Kestrel")  # already used for another real name
    with pytest.raises(ValueError):
        m.set_callsign("Marlowe", "Two Words")


def test_snapshot_geo_overwrite_keeps_fixture_offset() -> None:
    m = PseudonymMap(b"\x05" * 32)
    m.fixture_dlon = 5.5
    m.geo = {"centroid": [1.0, 2.0]}  # what 134's prepare() does on every snapshot run
    assert m.to_dict()["fixtures"]["dlon"] == 5.5
