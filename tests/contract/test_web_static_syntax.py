"""Every served ES module must actually PARSE.

Added by 119. The web suite asserts on served JS almost entirely by grepping source text —
`viewer.js` needs three.js and a WebGL context, so no test can import it — which means a plain
syntax error in the largest, most-edited module in the tree would pass the whole suite and
break the viewer completely on the next reload.

That is not hypothetical: 119 introduced exactly such a bug and this check caught it. An HTML
comment written inside `widgets/xr.js`'s `host.innerHTML` template literal contained backticks,
which terminated the template early. Every text tripwire stayed green.

`node --check` is the cheapest possible guard. It parses only — it does not resolve imports, so
bare specifiers like `three` (satisfied by the page's import map) are fine, and nothing is
executed. Files are copied to `.mjs` so node parses them as modules rather than scripts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def served_modules() -> list[Path]:
    """Every served .js except the vendored bundles (not ours, and huge)."""
    return sorted(
        path
        for path in STATIC_DIR.glob("**/*.js")
        if "vendor" not in path.relative_to(STATIC_DIR).parts
    )


def test_there_are_served_modules_to_check() -> None:
    """Guard the guard: a glob that silently matches nothing would make this suite vacuous."""
    modules = served_modules()
    assert len(modules) >= 10, [p.name for p in modules]
    names = {p.name for p in modules}
    assert {"viewer.js", "bands.js", "shell.js"} <= names


@pytest.mark.parametrize("module", served_modules(), ids=lambda p: p.name)
def test_served_module_parses(module: Path, tmp_path: Path) -> None:
    # node decides script-vs-module by extension; .mjs forces module parsing so `import`
    # statements at the top of every one of these files are legal.
    staged = tmp_path / f"{module.stem}.mjs"
    staged.write_text(module.read_text(encoding="utf-8"), encoding="utf-8")

    proc = subprocess.run(
        ["node", "--check", str(staged)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"{module.relative_to(STATIC_DIR)} does not parse:\n{proc.stderr}"
    )
