#!/usr/bin/env python3
"""076 screenshot harness — render every registered widget in real headless Chrome, one PNG each.

Dev tooling only: never imported by, served from, or required by the shipped app. Zero new
dependencies — Python stdlib plus a locally installed Chrome. It exists because the node DOM-stub
harnesses cannot see a browser LAYOUT failure (the 075 invisible-faceplate bug): only executing the
real widget in a real browser catches that class. See contracts/shots-harness.md.

    python tests/web_harness/shots/capture.py --label before
    python tests/web_harness/shots/capture.py --label after076 --only ros,ospf
    python tests/web_harness/shots/capture.py --label after076 --keep-server   # leave server up

The id list is DERIVED from registry.js `WIDGETS` (so a future catalog widget is captured with no
change here) plus a fixed set of representative minted-window ids that reference committed fixtures.
Every /api, /snapshots and export route a widget fetches is served from fixtures/<name>.json per
fixtures/routes.json, with each fixture's `_fresh` timestamp fields re-anchored to run time so ages
render plausibly. Exit is non-zero if any capture is missing or zero-byte.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
STATIC_DIR = REPO_ROOT / "src" / "obi" / "web" / "static"
FIXTURES_DIR = HERE / "fixtures"
ROUTES_PATH = FIXTURES_DIR / "routes.json"
OUT_ROOT = HERE / "out"

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# `--headless=new --screenshot` writes the PNG when the virtual-time budget elapses but does NOT
# reliably self-exit afterwards (it lingers on network/compositor idle). So we poll for the written
# file rather than wait on the process, then tear the whole process group down.
CAPTURE_TIMEOUT_S = 30.0

# Representative minted-window ids (the shell mints these on demand; they reference committed
# fixture data so a capture renders a realistic window). Kept beside the catalog ids derived from
# the registry — together they are the full coverage set (contracts/shots-harness.md §coverage).
MINTED_IDS = [
    "ros:10.20.0.1",   # router.json device address
    "conf:core-gw-1",  # configs.json / a device with history
    "ospf:core-gw-1",  # ospf.json node identity
    "link:ranch3~relay3",  # links.json l5 endpoint-pair key (bands.linkKey)
    "ospf-lint",       # the OSPF lint window (ospf.js params.view='lint')
]

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
}


def catalog_ids() -> list[str]:
    """The catalog widget ids, parsed from the WIDGETS array in registry.js.

    Bounded to the `export const WIDGETS = [ … ];` slice so the minted-def factories below it (which
    build ids from template literals, not `id: '…'` object keys) never leak in.
    """

    source = (STATIC_DIR / "registry.js").read_text(encoding="utf-8")
    start = source.index("export const WIDGETS = [")
    end = source.index("\n];", start)
    block = source[start:end]
    return re.findall(r"\bid:\s*'([^']+)'", block)


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _collect_fresh(obj: Any, fields: set[str], found: list[datetime]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in fields and isinstance(value, str):
                parsed = _parse_iso(value)
                if parsed is not None:
                    found.append(parsed)
            else:
                _collect_fresh(value, fields, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_fresh(item, fields, found)


def _shift_fresh(obj: Any, fields: set[str], delta: timedelta) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in fields and isinstance(value, str):
                parsed = _parse_iso(value)
                if parsed is not None:
                    obj[key] = (parsed + delta).isoformat()
            else:
                _shift_fresh(value, fields, delta)
    elif isinstance(obj, list):
        for item in obj:
            _shift_fresh(item, fields, delta)


def _freshen(payload: Any) -> Any:
    """Re-anchor a fixture's `_fresh` timestamp fields so the NEWEST becomes ~now and the rest keep
    their relative spacing (a time chart stays a time chart; a 5-min-old row stays 5 min old).
    Pops the `_fresh` manifest so it is never served."""

    if not isinstance(payload, dict):
        return payload
    fields = payload.pop("_fresh", None)
    if not isinstance(fields, list) or not fields:
        return payload
    field_set = {str(name) for name in fields}
    found: list[datetime] = []
    _collect_fresh(payload, field_set, found)
    if not found:
        return payload
    delta = datetime.now(UTC) - max(found)
    _shift_fresh(payload, field_set, delta)
    return payload


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, Path] = {}

    def log_message(self, *_args: Any) -> None:  # keep the console clean
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self._send(b"not served", "text/plain; charset=utf-8", status=404)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        # 1) fixture data routes (freshened per request)
        if path in self.routes:
            payload = _freshen(json.loads(self.routes[path].read_text(encoding="utf-8")))
            self._send(json.dumps(payload).encode(), _CONTENT_TYPES[".json"])
            return
        # 2) the harness page itself
        if path in ("/shot.html", "/shot.js"):
            target = HERE / path.lstrip("/")
            self._send(target.read_bytes(), _CONTENT_TYPES[target.suffix])
            return
        # 3) the real static assets, at their real root paths (so relative imports resolve)
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR in target.parents and target.is_file():
            content_type = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            self._send(target.read_bytes(), content_type)
            return
        self._not_found()


def _load_routes() -> dict[str, Path]:
    raw = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    return {
        url: FIXTURES_DIR / name
        for url, name in raw.items()
        if not url.startswith("_")
    }


def _start_server() -> socketserver.TCPServer:
    _Handler.routes = _load_routes()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # the whole Chrome process group
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _capture_one(chrome: str, base: str, widget_id: str, out_path: Path,
                 profile_dir: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)  # never mistake a stale capture for a fresh one
    command = [
        chrome,
        "--headless=new",
        f"--user-data-dir={profile_dir}",  # fresh profile: empty localStorage, no live-Chrome clash
        "--no-first-run",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1200,800",
        "--virtual-time-budget=8000",
        f"--screenshot={out_path}",
        f"{base}/shot.html?widget={widget_id}",
    ]
    # start_new_session ⇒ its own process group, so _terminate reaps the renderer children too.
    proc = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + CAPTURE_TIMEOUT_S
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break  # Chrome exited on its own (some pages do)
            if out_path.is_file() and out_path.stat().st_size > 0:
                time.sleep(0.4)  # let the final PNG write flush before we kill Chrome
                break
            time.sleep(0.2)
    finally:
        _terminate(proc)
    return out_path.is_file() and out_path.stat().st_size > 0


def main() -> int:
    parser = argparse.ArgumentParser(description="capture one PNG per registered obi widget")
    parser.add_argument("--label", required=True, help="names the tree state (before, after076)")
    parser.add_argument("--only", default="", help="comma id filter (exact or prefix-before-colon)")
    parser.add_argument("--keep-server", action="store_true",
                        help="leave the fixture server up after capturing (manual browser check)")
    args = parser.parse_args()

    chrome = os.environ.get("OBI_CHROME", DEFAULT_CHROME)
    if not Path(chrome).exists():
        print(f"Chrome not found at {chrome!r} (set $OBI_CHROME)", file=sys.stderr)
        return 2

    ids = [*catalog_ids(), *MINTED_IDS]
    if args.only:
        wanted = {token.strip() for token in args.only.split(",") if token.strip()}
        ids = [wid for wid in ids if wid in wanted or wid.split(":", 1)[0] in wanted]
        if not ids:
            print(f"--only {args.only!r} matched no widget ids", file=sys.stderr)
            return 2

    server = _start_server()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    out_dir = OUT_ROOT / args.label
    profile_dir = OUT_ROOT / ".chrome-profile"
    failures: list[str] = []
    try:
        print(f"serving fixtures at {base} — capturing {len(ids)} widget(s) to {out_dir}")
        for widget_id in ids:
            out_path = out_dir / f"{widget_id.replace(':', '_')}.png"
            ok = _capture_one(chrome, base, widget_id, out_path, profile_dir)
            print(f"  {'ok ' if ok else 'FAIL'}  {widget_id}")
            if not ok:
                failures.append(widget_id)
        if args.keep_server:
            print(f"\n--keep-server: browse {base}/shot.html?widget=<id> — Ctrl-C to stop")
            with contextlib.suppress(KeyboardInterrupt):
                threading.Event().wait()
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"\n{len(failures)} capture(s) missing or zero-byte: {', '.join(failures)}",
              file=sys.stderr)
        return 1
    print(f"\nall {len(ids)} captures written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
