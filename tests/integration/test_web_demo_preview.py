"""`obi web serve --demo`: the local static preview of a bundle (feature 136, T042)."""

from __future__ import annotations

import inspect
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

import obi.web.data_api as data_api
from obi.web.server import create_demo_server
from tests.fixtures.demo.harness import run_cli


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the demo preview must never call a live data handler")

    for name, fn in inspect.getmembers(data_api, inspect.isfunction):
        if fn.__module__ == data_api.__name__:
            monkeypatch.setattr(data_api, name, boom)
    bundle = tmp_path / "bundle"
    (bundle / "data" / "api").mkdir(parents=True)
    (bundle / "index.html").write_bytes(b"<!doctype html><title>obi</title>\n")
    (bundle / "data" / "api" / "x.json").write_bytes(b'{"a": [1, 2]}')
    server = create_demo_server(bundle, base="/obi/", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", bundle
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - localhost test
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def test_serves_the_bundle_byte_identically_under_the_base(served: tuple[str, Path]) -> None:
    base, bundle = served
    assert _get(f"{base}/obi/") == (200, (bundle / "index.html").read_bytes())
    assert _get(f"{base}/obi/data/api/x.json?v=1") == (200, b'{"a": [1, 2]}')
    assert _get(f"{base}/obi")[0] == 200  # redirected to /obi/


@pytest.mark.parametrize(
    "path", ["/", "/index.html", "/api/dash", "/data/api/x.json", "/obi/../index.html",
             "/obi/missing.json"],
)  # fmt: skip
def test_404_outside_the_base(served: tuple[str, Path], path: str) -> None:
    base, _bundle = served
    assert _get(f"{base}{path}")[0] == 404


def test_binds_localhost_only(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("x")
    server = create_demo_server(tmp_path, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_cli_refuses_a_missing_bundle(tmp_path: Path) -> None:
    result = run_cli("web", "serve", "--demo", "--bundle", str(tmp_path / "nope"))
    assert result.exit_code == 2
