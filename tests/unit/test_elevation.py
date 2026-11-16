"""Unit tests: the EPQS-backed, durably-cached elevation source (feature 022, T011)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.elevation import ElevationSource, _parse_epqs_value
from obi.core.models import GroundProvenance

FIXTURE = Path(__file__).parents[1] / "fixtures" / "ar" / "epqs-response.json"


class CountingFetcher:
    def __init__(self, value: float | None) -> None:
        self.value = value
        self.calls: list[tuple[float, float]] = []

    def __call__(self, lat: float, lon: float) -> float | None:
        self.calls.append((lat, lon))
        return self.value


def _source(tmp_path: Path, fetcher: CountingFetcher, **kwargs: object) -> ElevationSource:
    return ElevationSource(
        tmp_path / "elevations.json",
        default_ground_elevation=1191.0,
        fetcher=fetcher,
        **kwargs,  # type: ignore[arg-type]
    )


def test_miss_fetches_then_caches_durably(tmp_path: Path) -> None:
    fetcher = CountingFetcher(1190.66)
    source = _source(tmp_path, fetcher)
    assert source.ground("Playa", 40.78, -119.23) == (1190.66, GroundProvenance.EPQS)
    assert len(fetcher.calls) == 1
    # same site again in the same process → cache, no second fetch
    assert source.ground("Playa", 40.78, -119.23) == (1190.66, GroundProvenance.CACHE)
    assert len(fetcher.calls) == 1
    # a NEW process (fresh instance, same cache file) → still no fetch (durable, FR-005)
    fresh_fetcher = CountingFetcher(None)
    fresh = _source(tmp_path, fresh_fetcher)
    assert fresh.ground("Playa", 40.78, -119.23) == (1190.66, GroundProvenance.CACHE)
    assert fresh_fetcher.calls == []


def test_epqs_down_falls_back_to_default_with_degraded_provenance(tmp_path: Path) -> None:
    fetcher = CountingFetcher(None)
    source = _source(tmp_path, fetcher)
    assert source.ground("Never Seen", 40.0, -119.0) == (
        1191.0,
        GroundProvenance.DEFAULT_GROUND,
    )
    # the failure is NOT cached — a later run with connectivity fetches for real
    assert source.ground("Never Seen", 40.0, -119.0)[1] is GroundProvenance.DEFAULT_GROUND
    assert len(fetcher.calls) == 2


def test_override_always_wins_and_never_fetches(tmp_path: Path) -> None:
    fetcher = CountingFetcher(1190.66)
    source = _source(tmp_path, fetcher, overrides={"Basswood Relay - East": 1370.0})
    assert source.ground("Basswood Relay - East", 40.76, -119.27) == (
        1370.0,
        GroundProvenance.OVERRIDE,
    )
    assert fetcher.calls == []


def test_corrupt_cache_file_is_treated_as_empty(tmp_path: Path) -> None:
    cache = tmp_path / "elevations.json"
    cache.write_text("{not json")
    fetcher = CountingFetcher(1200.5)
    source = _source(tmp_path, fetcher)
    assert source.ground("Somewhere", 41.0, -119.5) == (1200.5, GroundProvenance.EPQS)
    # and the fetch repaired the cache file
    assert json.loads(cache.read_text())


def test_nearby_but_distinct_coordinates_are_distinct_cache_keys(tmp_path: Path) -> None:
    fetcher = CountingFetcher(1195.0)
    source = _source(tmp_path, fetcher)
    source.ground("A", 40.78001, -114.83772)
    source.ground("B", 40.78002, -114.83773)  # differs at the 5th decimal (~1 m)
    assert len(fetcher.calls) == 2


def test_parse_epqs_value_from_the_recorded_fixture() -> None:
    """The live EPQS response carries ``value`` as a JSON STRING — the parser must coerce."""

    payload = json.loads(FIXTURE.read_text())
    assert _parse_epqs_value(payload) == 1190.661987305


def test_parse_epqs_value_rejects_nonsense() -> None:
    assert _parse_epqs_value({"value": "-1000000"}) is None  # EPQS no-data sentinel
    assert _parse_epqs_value({"value": "not-a-number"}) is None
    assert _parse_epqs_value({"value": None}) is None
    assert _parse_epqs_value({}) is None
    assert _parse_epqs_value(None) is None
    assert _parse_epqs_value({"value": 25000}) is None  # above any terrain
