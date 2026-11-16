"""Contract: the workspace chrome is ONE bar, and the reachability indicator says what it means.

Feature 077 retired the top bar. These are source-level pins over the served shell — the cheap
tier of the three-tier strategy in ``specs/077-shell-chrome/research.md`` R6. They catch a
resurrected top bar, a reintroduced "live" reachability claim, or a leftover ``--topbar-h``
reference; they say nothing about layout (no box model here), which is a real-browser job.

One of these is a REGRESSION GUARD rather than a requirement: ``LAUNCHER_WIDGETS`` must SURVIVE the
"+" launcher's removal, because it also feeds the command palette's suggestions (research R3). The
obvious "clean up the dead launcher constant" follow-up would silently break FR-005/FR-026, so the
guard is here to stop it.
"""

from __future__ import annotations

from obi.web.server import STATIC_DIR

SHELL_JS = (STATIC_DIR / "shell.js").read_text(encoding="utf-8")
THEME_CSS = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")


def test_no_top_bar_survives_in_the_shell() -> None:
    """FR-001: the boot template builds no top bar."""
    assert 'id="topbar"' not in SHELL_JS
    assert "topbar" not in SHELL_JS


def test_no_top_bar_survives_in_the_stylesheet() -> None:
    """FR-001: no rule dresses a top bar that no longer exists."""
    assert "#topbar" not in THEME_CSS


def test_workspace_offset_token_is_gone_everywhere() -> None:
    """R3: --topbar-h is REMOVED, not zeroed — a zeroed token invites a 'restore'.

    Swept across every served file, not just the two this feature edits: a straggling reference
    anywhere would resolve to nothing and silently collapse a layout.
    """
    stragglers = [
        path.name
        for path in sorted(STATIC_DIR.rglob("*"))
        if path.is_file()
        and path.suffix in {".js", ".css", ".html"}
        and "topbar-h" in path.read_text(encoding="utf-8")
    ]
    assert stragglers == []


def test_reachability_indicator_reads_connected_not_live() -> None:
    """FR-008 / FR-009: it only ever meant 'this browser reached the server'.

    'live' overstated that — it reads as a freshness claim the probe never makes (FR-010; widgets
    wear their own age badges).
    """
    assert "● connected" in SHELL_JS
    assert "● live" not in SHELL_JS
    assert "○ server unreachable" in SHELL_JS  # the failing wording is unchanged


def test_no_live_class_dresses_the_indicator() -> None:
    """FR-009: the word is gone from the chrome's vocabulary, not just its visible text."""
    assert "#conn.live" not in THEME_CSS
    assert "#conn.connected" in THEME_CSS
    assert "#conn.failing" in THEME_CSS


def test_touch_launcher_button_is_gone() -> None:
    """FR-025: the '+' launcher was dropped, not relocated (operator decision).

    The Obi menu is the window switcher — and since the 2026-07-17 headset pass (078 round 5) it
    also carries the launcher section 077 predicted as the "cheap fix", because touch users had
    no tap path to widgets absent from their browser's persisted layout. The standalone '+'
    button stays gone.
    """
    assert "menu-button" not in SHELL_JS
    assert "#menu-button" not in THEME_CSS


def test_the_obi_menu_carries_the_widget_launcher_section() -> None:
    """078 round 5 (headset field pass): the Obi menu lists the full widget catalog below the
    open windows — tap to open, through the same open() the command line uses (singleton focus
    and multi-instance semantics identical)."""

    assert "'open a widget'" in SHELL_JS
    section = SHELL_JS[SHELL_JS.find("'open a widget'"):]
    # rows come from the shared, sorted catalog — through the gate-filtered view of it
    assert "launcherWidgets()" in section
    assert "open(w.id)" in section  # the one sanctioned open path
    assert "#menu .sect" in THEME_CSS


def test_launcher_widgets_survives_the_launcher() -> None:
    """R3 REGRESSION GUARD — not a requirement of the feature, a fence around it.

    LAUNCHER_WIDGETS looks like dead launcher state now that '+' is gone. It is not: it still feeds
    the command palette's suggestion list and the unknown-?w= help text — since the widget gate,
    via launcherWidgets(), the gate-filtered view of it. Deleting either regresses FR-005 / FR-026,
    which are the whole reason dropping the touch launcher was acceptable.
    """
    assert "LAUNCHER_WIDGETS" in SHELL_JS
    # the filtered view's render paths: help text + 2 suggestion paths + the menu section
    assert SHELL_JS.count("launcherWidgets()") >= 4
