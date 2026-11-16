"""Shape checks that need no private data (T016) — what a public clone's guard enforces.

Every plant is ASSEMBLED at run time (``"x" + "y"``) so this tracked file does not itself match.
"""

from __future__ import annotations

import pytest

from obi.core.identity.guard import scan_paths
from obi.core.identity.tokens import build_tokens

NO_PRIVATE = {"denylist": None, "mapping": None, "config": None}

FATAL_PLANTS = {
    "secret": "key = '" + "sk-" + "ant-" + "realLooking0123'",
    "secret-slack": "token = '" + "xo" + "xb-" + "1234-5678-abcd'",
    "pem": "-----BEGIN " + "PRIVATE KEY-----",
    "wg": "private-key=" + '"' + "A" * 42 + "A=",
    "tailnet": "url = 'https://lights.example-net." + "ts" + ".net/json'",
    "chat_id": "allow = ['" + "U0" + "ABQZ7X9K2" + "']",
    "channel_id": "channel = '" + "C0" + "BQZ7X9K2P" + "'",
    "mac": "mac = '" + ":".join(["3c", "22", "fb", "12", "34", "56"]) + "'",
    "public_ip": "host = '" + ".".join(["93", "184", "215", "14"]) + "'",
    "email": "contact = 'someone@" + "realcompany" + ".com'",
}

CLEAN = [
    "fake = '" + "sk-" + "ant-" + "fake-key-0001'",  # a fake-marked secret in a test
    "allow = ['" + "U0" + "FAKE0001" + "']",  # allowed fake chat ID
    "mac = '02:00:00:aa:bb:cc'",  # locally administered
    "mac = 'aa:bb:cc:dd:ee:ff'",
    "addr = '10.250.0.1'",  # RFC 1918 passes in shape-only mode
    "addr = '172.20.0.1/24'",
    "doc = '192.0.2.10 and 198.51.100.7 and 203.0.113.9'",
    "contact = 'ops@example.net'",
    "resolver = '1.1.1.1'",
]


def _scan(tree, files: dict[str, str]):
    root = tree(files)
    return scan_paths(None, root, build_tokens(**NO_PRIVATE), dictionary=frozenset({"the"}))


@pytest.mark.parametrize("kind", sorted(FATAL_PLANTS))
def test_each_shape_plant_is_fatal_and_not_printed(kind: str, tree) -> None:
    plant = FATAL_PLANTS[kind]
    result = _scan(tree, {"src/mod.py": "x = 1\n" + plant + "\n"})
    fatal = [f for f in result.fatal if f.file == "src/mod.py"]
    assert fatal, kind
    assert all(f.locator == "line 2" for f in fatal)
    report = result.report()
    assert plant not in report
    for fragment in plant.replace("'", " ").replace('"', " ").split():
        if len(fragment) >= 8:
            assert fragment not in report


def test_clean_lines_pass_in_shape_only_mode(tree) -> None:
    result = _scan(tree, {"tests/test_x.py": "\n".join(CLEAN) + "\n"})
    assert [(f.category, f.locator) for f in result.fatal] == []


def test_inputs_reported_as_skipped(tree) -> None:
    result = _scan(tree, {"a.txt": "hello\n"})
    assert "denylist skipped" in result.header() and "mapping skipped" in result.header()
