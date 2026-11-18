"""Contract test (T010): audio is transient — never written to disk, never retained (FR-003)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.fakes import chat_config, chat_runtime, text_response, voice_session


def _tree(path: Path) -> set[Path]:
    """Every file under ``path``."""

    return {p for p in path.rglob("*") if p.is_file()}


def test_a_turn_writes_no_audio_anywhere_and_retains_no_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np

    # The temp dir is where a buffer would be spooled (tempfile.* and anything honouring
    # TMPDIR). Redirect it to a PRIVATE dir under tmp_path so the check sees only this turn's
    # writes — the shared system tempdir is written concurrently by other pytest workers
    # (feature 138: the suite runs under xdist), which made a system-wide scan both flaky
    # and slow (~13s of rglob).
    private_tmp = tmp_path / "systmp"
    private_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(private_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(private_tmp))
    assert Path(tempfile.gettempdir()) == private_tmp

    config = chat_config(tmp_path)
    session = voice_session(
        chat_runtime(config, responses=[text_response("done")]), input_lines=["", "q"]
    )
    before = _tree(tmp_path)

    assert session.run() == 0

    # No new file anywhere a buffer could have been spooled (state dir + temp dir, both under
    # tmp_path) — the only permitted write is the audit trail (JSONL text).
    new = {p for p in _tree(tmp_path) - before if p.name != "audit.jsonl"}
    assert new == set()
    # And the session object graph holds no audio array after the turn completes.
    held = [v for v in vars(session).values() if isinstance(v, np.ndarray)]
    assert held == []
