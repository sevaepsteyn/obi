"""Unit (008): load_context is total — happy path plus every failing-file case, never raises."""

from __future__ import annotations

from pathlib import Path

from obi.chat.context import MAX_CONTEXT_BYTES, load_context
from obi.core.config import ChatSettings


def _settings(path: Path | None) -> ChatSettings:
    return ChatSettings(context_file=path)


def test_unset_is_a_noop() -> None:
    load = load_context(_settings(None))
    assert load.text is None
    assert load.warning is None
    assert load.source is None


def test_happy_load_returns_verbatim_text(tmp_path: Path) -> None:
    content = "chestnut is site 5; gateways are <site>-gw"
    path = tmp_path / "context.md"
    path.write_text(content)

    load = load_context(_settings(path))

    assert load.text == content  # verbatim, unparsed (FR-008)
    assert load.warning is None
    assert load.source == path
    assert load.size_bytes == len(content.encode())


def test_missing_file_warns_and_yields_no_context(tmp_path: Path) -> None:
    load = load_context(_settings(tmp_path / "nope.md"))
    assert load.text is None
    assert load.warning is not None and "not found" in load.warning


def test_empty_file_is_ignored_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "empty.md"
    path.write_text("   \n\t\n")
    load = load_context(_settings(path))
    assert load.text is None
    assert load.warning is not None and "empty" in load.warning


def test_oversized_file_is_skipped_not_truncated(tmp_path: Path) -> None:
    path = tmp_path / "big.md"
    path.write_text("x" * (MAX_CONTEXT_BYTES + 1))

    load = load_context(_settings(path))

    assert load.text is None  # skipped entirely — never a partial/truncated inject (FR-006)
    assert load.warning is not None and "KiB" in load.warning


def test_unreadable_path_warns_without_raising(tmp_path: Path) -> None:
    # A directory is a stat-able but unreadable "file": read_text raises IsADirectoryError, which
    # the loader must turn into a warning rather than letting it escape.
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    load = load_context(_settings(a_dir))
    assert load.text is None
    assert load.warning is not None
