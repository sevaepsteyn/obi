"""Unit tests for the private PseudonymMap (feature 134, T010)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from obi.core.demo import ProfileError
from obi.core.demo.mapping import PseudonymMap, normalize_mac
from obi.core.demo.naming import TREES, within_one_edit

SEED = b"\x07" * 32


def test_one_real_value_one_fake_per_class_and_unique() -> None:
    m = PseudonymMap(SEED)
    a = m.place("northfield")
    assert m.place("Northfield") == a
    others = {m.place(w) for w in ("southgate", "eastmoor", "westbrook", "lakeview")}
    assert a not in others and len(others) == 4


def test_no_fake_equals_or_nearly_equals_a_real_token() -> None:
    m = PseudonymMap(SEED)
    m.forbid({"aspen", "birch", "cedarr"})
    labels = [m.place(f"real{i}") for i in range(len(TREES))]
    for label in labels:
        assert label not in {"aspen", "birch"}
        assert not within_one_edit(label, "cedarr")


def test_macs_are_locally_administered_and_spelling_agnostic() -> None:
    m = PseudonymMap(SEED)
    a = m.mac("AA:BB:CC:00:11:22")
    assert a == m.mac("aa-bb-cc-00-11-22") == m.mac("aabb.cc00.1122")
    assert int(a[:2], 16) & 0b10 and not int(a[:2], 16) & 0b1
    assert normalize_mac("not a mac") is None


def test_serial_format() -> None:
    m = PseudonymMap(SEED)
    s = m.serial("HE90812ABCD")
    assert len(s) == 12 and s == s.upper()
    assert all(c in "0123456789ABCDEF" for c in s)


def test_people_and_nodes_get_generic_labels() -> None:
    m = PseudonymMap(SEED)
    assert m.person("U0FAKE0001") == "admin"
    assert m.person("someone") == "user-1"
    assert m.node("!deadbeef") == "node-01"


def test_persist_round_trip_owner_only_and_append_only(tmp_path: Path) -> None:
    path = tmp_path / "demo" / "mapping.json"
    m = PseudonymMap(SEED)
    first = m.place("northfield")
    ip = m.ip.map_ip("10.99.5.1")
    m.save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    again = PseudonymMap.load_or_create(path)
    assert again.place("northfield") == first
    assert again.ip.map_ip("10.99.5.1") == ip
    new = again.place("southgate")
    assert new != first
    assert again.reverse(first) == [("place", "northfield")]


def test_group_readable_mapping_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    PseudonymMap(SEED).save(path)
    path.chmod(0o644)
    with pytest.raises(ProfileError):
        PseudonymMap.load_or_create(path)
