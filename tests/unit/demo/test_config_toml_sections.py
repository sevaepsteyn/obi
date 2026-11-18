"""The dataset's config.toml never carries the 137-moved location sections."""

from __future__ import annotations

from types import SimpleNamespace

from obi.core.demo.sources.config_toml import DROPPED_SECTIONS, _sanitize


def test_location_sections_are_dropped() -> None:
    ctx = SimpleNamespace(rewriter=SimpleNamespace(ident=lambda s: s, text=lambda s: s))
    raw = {
        "brc": {"origin_lat": 45.0123, "origin_lon": -100.0456, "year": 2026},
        "playa": {"base_url": "https://api.example.net"},
        "dash": {"ping_router": "site-gw-1"},
    }
    out = _sanitize(ctx, raw, ())  # type: ignore[arg-type]
    assert {"brc", "playa"} <= DROPPED_SECTIONS
    assert out == {"dash": {"ping_router": "site-gw-1"}}
