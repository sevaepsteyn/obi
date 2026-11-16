"""Contract tests: viewer navigation + live update (029 ar-viewer-nav.md, revised by 080).

There is no browser JS runner (accepted 022 trade-off), so most of these are tripwires on
the packaged viewer text: the interaction/refresh machinery must be present, and the 022
"thin adapter" negatives (no geodesy, no external origins) must survive the new code.
The 080 stick-nav rework additionally moved its pure math into nav.js precisely so the
numbers CAN be executed — the nav_headless.mjs harness test below is the layer that would
have caught the three shipped nav bugs, which all passed every text tripwire here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR, _allowlist

VIEWER = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
XR_WIDGET = (STATIC_DIR / "widgets" / "xr.js").read_text(encoding="utf-8")
NAV = (STATIC_DIR / "nav.js").read_text(encoding="utf-8")


# --- US1: hands navigation (T004) ---------------------------------------------------------


def test_hand_tracking_is_an_optional_session_feature() -> None:
    assert "optionalFeatures" in VIEWER
    assert "'hand-tracking'" in VIEWER
    assert "'local'" in VIEWER  # still requested; hands are additive


def test_grab_engine_listens_to_select_events() -> None:
    assert "selectstart" in VIEWER
    assert "selectend" in VIEWER
    assert "inputsourceschange" in VIEWER  # grabs die with their input source
    assert "targetRaySpace" in VIEWER


def test_tap_toggles_zones_and_ax_button_path_survives() -> None:
    assert "TAP_MS" in VIEWER
    assert "TAP_MOVE_M" in VIEWER
    assert "ZONE_BUTTON" in VIEWER  # 025 A/X face-button toggle is preserved


def test_navigation_is_clamped() -> None:
    assert "clampScale" in VIEWER
    assert "clampPan" in VIEWER


def test_gestures_anchor_to_gazed_content() -> None:
    """Field lesson (2026-07-06 headset pass): transforms centered at the viewer are
    angularly invisible at km scale — gestures must anchor to the looked-at link
    (zoom/yaw about the gaze-content point, pans amplified by its distance)."""

    assert "gazeAnchor" in VIEWER
    assert "distanceSqToSegment" in VIEWER  # anchor picks the link nearest the gaze ray
    assert "GRAB_REACH_M" in VIEWER  # pan amplification = anchor distance / hand reach


def test_hand_gamepads_do_not_drive_thumbstick_nav() -> None:
    """Quest hands surface a vestigial gamepad; only real controllers get stick nav."""

    assert "source.hand" in VIEWER


def test_viewer_stays_a_thin_adapter() -> None:
    """022 negatives re-asserted on the 029 text: no geodesy, no external origins."""

    for marker in ("6371", "haversine", "toRadians", "latitude"):
        assert marker not in VIEWER, marker
    assert not re.search(r"https?://", VIEWER)
    assert not re.search(r"https?://(?!localhost)", XR_WIDGET)


def test_setup_mentions_hands() -> None:
    # 037: the setup surface is the XR widget; the gesture crib sheet moved with it.
    assert "pinch" in XR_WIDGET.lower()


# --- US2: live update (T009) ---------------------------------------------------------------


def test_viewer_polls_the_export() -> None:
    assert "setInterval" in VIEWER
    assert "generated_at" in VIEWER
    assert "no-cache" in VIEWER
    assert "clearInterval" in VIEWER  # polling dies with the session


def test_viewer_disposes_replaced_scene_objects() -> None:
    assert ".dispose(" in VIEWER
    assert "ZONE_UNIT_SPHERE" in VIEWER  # the shared geometry survives rebuilds


def test_data_freshness_shows_on_the_setup_page_not_in_scene() -> None:
    """Revised post-031 (operator decision, 2026-07-06 VR pass): the in-scene freshness
    sprite floated a translucent card dead in the view and was removed; the export
    timestamp lives on the setup page, and the poll machinery (above) stays."""

    assert "generated_at" in XR_WIDGET
    assert "makeUpdatableLabel(new THREE.Vector3(0, -0.45, -1.6)" not in VIEWER


def test_setup_keeps_its_one_shot_fetch() -> None:
    """The XR widget fetches once at open; in-session polling lives in viewer.js."""

    assert XR_WIDGET.count("fetch(") == 1


# --- US3: controller navigation parity (T013) ----------------------------------------------


def test_thumbsticks_split_by_handedness() -> None:
    assert "handedness" in VIEWER
    assert "'left'" in VIEWER
    assert "'right'" in VIEWER
    assert "ZOOM_RATE" in VIEWER
    assert "PAN_RATE" in VIEWER


def test_stick_axis_fallback_is_preserved() -> None:
    """The 022 axes[2]-with-axes[0]-fallback quirk handling must survive."""

    assert "axes[2]" in VIEWER
    assert "axes[0]" in VIEWER


# --- 080: the stick-nav rework (specs/080-xr-nav/contracts/xr-nav.md) ----------------------


def test_nav_module_is_served(tmp_path: Path) -> None:
    """nav.js joins the startup-fixed allowlist (048 rule: shipping it = one restart)."""

    routes = _allowlist(STATIC_DIR, tmp_path / "links.json")
    assert "/nav.js" in routes
    assert routes["/nav.js"] == STATIC_DIR / "nav.js"
    assert (STATIC_DIR / "nav.js").is_file()


def test_nav_module_is_pure() -> None:
    """DOM-free, import-free, three-free — the node harness runs it unstubbed."""

    for marker in ("document", "window", "THREE", "import "):
        assert marker not in NAV, marker


def test_left_stick_moves_in_the_view_frame_not_the_room() -> None:
    """US1: the reference-space-axis pan (the 'push forward goes some other way' bug)
    is gone; movement derives from the camera pose via the nav.js basis helpers, and
    (field round 1) stays strictly horizontal — altitude is zoom's job."""

    assert "./nav.js" in VIEWER
    assert "horizontalBasis" in VIEWER
    assert "smoothDistance" in VIEWER
    assert "PAN_RATE_PER_S" in VIEWER
    assert "getWorldDirection" in VIEWER
    assert "world.position.x -= x * step" not in VIEWER
    assert "world.position.z -= y * step" not in VIEWER
    assert "world.position.y -=" not in VIEWER  # sticks never change altitude
    assert "world.position.y +=" not in VIEWER


def test_gaze_anchor_never_lands_beyond_the_terrain() -> None:
    """Field round 1: at the mountain the nearest LINK segment lies kilometers past the
    terrain face being looked at — zoom anchored beyond the ground and pushed the
    operator out. The anchor distance is now capped by the gaze's terrain raycast."""

    assert "terrainGazeDistance" in VIEWER
    assert "Raycaster" in VIEWER
    assert "TERRAIN_HIT_MAX_AGE_MS" in VIEWER


def test_right_stick_latches_one_anchor_per_gesture() -> None:
    """US2: the zoom/yaw pivot is captured at deadzone engage and held — never re-picked
    per frame (the push-out/spin-at-close-range bug)."""

    assert "remapDeflection" in VIEWER
    assert "justEngaged" in VIEWER
    assert "justReleased" in VIEWER
    assert "applyAboutPoint" in VIEWER
    assert "gazeAnchor" in VIEWER


def test_stick_yaw_never_rotates_about_the_map_origin() -> None:
    """US3: the origin-pivot yaw (the 'strange spins around the map' bug) is gone — the
    only world.rotation.y write lives inside applyAboutPoint."""

    assert "world.rotation.y -=" not in VIEWER
    assert VIEWER.count("world.rotation.y") == 1  # the += inside applyAboutPoint


def test_stick_rates_are_per_second_with_radial_deadzone() -> None:
    """US4: no per-frame rate vocabulary or per-axis deadzone gates survive; every rate
    applies × the frame delta."""

    for retired in ("YAW_DEADZONE", "PAN_RATE =", "YAW_RATE =", "ZOOM_RATE =",
                    "Math.abs(x) >", "Math.abs(y ?? 0) >"):
        assert retired not in VIEWER, retired
    for marker in ("frameDt", "PAN_RATE_PER_S * dt", "ZOOM_RATE_PER_S * dt",
                   "YAW_RATE_PER_S * dt"):
        assert marker in VIEWER, marker


def test_stick_gestures_die_with_their_input_source() -> None:
    """FR-010: a vanished controller drops its latch — no runaway motion."""

    assert "stickGestures.delete" in VIEWER


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_nav_math_computes_per_contract() -> None:
    """Execute the real remap/hysteresis/basis/EMA/dt math in node (046 pattern).

    Text tripwires cannot catch a ramp off-by-one or a wrong EMA alpha — the shipped
    origin-pivot/room-axis/wandering-anchor bugs all passed every assertion above.
    """

    harness = Path(__file__).parent.parent / "web_harness" / "nav_headless.mjs"
    proc = subprocess.run(
        ["node", str(harness), str(STATIC_DIR)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["target"] == "nav"
    assert summary["failures"] == [], summary["failures"]
    assert summary["checks"] > 20
