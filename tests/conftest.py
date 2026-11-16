"""Suite-wide isolation (feature 042).

Every test runs with ``XDG_STATE_HOME`` pointed at a per-test temp directory so that code
paths resolving default state locations — most importantly the audit event store, which is
ON by default — can never write to the developer's real ``~/.local/state/obi``. Tests that
need a specific state root still win by setting the variable themselves (their monkeypatch
runs after this fixture).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

#: The developer's REAL state root, captured before any per-test isolation (feature 137): the
#: in-suite identity guard reads the private 134 mapping from it, read-only.
REAL_XDG_STATE_HOME = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")


@pytest.fixture(autouse=True)
def _isolate_xdg_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
