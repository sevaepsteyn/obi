"""Contract tests: the landing + boot-mode selection (033's version selection, reshaped by
037 into the workspace shell's boot).

Asset-source assertions, the repo's established pattern for the viewer bundle (no XR or DOM
emulation in CI — the markers below pin the behaviors the field pass verifies live). The
033 guarantees survive with new addresses: manual override still beats auto-detect (legacy
``?version=`` redirects), detection is still affirmative-only (it now picks the default
layout instead of a page), and unknown values still never dead-end.
"""

from __future__ import annotations

import re
from pathlib import Path

from obi.web.server import STATIC_DIR, _allowlist

INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SHELL = (STATIC_DIR / "shell.js").read_text(encoding="utf-8")


# --- legacy entry points keep working (037 FR-023; was 033 FR-002) ----------------------------


def test_boot_reads_the_version_query_parameter() -> None:
    assert "URLSearchParams" in SHELL
    assert re.search(r"""get\(\s*['"]version['"]\s*\)""", SHELL)


def test_only_the_two_known_version_values_redirect() -> None:
    """``?version=2d|3d`` land on their widget pages; anything else falls through to the
    workspace (never a dead end on a typo)."""

    assert re.search(r"""===\s*['"]2d['"]""", SHELL)
    assert re.search(r"""===\s*['"]3d['"]""", SHELL)
    assert "'?w=map'" in SHELL
    assert "'?w=xr'" in SHELL


# --- conservative auto-detection survives (033 FR-002 -> 037 FR-021) ---------------------------


def test_detection_uses_the_affirmative_webxr_probe() -> None:
    assert "navigator.xr" in SHELL
    assert "isSessionSupported" in SHELL
    assert re.search(r"""isSessionSupported\(\s*['"]immersive-vr['"]\s*\)""", SHELL)
    assert re.search(r"""isSessionSupported\(\s*['"]immersive-ar['"]\s*\)""", SHELL)


def test_detection_failure_degrades_not_a_dead_end() -> None:
    """Absent API / rejection / exception all read as unsupported: the probe is wrapped so
    any non-affirmative outcome yields false — which now means 'no XR window in the default
    layout', with the workspace itself identical either way."""

    probe = re.search(r"async function immersiveSupported\(\)[^}]*\{.*?\n\}", SHELL, flags=re.S)
    assert probe, "expected an immersiveSupported() probe function"
    body = probe.group(0)
    assert "catch" in body, "the probe must swallow rejections/exceptions"
    assert "return false" in body, "any non-affirmative outcome must read as unsupported"
    assert "if (!navigator.xr) return false" in body


# --- the unified bundle (033 FR-001, widened by 037) --------------------------------------------


def test_viewer2d_is_in_the_server_allowlist(tmp_path: Path) -> None:
    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    assert "/viewer2d.js" in routes
    assert routes["/viewer2d.js"] == STATIC_DIR / "viewer2d.js"
    assert (STATIC_DIR / "viewer2d.js").is_file()


def test_both_viewer_generations_load_from_the_one_origin() -> None:
    """The 2D map loads via the MAP widget; the immersive session via the XR widget —
    same lazy dynamic-import idiom the 033 landing used."""

    map_widget = (STATIC_DIR / "widgets" / "map.js").read_text(encoding="utf-8")
    xr_widget = (STATIC_DIR / "widgets" / "xr.js").read_text(encoding="utf-8")
    assert re.search(r"""['"]\.\./viewer2d\.js['"]""", map_widget)
    assert re.search(r"""import\(['"]\.\./viewer\.js['"]\)""", xr_widget)
    # The XR widget's setup choices survive intact (site picker, mode picker, enter button).
    for marker in ('<option value="ar">', '<option value="vr"', 'class="enter"', 'id="xr-site"'):
        assert marker in xr_widget, marker


def test_no_served_asset_references_the_retired_port() -> None:
    for asset in STATIC_DIR.glob("**/*"):
        if asset.is_file() and asset.suffix in (".html", ".js", ".css"):
            assert "8444" not in asset.read_text(encoding="utf-8"), asset.name
