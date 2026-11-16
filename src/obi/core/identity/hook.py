"""The identity pre-commit hook (feature 137, clarification Q5): scan only the lines a commit ADDS.

Import-light (no CLI framework, no store modules). Reads ``git diff --cached -U0``, maps each
added line to its new-file line number, runs the identity guard over it with whatever private
inputs exist on this machine, prints value-free findings, and exits 1 on any fatal finding.
An unrelated edit to a file that still holds real values elsewhere is never blocked.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from obi.core.demo.gate import FATAL, LeakFinding
from obi.core.identity.guard import IdentityGuard, load_guard_config
from obi.core.identity.tokens import IdentityTokens, build_tokens

_FILE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    """``(path, new_line_number, text)`` for every ``+`` line of a unified diff (``-U0``)."""

    out: list[tuple[str, int, str]] = []
    path: str | None = None
    line_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            m = _FILE.match(raw)
            path = m.group(1) if m else None
            continue
        if raw.startswith("--- "):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            out.append((path, line_no, raw[1:]))
            line_no += 1
        elif raw.startswith(" "):
            line_no += 1
    return out


def scan_added_lines(diff_text: str, root: Path, tokens: IdentityTokens) -> list[LeakFinding]:
    config = load_guard_config(root)
    guard = IdentityGuard(tokens, config)
    out: list[LeakFinding] = []
    for path, n, text in added_lines(diff_text):
        if config.excluded(path) or not text.strip():
            continue
        out.extend(
            f for f in guard.check_line(text, path, f"line {n} (added)") if f.severity == FATAL
        )
    return out


def main() -> int:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    diff = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout
    if not diff.strip():
        return 0
    tokens = build_tokens()
    findings = scan_added_lines(diff, root, tokens)
    if not findings:
        return 0
    print("identity guard: this commit ADDS deployment identity (feature 137):", file=sys.stderr)
    for f in findings:
        print(f"  [FATAL] {f.category}/{f.check} {f.file} {f.locator} @{f.offset}", file=sys.stderr)
    print("Fix with `obi identity scrub <path>` or edit by hand; never commit real values.",
          file=sys.stderr)  # fmt: skip
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
