"""Function words are never place words, so replayed prose is not garbled."""

from __future__ import annotations

from obi.core.demo.naming import GENERIC_TOKENS


def test_function_words_are_generic() -> None:
    for word in ("not", "where", "was", "new", "back", "missing", "update", "offline"):
        assert word in GENERIC_TOKENS, word
