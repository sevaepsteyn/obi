"""Python ↔ JS key-scheme parity (feature 136, T007): the browser must find the file the
exporter wrote, for every query. Skipped where node is absent."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.core.demo.routes import route_key
from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "demo_routekey.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_VALUES = ["aspen-gw", "core-gw-1", "birch ptp/60", "ünï", "a&b=c", "it's (x)!*", "24h", "7d",
           "1a2b3c4d", "sfp-sfpplus1", "~tilde.dot_under"]  # fmt: skip


def _queries(n: int = 60) -> list[dict[str, object]]:
    rng = random.Random(136)
    routes = {"health": ["device"], "metrics": ["target", "metric", "window", "interface"],
              "configs": ["device", "view", "at", "from", "to", "limit"], "router": ["device"],
              "dash": []}  # fmt: skip
    out: list[dict[str, object]] = []
    for _ in range(n):
        route = rng.choice(sorted(routes))
        keys = routes[route] + ["junk"]
        params = {k: rng.choice(_VALUES) for k in rng.sample(keys, rng.randint(0, len(keys)))}
        out.append({"route": route, "params": params})
    return out


def test_js_and_python_keys_agree(tmp_path: Path) -> None:
    queries = _queries()
    fixture = tmp_path / "queries.json"
    fixture.write_text(json.dumps(queries), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "demo" / "api.js"), str(fixture)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    js_keys = json.loads(proc.stdout.strip().splitlines()[-1])
    py_keys = [route_key(str(q["route"]), q["params"]) for q in queries]  # type: ignore[arg-type]
    assert js_keys == py_keys
