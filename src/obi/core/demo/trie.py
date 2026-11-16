"""Compile a large set of literal tokens into one trie-shaped regex (feature 134).

A plain ``a|b|c|…`` alternation over thousands of literals makes Python's ``re`` try every
alternative at every position — the first live snapshot spent most of its two hours there. A
trie pattern (``north(?:field|gate)``) branches on characters instead, so the cost tracks the token
length, not the token count. Greedy optional groups keep longest-match semantics (``core-gw-1``
wins over ``core-gw``). A pure algorithm: both the rewriter and the gate use it, each over its
own token set — it carries no knowledge of what is being matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_END = ""


def _insert(root: dict[str, Any], word: str) -> None:
    node = root
    for ch in word:
        node = node.setdefault(ch, {})
    node[_END] = True


def _emit(node: dict[str, Any]) -> str:
    optional = _END in node
    branches: list[str] = []
    singles: list[str] = []
    for ch in sorted(k for k in node if k != _END):
        child = node[ch]
        if set(child) == {_END}:
            singles.append(ch)
        else:
            branches.append(re.escape(ch) + _emit(child))
    if singles:
        branches.append(
            re.escape(singles[0])
            if len(singles) == 1
            else "[" + "".join(re.escape(c) for c in singles) + "]"
        )
    if not branches:
        return ""
    body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
    if optional:
        return "(?:" + body + ")?"
    return body


def trie_pattern(words: Iterable[str]) -> str | None:
    """A regex source matching exactly the given non-empty literals, longest first."""

    root: dict[str, Any] = {}
    count = 0
    for word in words:
        if word:
            _insert(root, word)
            count += 1
    if not count:
        return None
    return "(?:" + _emit(root) + ")"
