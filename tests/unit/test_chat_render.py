"""Terminal answer rendering (feature 082): markdown renders; bracketed text is never corrupted."""

from __future__ import annotations

from rich.console import Console

from obi.chat.session import print_answer


def _rendered(answer: str) -> str:
    console = Console(record=True, width=100, force_terminal=False)
    print_answer(console, answer)
    return console.export_text()


def test_markdown_emphasis_renders_without_literal_symbols() -> None:
    out = _rendered("**tamarack-gw** is up for **7 weeks**.")
    assert "tamarack-gw" in out and "7 weeks" in out
    assert "**" not in out  # the bold markers are consumed, not printed literally


def test_bracketed_text_is_preserved_and_never_parsed_as_markup() -> None:
    # The old f-string print let rich parse `[ether1]` as console markup (corruption/crash risk).
    # A Markdown renderable does not — the literal token survives.
    out = _rendered("interface [ether1] is flapping and [unknown-tag] appears")
    assert "[ether1]" in out
    assert "[unknown-tag]" in out


def test_plain_answer_text_is_preserved() -> None:
    out = _rendered("tamarack-gw is healthy — CPU at 1% and 3.7 GiB free.")
    assert "tamarack-gw is healthy" in out
    assert "CPU at 1%" in out
