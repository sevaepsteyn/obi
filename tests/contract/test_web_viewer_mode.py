"""Contract tests: the 119 in-session AR/VR backdrop toggle.

Replaces the 031-follow-up "one scene, two backdrops" suite, which pinned a mode picked on the
2D setup form and baked into `requestSession`. The viewer now runs ONE session type and treats
"VR" as a runtime backdrop the operator flips from the settings panel.

Normative contract: specs/119-xr-backdrop/contracts/xr-backdrop.md.

These are source tripwires — the same discipline as the other viewer suites, because no test
can load viewer.js (it needs three.js and a WebGL context). The part that CAN be executed —
the blend-mode gate and the per-backdrop terrain-style key — lives in bands.js and is run for
real by tests/web_harness/bands_modes_headless.mjs (see test_web_bands_modes.py). That split is
deliberate: the failure mode this feature is most likely to ship, `scene.background` set to a
THREE.Color rendering nothing at all in an alpha-blend session, is invisible to every grep.
"""

from __future__ import annotations

from obi.web.server import STATIC_DIR

VIEWER = (STATIC_DIR / "viewer.js").read_text(encoding="utf-8")
BANDS = (STATIC_DIR / "bands.js").read_text(encoding="utf-8")
XR_WIDGET = (STATIC_DIR / "widgets" / "xr.js").read_text(encoding="utf-8")


def code_only(source: str) -> str:
    """Drop whole-line comments so the "must NOT appear" tripwires below judge CODE, not prose.

    This codebase comments densely and on purpose — the r160 clear-override mechanism, for
    instance, is only comprehensible if the source quotes it. A negative grep over raw text
    would then forbid explaining the very thing it guards, which pushes future authors toward
    thinner comments to appease a test. Only full-line comments are stripped: mid-line `//`
    would also eat the `https://` in string literals, and nothing here needs that.
    """
    kept, in_block = [], False
    for line in source.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = "*/" not in stripped
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        kept.append(line)
    return "\n".join(kept)


VIEWER_CODE = code_only(VIEWER)
XR_WIDGET_CODE = code_only(XR_WIDGET)


def test_one_session_type_with_a_vr_fallback() -> None:
    """C1: immersive-ar wherever supported — its alpha-blend composite is what lets an opaque
    backdrop hide reality, so one AR session can present both looks. immersive-vr survives only
    as the fallback for runtimes without passthrough."""

    assert "'immersive-ar'" in VIEWER
    assert "'immersive-vr'" in VIEWER
    assert "canAR ? 'immersive-ar' : 'immersive-vr'" in VIEWER


def test_the_viewer_never_probes_support_inside_the_activation_window() -> None:
    """C1: requestSession CONSUMES transient user activation, so AR support cannot be discovered
    by trying it and falling back — a second attempt in the same handler fails with a security
    error whatever the real cause was. Support is resolved by the widget at mount and passed in."""

    assert "isSessionSupported" not in VIEWER_CODE
    assert "canAR" in VIEWER
    # ...and the widget is where the probe actually lives.
    assert "isSessionSupported('immersive-ar')" in XR_WIDGET


def test_the_opaque_backdrop_is_a_texture_not_a_color() -> None:
    """C2 — the load-bearing detail of the whole feature.

    three.js r160's WebGLBackground.render turns a Color background into a clear color, then
    overrides that clear from the XR blend mode ((0,0,0,0) for alpha-blend) before clearing. A
    Color background has no geometry, so under an always-AR session it renders NOTHING and
    "opaque" would look identical to passthrough. A texture background takes the other branch:
    a real full-screen mesh, drawn after the clear, immune to the override.
    """

    assert "THREE.DataTexture" in VIEWER
    assert "THREE.SRGBColorSpace" in VIEWER  # or the shade drifts from 0x0b0f13
    assert "scene.background = opaque ? OPAQUE_BACKDROP : null" in VIEWER
    # The regression this guards: any reintroduction of a Color background.
    assert "scene.background = new THREE.Color" not in VIEWER_CODE
    # Nothing may reintroduce a clear-color dependency either.
    for banned in ("setClearColor", "setClearAlpha", "autoClear", "scene.fog"):
        assert banned not in VIEWER_CODE, banned


def test_the_toggle_is_gated_on_the_live_blend_mode() -> None:
    """C3: ask the session what it actually composites rather than assuming from what we asked
    for. Only alpha-blend can show both backdrops; opaque and additive get no row at all."""

    assert "getEnvironmentBlendMode()" in VIEWER
    assert "backdropAvailable(" in VIEWER
    assert "backdropAvailable" in BANDS  # the decision itself is the executable helper
    assert "canFlipBackdrop" in VIEWER


def test_the_backdrop_row_lives_in_the_panel_and_takes_no_button() -> None:
    """C4: a scene-section row, availability-gated exactly as 098 gates the city grid. Both face
    buttons are already assigned (A/X view modes, B/Y terrain) and that assignment stands."""

    assert "toggleRow('backdrop'" in VIEWER
    assert "state.backdropAvailable" in VIEWER
    assert "case 'backdrop'" in VIEWER
    assert "function toggleBackdrop" in VIEWER
    assert "BACKDROP_BUTTON" not in VIEWER_CODE  # no new controller binding
    # The help sheet must name it — it is the only way to discover the control.
    assert "gear → backdrop" in VIEWER


def test_the_flip_never_tears_the_session_down() -> None:
    """C5: the flip is a repaint, not a restart. toggleBackdrop must not end the session, build
    a world, or fetch anything — that is what preserves viewpoint, layers, open surfaces and an
    in-flight voice turn by construction rather than by bookkeeping."""

    body = VIEWER.split("function toggleBackdrop", 1)[1].split("\n  }", 1)[0]
    for banned in ("session.end", "requestSession", "fetch(", "buildWorld", "new THREE.Scene"):
        assert banned not in body, banned
    assert "saveBackdropOpaque" in body  # the choice persists
    assert "applyTerrainStyle" in body  # ...and the terrain style follows the backdrop
    assert "refreshPanel" in body


def test_one_preference_owns_the_backdrop() -> None:
    """C6: the picker and the in-session row go through the same two helpers, so they cannot
    disagree — the specific bug being designed out is an in-headset choice silently overridden
    by a stale setup-form value on the next entry. The old per-tab key is gone."""

    assert "obi.viewer.backdrop" in BANDS
    assert "loadBackdropOpaque" in XR_WIDGET
    assert "saveBackdropOpaque" in XR_WIDGET
    assert "loadBackdropOpaque" in VIEWER
    assert "saveBackdropOpaque" in VIEWER
    assert "obi-ar-mode" not in XR_WIDGET_CODE
    # The widget still owns no storage of its own (test_web_workspace's owners rule).
    assert "localStorage" not in XR_WIDGET_CODE


def test_the_setup_picker_chooses_the_starting_backdrop_not_the_session_type() -> None:
    """C7: the picker survives, but it now seeds the backdrop rather than selecting a session
    type — so entry must not be gated on support for a mode the viewer never requests."""

    assert 'id="xr-mode"' in XR_WIDGET
    assert "startOpaque" in XR_WIDGET
    assert "{ canAR, startOpaque }" in XR_WIDGET
    assert "canAR || canVR" in XR_WIDGET  # gated on immersive support at all
    assert "isSessionSupported(sessionType)" not in XR_WIDGET_CODE  # the per-mode gate is gone
    # The operator is told the choice is switchable in the headset (FR-013)...
    assert "gear &rarr; backdrop" in XR_WIDGET or "gear → backdrop" in XR_WIDGET
    # ...and how to recover from a refused session (FR-014, C1: a fresh gesture, not a retry).
    assert "XR_RETRY" in XR_WIDGET
    # The setup's one-shot geometry fetch survives (029) — no new fetch in the widget.
    assert XR_WIDGET.count("fetch(") == 1
    # 119 added a second `.note` div above the existing one, so the mic/voice status line must
    # be addressed by its own class — `querySelector('.note')` silently returns whichever comes
    # first in the markup, which would send voice status into the backdrop hint.
    assert "querySelector('.note')" not in XR_WIDGET_CODE
    assert "querySelector('.status-note')" in XR_WIDGET_CODE


def test_the_shell_probe_is_untouched() -> None:
    """C7: shell.js still decides whether the XR window joins the default workspace layout, and
    still accepts either session type. 119 changes nothing there."""

    shell = (STATIC_DIR / "shell.js").read_text(encoding="utf-8")
    assert "immersiveSupported" in shell
    assert "isSessionSupported('immersive-vr')" in shell
    assert "isSessionSupported('immersive-ar')" in shell
