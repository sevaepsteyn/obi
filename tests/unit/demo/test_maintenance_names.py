"""Maintenance site lists are identifiers: harvested from config and renamed in dash files."""

from __future__ import annotations

from types import SimpleNamespace

from obi.core.demo.harvest import harvest_config
from obi.core.demo.sources.jsonfiles import _ident_maintenance


def test_maintenance_sites_are_harvested() -> None:
    raw = {"maintenance": {"sites": ["lantern"], "uisp_sites": ["Old Mill - Annex"]}}
    h = harvest_config(raw, frozenset({"lantern", "mill"}))
    assert "lantern" in h.place_words


def test_dash_maintenance_lists_use_ident() -> None:
    ctx = SimpleNamespace(rewriter=SimpleNamespace(ident=lambda s: f"<{s}>"))
    board = {"maintenance": {"sites": ["lantern"], "signals": ["mesh"], "reason": "teardown"}}
    out = _ident_maintenance(ctx, board)  # type: ignore[arg-type]
    assert out["maintenance"]["sites"] == ["<lantern>"]
    assert out["maintenance"]["signals"] == ["mesh"] and out["maintenance"]["reason"] == "teardown"
