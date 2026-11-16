"""The private identifier set the guard and scrubber share (T010)."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path

import pytest

from obi.core.config import ConfigError
from obi.core.identity.tokens import build_tokens


def test_all_three_inputs(fake_denylist: Path, fake_mapping: Path, fake_config: Path) -> None:
    t = build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)
    assert t.inputs == {"denylist": "loaded", "mapping": "loaded", "config": "loaded"}
    h = t.harvest
    assert h.event_terms["quillon"] is True and h.event_terms["playa"] is False
    assert "northfield" in h.place_words  # mapping place + denylist @
    assert "quillon mae" in h.persons  # mapping person
    assert "zephyrine" in h.persons  # config radio caller
    assert "QX12345678" in h.serials
    assert ipaddress.ip_address("10.99.4.7") in t.real_blocks[0]
    assert t.centroid == (45.0, -100.0)
    assert t.mapping is not None


def test_missing_inputs_are_skipped_not_raised(tmp_path: Path) -> None:
    t = build_tokens(
        denylist=tmp_path / "nope.txt", mapping=tmp_path / "nope.json", config=tmp_path / "no.toml"
    )
    assert set(t.inputs.values()) == {"skipped (absent)"}
    assert t.harvest.token_count() == 0 and t.real_blocks == [] and t.centroid is None


def test_disabled_inputs(fake_denylist: Path, fake_mapping: Path, fake_config: Path) -> None:
    t = build_tokens(denylist=None, mapping=None, config=None)
    assert set(t.inputs.values()) == {"skipped (disabled)"}


@pytest.mark.parametrize("which", ["denylist", "mapping", "config"])
def test_world_readable_private_file_is_refused(
    which: str, fake_denylist: Path, fake_mapping: Path, fake_config: Path
) -> None:
    path = {"denylist": fake_denylist, "mapping": fake_mapping, "config": fake_config}[which]
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match="owner-only"):
        build_tokens(denylist=fake_denylist, mapping=fake_mapping, config=fake_config)
