"""Shared helpers for scanning RENDERED text with the identity guard (feature 137).

The private inputs are the developer's own (denylist, 134 mapping, live config), read-only from
their real locations — exactly as the in-suite tree test does — so rendered prompts and tool
text are checked against the real deployment's identity, not only against shapes. On a machine
without them (a public clone) only the shape checks run.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from obi.core.demo.gate import FATAL
from obi.core.identity.guard import IdentityGuard, load_guard_config
from obi.core.identity.tokens import IdentityTokens, build_tokens, default_denylist_path
from tests.conftest import REAL_XDG_STATE_HOME

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def private_tokens() -> IdentityTokens:
    from obi.core.config import default_config_path

    return build_tokens(
        denylist=default_denylist_path(),
        mapping=Path(REAL_XDG_STATE_HOME) / "obi" / "demo" / "mapping.json",
        config=default_config_path(),
    )


@lru_cache(maxsize=1)
def _guard() -> IdentityGuard:
    return IdentityGuard(private_tokens(), load_guard_config(ROOT))


def fatal_in(text: str, label: str) -> list[str]:
    """``category/check line N`` for every fatal finding (never the value)."""

    guard = _guard()
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip():
            out.extend(
                f"{f.category}/{f.check} line {n}"
                for f in guard.check_line(line, label, f"line {n}")
                if f.severity == FATAL
            )
    return out
