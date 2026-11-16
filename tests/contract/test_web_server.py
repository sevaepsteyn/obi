"""Contract tests: the AR server's path allowlist (feature 022, T021 — contracts/ar-cli.md).

One in-process TLS server on an ephemeral port; every assertion is about what the server
will and will not serve (FR-012). The self-signed certificate is generated once per test
session into a tmp state dir via the real ``openssl`` path (exercising ``ensure_certificate``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from obi.web.server import STATIC_DIR, create_server, ensure_certificate


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    tmp_path = tmp_path_factory.mktemp("ar-server")
    export_path = tmp_path / "links.json"
    export_path.write_text(json.dumps({"generated_at": "2026-07-01T00:00:00+00:00"}))
    server = create_server(
        bind="127.0.0.1", port=0, export_path=export_path, state_dir=tmp_path / "state"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _get(base: str, path: str) -> httpx.Response:
    return httpx.get(f"{base}{path}", verify=False, timeout=5.0)


def test_allowlisted_paths_are_served(served: tuple[str, Path]) -> None:
    base, _tmp = served
    assert _get(base, "/").status_code == 200
    assert _get(base, "/index.html").status_code == 200
    assert _get(base, "/viewer.js").status_code == 200
    assert _get(base, "/vendor/three.module.min.js").status_code == 200
    response = _get(base, "/links.json")
    assert response.status_code == 200
    assert response.json()["generated_at"] == "2026-07-01T00:00:00+00:00"


def test_everything_else_is_404(served: tuple[str, Path]) -> None:
    base, _tmp = served
    for path in (
        "/audit.jsonl",
        "/config.toml",
        "/../pyproject.toml",
        "/vendor/../../core/config.py",
        "/static/",
        "/vendor/",
        "/links.json.tmp",
        "/index.html/../viewer.js.map",
    ):
        assert _get(base, path).status_code == 404, path


def test_no_directory_listing(served: tuple[str, Path]) -> None:
    base, _tmp = served
    for path in ("/", "/vendor"):
        response = _get(base, path)
        body = response.text if response.status_code == 200 else ""
        assert "Directory listing" not in body


def test_query_strings_do_not_widen_the_allowlist(served: tuple[str, Path]) -> None:
    base, _tmp = served
    assert _get(base, "/links.json?x=1").status_code == 200
    assert _get(base, "/secret?path=/links.json").status_code == 404


def test_certificate_is_generated_once_and_reused(tmp_path: Path) -> None:
    cert1, key1 = ensure_certificate(tmp_path)
    stamp = cert1.stat().st_mtime_ns
    cert2, _key2 = ensure_certificate(tmp_path)
    assert cert2 == cert1
    assert cert2.stat().st_mtime_ns == stamp  # not regenerated
    assert key1.stat().st_mode & 0o077 == 0  # key is 0600


def test_static_assets_exist_in_the_package() -> None:
    """The wheel must ship the whole bundle: the 022/033 viewer files plus the 037
    workspace (shell, window manager, registry, theme, one module per widget)."""

    for name in (
        "index.html",
        "viewer.js",
        "viewer2d.js",
        "voice.js",
        "vendor/three.module.min.js",
        "theme.css",
        "shell.js",
        "wm.js",
        "registry.js",
        "widgets/map.js",
        "widgets/talk.js",
        "widgets/xr.js",
        "kit.js",
        "widgets/logs.js",
        "boards.js",
        "widgets/uisp.js",
        "widgets/ros.js",      # 047: was stat.js (STAT → ROS)
        "widgets/router.js",   # 047: the per-router live-detail widget
        "widgets/sik.js",
        "widgets/unifi.js",
        "widgets/nbx.js",
        "widgets/watch.js",
        "chart.js",
        "widgets/met.js",
        "widgets/cfg.js",
        "widgets/audit.js",
        "widgets/links.js",
        "widgets/snap.js",   # 076: the ephemeral snapshot window (the one new served file)
    ):
        assert (STATIC_DIR / name).is_file(), name


# --- the workspace routes (037, contracts/workspace-urls.md) ---------------------------------


def test_workspace_routes_serve_with_the_right_content_types(
    served: tuple[str, Path],
) -> None:
    base, _tmp = served
    for path in ("/shell.js", "/wm.js", "/registry.js", "/widgets/map.js",
                 "/widgets/talk.js", "/widgets/xr.js",
                 "/widgets/snap.js"):  # 076: the ephemeral snapshot window is served
        response = _get(base, path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/javascript"), path
    css = _get(base, "/theme.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")


def test_workspace_routes_are_get_head_only(served: tuple[str, Path]) -> None:
    base, _tmp = served
    for path in ("/shell.js", "/theme.css", "/widgets/map.js"):
        response = httpx.post(f"{base}{path}", verify=False, timeout=5.0)
        assert response.status_code == 405, path


def test_widget_directory_itself_is_not_served(served: tuple[str, Path]) -> None:
    base, _tmp = served
    for path in ("/widgets", "/widgets/", "/widgets/nope.js"):
        assert _get(base, path).status_code == 404, path


def test_terrain_route_is_served_when_present(served: tuple[str, Path]) -> None:
    """031: /terrain.json joins the allowlist, serving the file beside the export."""

    base, tmp = served
    terrain_path = tmp / "terrain.json"
    assert _get(base, "/terrain.json").status_code == 404  # not written yet
    terrain_path.write_text(json.dumps({"generated_at": "x", "profiles": {}, "sites": {}}))
    response = _get(base, "/terrain.json")
    assert response.status_code == 200
    assert response.json()["profiles"] == {}
    assert _get(base, "/terrain.json.tmp").status_code == 404  # atomicity leftovers never leak


def test_terrain_shade_png_is_served_as_image_when_present(served: tuple[str, Path]) -> None:
    """055: /terrain-shade.png joins the allowlist beside links.json, typed image/png.

    Same 404-until-written degrade shape as terrain.json; the sidecar meta is never served.
    """

    base, tmp = served
    shade_path = tmp / "terrain-shade.png"
    assert _get(base, "/terrain-shade.png").status_code == 404  # not written yet
    # a tiny valid PNG magic + bytes is enough to prove the byte-serving path
    shade_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    response = _get(base, "/terrain-shade.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    # the regeneration-policy sidecar and atomicity leftovers stay unserved
    assert _get(base, "/terrain-shade.meta.json").status_code == 404
    assert _get(base, "/terrain-shade.png.tmp").status_code == 404


def test_terrain_shade_route_name_matches_the_export_artifact() -> None:
    """The served path resolves to ``terrain-shade.png`` beside the export (name parity)."""

    from obi.web.server import _allowlist

    routes = _allowlist(STATIC_DIR, Path("/state/ar/links.json"))
    assert routes["/terrain-shade.png"] == Path("/state/ar/terrain-shade.png")


# --- the snapshot routes (040) -------------------------------------------------------------------


def test_snapshot_allowlist_matches_the_tool_catalog(tmp_path: Path) -> None:
    """server.py hardcodes the snapshot names (staying import-light); this test is the
    drift tripwire against core.snapshot.SNAPSHOT_TOOL_NAMES."""

    from obi.core.snapshot import SNAPSHOT_TOOL_NAMES
    from obi.web.server import _allowlist

    routes = _allowlist(STATIC_DIR, tmp_path / "links.json", tmp_path / "snapshots")
    served = {p for p in routes if p.startswith("/snapshots/")}
    assert served == {f"/snapshots/{name}.json" for name in SNAPSHOT_TOOL_NAMES}


def test_snapshot_route_is_404_until_first_run_then_serves(served: tuple[str, Path]) -> None:
    base, tmp = served
    assert _get(base, "/snapshots/status.json").status_code == 404
    snapshots = tmp / "state" / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / "status.json").write_text(
        json.dumps({"tool": "status", "generated_at": "x", "data": {}})
    )
    response = _get(base, "/snapshots/status.json")
    assert response.status_code == 200
    assert response.json()["tool"] == "status"
    assert _get(base, "/snapshots/other.json").status_code == 404


def test_brc_routes_are_allowlisted_beside_the_export() -> None:
    """098: the city-grid artifact + the pure geometry module (contracts/brc-artifacts.md C3)."""

    from obi.web.server import _allowlist

    routes = _allowlist(STATIC_DIR, Path("/state/ar/links.json"))
    assert routes["/brc.json"] == Path("/state/ar/brc.json")
    assert routes["/brc.js"] == STATIC_DIR / "brc.js"


def test_brc_route_is_served_when_present(served: tuple[str, Path]) -> None:
    base, tmp = served
    assert _get(base, "/brc.json").status_code == 404  # no artifact yet ⇒ the C2 degraded path
    (tmp / "brc.json").write_text(json.dumps({"year": 2026, "sites": {}}))
    response = _get(base, "/brc.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["year"] == 2026
    js = _get(base, "/brc.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")


def test_demo_modules_are_never_served_live(served: tuple[str, Path]) -> None:
    # feature 136: static/demo/* is copied into static demo bundles only — the live server's
    # allowlist must never expose it (the fetch shim must not run against live data).
    base, _tmp = served
    for path in ("/demo/boot.js", "/demo/api.js", "/demo/clock.js", "/demo/replay.js"):
        assert _get(base, path).status_code == 404, path
