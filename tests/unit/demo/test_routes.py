"""Route catalogue + key scheme (feature 136, T005)."""

from __future__ import annotations

import re
from pathlib import Path

from obi.core.demo.routes import ROUTES, canonical_query, data_path, route_key

SERVER = Path(__file__).resolve().parents[3] / "src" / "obi" / "web" / "server.py"


def test_canonical_keeps_only_key_params_sorted_and_encoded() -> None:
    q = canonical_query("metrics", {"window": "24h", "target": "aspen gw", "metric": "cpu",
                                    "interface": "ether1", "junk": "x"})  # fmt: skip
    assert q == "metric=cpu&target=aspen%20gw&window=24h"


def test_key_is_index_when_empty_and_stable_otherwise() -> None:
    assert route_key("health", {}) == "index"
    assert route_key("health", {"other": "x"}) == "index"
    a = route_key("health", {"device": "aspen-gw"})
    assert a == route_key("health", {"device": "aspen-gw", "window": "7d"})
    assert len(a) == 16 and a != route_key("health", {"device": "birch-gw"})
    assert data_path("health", {"device": "aspen-gw"}) == f"data/api/health/{a}.json"


def test_every_live_route_is_classified() -> None:
    source = SERVER.read_text(encoding="utf-8")
    respond = source[source.index("def _respond") :]
    live = set(re.findall(r'"(/(?:api/[a-z-]+\.(?:json|wav)|api/[a-z]+|voice\.json|healthz))"',
                          respond))  # fmt: skip
    catalogued = {spec.path for spec in ROUTES.values()}
    assert live, "no routes parsed from server.py"
    assert live <= catalogued, f"unclassified live routes: {sorted(live - catalogued)}"
