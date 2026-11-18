"""Browser filter engine ≡ live handlers (feature 136, T017, research R9).

The static demo filters logs and audit in the browser (the one Principle III exception, plan
Complexity Tracking). This pins it to the server: with slices covering the whole synthetic
store, every query must return exactly what the live Python handler returns. Skipped without node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest

from obi.core.demo.export import dataset_env, load_dataset, load_export_profile, render_slices
from obi.core.demo.routes import capture
from obi.web import data_api
from obi.web.server import STATIC_DIR
from tests.fixtures.demo.harness import build_dataset

HARNESS = Path(__file__).parent.parent / "web_harness" / "demo_api.mjs"
pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node not installed"),
    pytest.mark.slow,  # builds a demo dataset (seconds)
]

LOG_QUERIES = [
    {}, {"window": "1h"}, {"window": "6h"}, {"window": "7d"}, {"device": "gw"},
    {"severity": "error"}, {"severity": "warning", "window": "7d"}, {"source": "syslog"},
    {"source": "core"}, {"q": "login failure"}, {"q": "admin logged in"}, {"q": "ospf neighbor"},
    {"q": "valley floor", "window": "7d"}, {"device": "core", "q": "snmp"},
]  # fmt: skip
AUDIT_QUERIES = [
    {}, {"kind": "device_command"}, {"kind": "action,tool_call"}, {"source": "cli:status"},
    {"outcome": "ok"}, {"since": "1h"}, {"since": "7d"}, {"text": "print"},
    {"limit": "5"}, {"since": "30d", "kind": "action"},
]  # fmt: skip


@dataclass
class Built:
    bundle: Path
    view: object


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Built]:
    root = tmp_path_factory.mktemp("parity")
    dataset, config, _truth = build_dataset(root)
    profile = load_export_profile(
        {"demo": {"export": {"bundle_dir": str(root / "b"), "logs_slice_rows": 1_000_000,
                             "audit_slice_rows": 1_000_000}}}, config_path=config)  # fmt: skip
    view = load_dataset(dataset)
    render_slices(view, root / "b", profile)
    yield Built(root / "b", view)


def _python(built: Built) -> list[object]:
    view = built.view
    cfg = view.config  # type: ignore[attr-defined]
    results: list[object] = []
    with dataset_env(view.dataset), data_api.frozen_now(view.now):  # type: ignore[attr-defined]
        for params in LOG_QUERIES:
            _s, payload = capture(data_api.handle_logs_request,
                                  "/api/logs.json?" + urlencode(params), cfg.logs)  # fmt: skip
            results.append([[e["timestamp"], e["host"], e["message"]] for e in payload["entries"]])
        for params in AUDIT_QUERIES:
            _s, payload = capture(data_api.handle_audit_request,
                                  "/api/audit.json?" + urlencode(params), cfg.audit)  # fmt: skip
            results.append({"ids": [e["id"] for e in payload["events"]],
                            "total": payload["total"]})  # fmt: skip
    return results


def test_browser_engine_matches_the_live_handlers(built: Built, tmp_path: Path) -> None:
    queries = [{"route": "logs", "params": q} for q in LOG_QUERIES] + [
        {"route": "audit", "params": q} for q in AUDIT_QUERIES
    ]
    qfile = tmp_path / "q.json"
    qfile.write_text(json.dumps(queries))
    proc = subprocess.run(
        [
            "node",
            str(HARNESS),
            str(STATIC_DIR / "demo" / "api.js"),
            str(built.bundle),
            str(qfile),
            built.view.now.isoformat(),
        ],  # type: ignore[attr-defined]
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    js = json.loads(proc.stdout.strip().splitlines()[-1])
    py = _python(built)
    for query, a, b in zip(queries, js, py, strict=True):
        assert a == b, query
    assert any(r for r in py[: len(LOG_QUERIES)]), "the matrix must exercise non-empty results"
