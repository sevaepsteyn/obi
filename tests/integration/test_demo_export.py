"""`obi demo export` end to end, served statically (feature 136, T024).

1. Build the bundle through the CLI from the synthetic network.
2. Grep the whole bundle INDEPENDENTLY for every planted real token (SC-002).
3. Serve it from a plain static server under `/obi/`, logging every request, and run real
   headless Chrome on the real workspace page (`/obi/?w=<id>`) for every catalog widget plus
   minted router/config/OSPF windows.

The run must show zero 404s (other than favicon) and zero requests escaping the `/obi/`
sub-path. That proves the boot shim answers every widget data request from files, with no server
behind it. Chrome steps are skipped where Chrome is absent.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import socketserver
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.fixtures.demo.harness import DemoEnv, build_dataset, dump_text, offline_terrain, run_cli

# Runs the demo snapshot/export pipeline (seconds per test) — skip with -m 'not slow'.
pytestmark = pytest.mark.slow

SHOTS = Path(__file__).resolve().parents[1] / "web_harness" / "shots" / "capture.py"
CHROME = os.environ.get(
    "OBI_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
#: Headless Chrome instances run at once by the board test.
_SHOT_CONCURRENCY = 6


def _capture_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("obi_shots_capture", SHOTS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, DemoEnv]]:
    root = tmp_path_factory.mktemp("e2e")
    dataset, config, truth = build_dataset(root)
    bundle = root / "bundle"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_STATE_HOME", str(root / "xdg"))
        mp.setenv("XDG_CACHE_HOME", str(root / "cache"))
        offline_terrain(mp)
        result = run_cli("demo", "export", "--config", str(config), "--dataset", str(dataset),
                         "--out", str(bundle))  # fmt: skip
        assert result.exit_code == 0, result.output
    yield bundle, DemoEnv(root, root / "xdg", config, dataset, truth)


def test_bundle_holds_no_planted_real_token(built: tuple[Path, DemoEnv]) -> None:
    bundle, env = built
    text = dump_text(bundle)
    for token in env.truth.all_fatal_tokens():
        assert token not in text
    assert str(Path.home()) not in text


def _shoot(shots, url: str, out: Path, profile: Path) -> bool:  # type: ignore[no-untyped-def]
    """One headless-Chrome screenshot of ``url`` (the capture harness's flags + teardown)."""

    import subprocess
    import time

    command = [CHROME, "--headless=new", f"--user-data-dir={profile}", "--no-first-run",
               "--hide-scrollbars", "--window-size=1200,800", "--virtual-time-budget=8000",
               f"--screenshot={out}", url]  # fmt: skip
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)  # fmt: skip
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if out.is_file() and out.stat().st_size > 0:
                time.sleep(0.4)
                break
            time.sleep(0.2)
    finally:
        shots._terminate(proc)
    return out.is_file() and out.stat().st_size > 0


class _Logged(http.server.SimpleHTTPRequestHandler):
    log: list[tuple[str, int]] = []
    root: Path = Path()

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0]
        if not path.startswith("/obi/"):
            return str(self.root / "__outside__")
        return str(self.root / path[len("/obi/") :])

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        type(self).log.append((self.path, int(code) if str(code).isdigit() else 0))

    def log_message(self, *_args: object) -> None:
        return None


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome not installed")
def test_every_board_renders_from_files_under_a_sub_path(
    built: tuple[Path, DemoEnv], tmp_path: Path
) -> None:
    bundle, _env = built
    shots = _capture_module()
    _Logged.root, _Logged.log = bundle, []
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Logged)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/obi"

    status = json.loads((bundle / "data" / "snapshots" / "status.json").read_text())
    address = status["data"]["devices"][0]["address"]
    configs = json.loads(next((bundle / "data" / "api" / "configs").glob("index.json")).read_text())
    device = next(r["identity"] for r in configs["rows"] if r["has_backup"])
    ids = [*shots.catalog_ids(), f"ros:{address}", f"conf:{device}", f"ospf:{device}"]
    def shoot(widget_id: str) -> bool:
        # Chrome locks its user-data-dir, so each concurrent shot gets its own profile.
        stem = widget_id.replace(":", "_")
        out = tmp_path / f"{stem}.png"
        return _shoot(shots, f"{base}/?w={widget_id}", out, tmp_path / "profiles" / stem)

    try:
        # Concurrent shots (feature 138): ~23 boards × ~1s each was the suite's longest test.
        with ThreadPoolExecutor(max_workers=_SHOT_CONCURRENCY) as pool:
            failed = [w for w, ok in zip(ids, pool.map(shoot, ids), strict=True) if not ok]
        # Under xdist several workers launch Chrome at once and a cold start occasionally misses
        # the 30 s deadline with no screenshot (seen for `talk`, 2026-11-17). Retry each miss once,
        # alone; a board that is really broken fails both times.
        failed = [w for w in failed if not shoot(w)]
    finally:
        server.shutdown()
        server.server_close()
    misses = [(p, c) for p, c in _Logged.log if c == 404 and "favicon" not in p]
    # The browser itself probes /favicon.ico at the site root — not an app request.
    outside = [p for p, _c in _Logged.log if not p.startswith("/obi/") and p != "/favicon.ico"]
    assert not failed, failed
    assert not misses, misses
    assert not outside, outside
    assert any("/obi/data/" in p for p, _c in _Logged.log)  # data came from files
