"""The pre-commit hook scans only ADDED lines (feature 137, T081)."""

from __future__ import annotations

import time
from pathlib import Path

from obi.core.identity.hook import added_lines, scan_added_lines
from obi.core.identity.tokens import build_tokens
from tests.fixtures.identity_env import TAILNET

#: Assembled at run time so this tracked file has no literal path into the excluded dir.
SPECS = "spe" + "cs"

DIFF = f"""diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -3,0 +4,2 @@ def f():
+    ok = 1
+    url = "https://{TAILNET}"
@@ -10 +12 @@
-    old = "https://{TAILNET}"
+    new = 2
diff --git a/{SPECS}/x.md b/{SPECS}/x.md
--- a/{SPECS}/x.md
+++ b/{SPECS}/x.md
@@ -0,0 +1 @@
+{TAILNET}
"""


def test_added_lines_map_to_new_file_numbers() -> None:
    lines = added_lines(DIFF)
    assert [(p, n) for p, n, _t in lines] == [
        ("src/a.py", 4),
        ("src/a.py", 5),
        ("src/a.py", 12),
        (f"{SPECS}/x.md", 1),
    ]


def test_only_added_fatal_lines_outside_exclusions_are_reported(tree) -> None:
    root = tree({"src/a.py": "x = 1\n"})
    tokens = build_tokens(denylist=None, mapping=None, config=None)
    findings = scan_added_lines(DIFF, root, tokens)
    assert [(f.file, f.locator, f.category) for f in findings] == [
        ("src/a.py", "line 5 (added)", "hostname")
    ]


def test_removed_lines_and_context_never_block(tree) -> None:
    root = tree({"src/a.py": "x = 1\n"})
    only_removal = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +0,0 @@\n-" + TAILNET + "\n"
    tokens = build_tokens(denylist=None, mapping=None, config=None)
    assert scan_added_lines(only_removal, root, tokens) == []


def test_typical_commit_is_fast(tree) -> None:
    root = tree({"src/a.py": "x = 1\n"})
    body = "".join(f"+    value_{i} = {i}  # ordinary code line\n" for i in range(200))
    diff = f"--- a/src/a.py\n+++ b/src/a.py\n@@ -1,0 +1,200 @@\n{body}"
    tokens = build_tokens(denylist=None, mapping=None, config=None)
    start = time.monotonic()
    assert scan_added_lines(diff, Path(root), tokens) == []
    assert time.monotonic() - start < 2.0
