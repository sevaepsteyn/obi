"""Unit tests for the trie regex compiler (feature 134)."""

from __future__ import annotations

import random
import re

from obi.core.demo.trie import trie_pattern


def test_matches_exactly_the_literals_longest_first() -> None:
    src = trie_pattern(["core-gw", "core-gw-1", "north", "northfield", "a.b"])
    assert src is not None
    pat = re.compile(src)
    assert pat.fullmatch("core-gw-1") and pat.fullmatch("northfield") and pat.fullmatch("a.b")
    assert pat.match("core-gw-12").group(0) == "core-gw-1"  # type: ignore[union-attr]
    assert not pat.fullmatch("axb") and not pat.fullmatch("nort")


def test_equivalent_to_plain_alternation_on_random_words() -> None:
    rng = random.Random(7)
    words = {"".join(rng.choice("abc-") for _ in range(rng.randint(1, 6))) for _ in range(300)}
    trie = re.compile(trie_pattern(words) or "")
    plain = re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)))
    for _ in range(500):
        text = "".join(rng.choice("abc- ") for _ in range(30))
        assert [m.group(0) for m in trie.finditer(text)] == [
            m.group(0) for m in plain.finditer(text)
        ]


def test_empty() -> None:
    assert trie_pattern([]) is None
