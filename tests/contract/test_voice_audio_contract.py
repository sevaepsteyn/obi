"""Contract test (T010): audio is transient — never written to disk, never retained (FR-003)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.fakes import chat_config, chat_runtime, text_response, voice_session


def _tree(path: Path, *, own_root: Path | None = None) -> set[Path]:
    """Every file under ``path`` — skipping OTHER pytest invocations' retained tmp roots.

    pytest keeps the last few ``pytest-of-*/pytest-N`` roots under the shared system tempdir
    and prunes/creates them on its own schedule; those races are neighbour noise, not audio
    this turn spooled. ``own_root`` (this invocation's numbered root) stays in scope.
    """

    files: set[Path] = set()
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        foreign_pytest_root = (
            own_root is not None
            and any(part.startswith("pytest-of-") for part in p.parts)
            and own_root not in p.parents
        )
        if foreign_pytest_root:
            continue
        files.add(p)
    return files


def test_a_turn_writes_no_audio_anywhere_and_retains_no_buffer(tmp_path: Path) -> None:
    import numpy as np

    config = chat_config(tmp_path)
    session = voice_session(
        chat_runtime(config, responses=[text_response("done")]), input_lines=["", "q"]
    )
    tmpdir = Path(tempfile.gettempdir())
    own_root = tmp_path.parent.parent  # …/pytest-of-<user>/pytest-N — THIS invocation's root
    before_state = _tree(tmp_path)
    before_tmp = _tree(tmpdir, own_root=own_root)

    assert session.run() == 0

    # No new file anywhere a buffer could have been spooled — the only permitted write is the
    # audit trail (JSONL text) under the config's state dir.
    new_state = {p for p in _tree(tmp_path) - before_state if p.name != "audit.jsonl"}
    assert new_state == set()
    assert _tree(tmpdir, own_root=own_root) - before_tmp == set()
    # And the session object graph holds no audio array after the turn completes.
    held = [v for v in vars(session).values() if isinstance(v, np.ndarray)]
    assert held == []
