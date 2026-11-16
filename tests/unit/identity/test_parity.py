"""Parity rendering + snapshots (T012): pure, owner-only, comparable across code versions."""

from __future__ import annotations

import socket
import sqlite3
import stat
from pathlib import Path

import pytest

from obi.core.config import load_config
from obi.core.identity import parity

SURFACES = {
    "prompt.chat", "prompt.voice", "prompt.radio", "prompt.watch", "prompt.mcp", "tools.chat",
    "tools.mcp", "tools.watch", "tools.radio.safe", "tools.radio.operator", "radio.vocab",
    "settings",
}  # fmt: skip


def test_render_has_every_surface_without_io(fake_config: Path, monkeypatch) -> None:
    def boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("parity rendering performed I/O")

    config = load_config(fake_config)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(sqlite3, "connect", boom)
    surfaces = parity.render_surfaces(config)
    assert set(surfaces) == SURFACES
    errors = {k: v for k, v in surfaces.items() if v.startswith("<render error")}
    assert errors == {}
    assert "## " in surfaces["tools.chat"]
    assert "radio.known_callers" not in surfaces["settings"]  # only the moved keys
    assert "paging.slack.recipients" in surfaces["settings"]


def test_record_is_owner_only_and_round_trips(fake_config: Path) -> None:
    config = load_config(fake_config)
    path = parity.record(config, "pre-137")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert parity.load("pre-137") == parity.render_surfaces(config)


def test_bad_label_is_refused() -> None:
    with pytest.raises(ValueError):
        parity.snapshot_path("../escape")


def test_compare_classifies_surfaces() -> None:
    a = {"x": "same", "y": "ask about northfield-gw\nok", "z": "one", "w": "only-a"}
    b = {"x": "same", "y": "ask about site-gw-1\nok", "z": "two", "v": "only-b"}
    result = {
        d.surface: d.status for d in parity.compare(a, b, intended=[("y", r"-gw")], wording=False)
    }
    assert result == {
        "x": "equal",
        "y": "intended",
        "z": "different",
        "w": "missing",
        "v": "missing",
    }


def test_wording_surfaces_are_intended_unless_tool_set_changes() -> None:
    a = {
        "prompt.chat": "old words",
        "tools.chat": "## x\nold",
        "tools.mcp": "## x\nold",
        "settings": "k = 1",
        "radio.vocab": "v",
    }
    b = {
        "prompt.chat": "new words",
        "tools.chat": "## x\nnew",
        "tools.mcp": "## y\nnew",
        "settings": "k = 2",
        "radio.vocab": "v",
    }
    result = {d.surface: d.status for d in parity.compare(a, b)}
    assert result == {
        "prompt.chat": "intended",
        "tools.chat": "intended",
        "tools.mcp": "different",
        "settings": "different",
        "radio.vocab": "equal",
    }
