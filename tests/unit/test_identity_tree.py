"""The whole tracked tree under the identity guard (feature 137, T022/T084).

ENFORCING: zero FATAL findings, in two runs:

* private — the developer's own denylist, 134 mapping and config, read-only from their REAL
  XDG locations (captured in tests/conftest.py before per-test isolation); reported as skipped,
  never silently passed, on a machine that has none (a public clone);
* shape-only — exactly what a public clone enforces.

Failures name file, line and category only — never the matched value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.identity.guard import scan_paths
from obi.core.identity.tokens import LOADED, build_tokens, default_denylist_path
from tests.conftest import REAL_XDG_STATE_HOME

ROOT = Path(__file__).resolve().parents[2]


def _private_tokens():
    from obi.core.config import default_config_path

    return build_tokens(
        denylist=default_denylist_path(),
        mapping=Path(REAL_XDG_STATE_HOME) / "obi" / "demo" / "mapping.json",
        config=default_config_path(),
    )


def _enforce(mode: str, result) -> None:  # type: ignore[no-untyped-def]
    where = [f"{f.file} {f.locator} {f.category}/{f.check}" for f in result.fatal]
    assert not where, (
        f"identity guard: {len(where)} FATAL finding(s) in {mode} mode — scrub them "
        "(`obi identity scan` shows the same list):\n  " + "\n  ".join(where[:50])
    )


def test_tree_shape_only() -> None:
    result = scan_paths(None, ROOT, build_tokens(denylist=None, mapping=None, config=None))
    _enforce("shape", result)


def test_tree_private() -> None:
    tokens = _private_tokens()
    if not tokens.has_private:
        pytest.skip(f"no private identity inputs on this machine: {tokens.inputs}")
    result = scan_paths(None, ROOT, tokens)
    assert all(v == LOADED or v.startswith("skipped") for v in result.inputs.values())
    _enforce("private", result)
