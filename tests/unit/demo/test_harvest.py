"""Unit tests for the identifier harvest (feature 134, T016)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from obi.core.demo.harvest import Harvest, harvest
from tests.fixtures.demo.build_state import (
    BUSY_END,
    BUSY_START,
    FIXTURE_DIR,
    SyntheticTruth,
    build_state,
)

WORDS = frozenset({"orchard", "relay", "trailer", "camp", "handheld"})


@pytest.fixture(scope="module")
def harvested(tmp_path_factory: pytest.TempPathFactory) -> tuple[Harvest, SyntheticTruth]:
    root = tmp_path_factory.mktemp("state")
    truth = build_state(root)
    raw = tomllib.loads((FIXTURE_DIR / "fixture.toml").read_text())
    window = (int(BUSY_START.timestamp() * 1e6), int(BUSY_END.timestamp() * 1e6))
    return harvest(raw, root / "obi", window, FIXTURE_DIR / "denylist.txt", WORDS), truth


def test_every_planted_address_harvested(harvested: tuple[Harvest, SyntheticTruth]) -> None:
    h, truth = harvested
    # Free-text-only addresses (a DHCP lease, the public uplink in a log line) are not harvested:
    # the gate's IP allowlist catches any stray address regardless of the harvest.
    assert truth.ips - {"10.99.1.57", "8.8.4.1"} <= h.ips
    assert "10.99.0.0/16" in h.networks


def test_place_words_classified(harvested: tuple[Harvest, SyntheticTruth]) -> None:
    h, truth = harvested
    for word in truth.distinctive_words:
        assert h.place_words[word] is False
    assert h.place_words["orchard"] is True
    assert "gw" not in h.place_words and "core" not in h.place_words


def test_identity_families(harvested: tuple[Harvest, SyntheticTruth]) -> None:
    h, truth = harvested
    assert truth.serials <= h.serials
    assert truth.slack_ids <= h.slack_ids
    assert "quillon" in h.persons
    assert truth.nodes <= h.node_ids
    assert h.node_names["quillon handheld"] == "!0fa4e001"
    assert "fictional-org.test" in h.domains
    assert h.event_terms.get("night crew") is False  # a roster GROUP is org vocabulary


def test_secrets_only_in_secret_category(harvested: tuple[Harvest, SyntheticTruth]) -> None:
    h, _truth = harvested
    assert {"fakepass99", "fakecommunity", "fake-uisp-token-123", "fakepagerpw"} <= h.secrets
    assert "fakepass99" not in h.place_words and "fakecommunity" not in h.names


def test_event_terms_and_severity(harvested: tuple[Harvest, SyntheticTruth]) -> None:
    h, _truth = harvested
    assert h.event_terms["fictional camp"] is False
    assert h.event_terms["imaginary sculpture"] is False
    assert h.event_terms["playa"] is False
    assert h.event_terms["quillon"] is True


def test_coordinates_collected(harvested: tuple[Harvest, SyntheticTruth]) -> None:
    h, _truth = harvested
    assert h.site_coords and all(44 < lat < 46 for lat, _ in h.site_coords)
    assert (45.0123, -100.0456) in h.coords


def test_excluded_dirs_are_read_only(tmp_path: Path) -> None:
    root = tmp_path / "s"
    build_state(root)
    before = {p: p.stat().st_mtime_ns for p in (root / "obi").rglob("*") if p.is_file()}
    raw = tomllib.loads((FIXTURE_DIR / "fixture.toml").read_text())
    harvest(raw, root / "obi", (0, 2**62), FIXTURE_DIR / "denylist.txt", WORDS)
    after = {p: p.stat().st_mtime_ns for p in (root / "obi").rglob("*") if p.is_file()}
    assert before == after


def test_at_lines_are_place_words(tmp_path: Path) -> None:
    from obi.core.demo.harvest import Harvest, read_denylist

    deny = tmp_path / "deny.txt"
    deny.write_text("@quuxfiber\n!somebody\nwarnword\n")
    h = Harvest()
    read_denylist(deny, h)
    assert h.place_words["quuxfiber"] is False
