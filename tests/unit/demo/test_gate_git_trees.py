"""A git tree's raw hash bytes are never scanned as text (the feature 138 parallel-run flake).

A tree object is ``<mode> <name>\\0<20 raw hash bytes>`` per entry. Decoding the whole body as
text let hash bytes that happen to spell an email shape raise a FATAL finding — about one
full parallel run in twenty, since each test environment seeds its own mapping (hence hashes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core import configstore
from obi.core.demo.gate import LeakGate, tree_names
from obi.core.demo.harvest import Harvest

#: 20 "hash" bytes that read as an email address when decoded as text.
AT = bytes([64])
EMAILISH_HASH = b"qq7" + AT + b"pqr.stu" + bytes(range(1, 10))


def _tree(*entries: tuple[str, bytes]) -> bytes:
    return b"".join(b"100644 " + name.encode() + b"\0" + digest for name, digest in entries)


def test_tree_names_skips_the_raw_hashes() -> None:
    body = _tree(("core-gw-1.rsc", EMAILISH_HASH), ("notes.txt", b"\x00" * 20))
    assert tree_names(body) == ["core-gw-1.rsc", "notes.txt"]


def test_gate_does_not_read_hash_bytes_as_an_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _tree(("core-gw-1.rsc", EMAILISH_HASH))
    blob = b"/system identity set name=core-gw-1"
    dump = (
        b"a" * 40 + b" tree " + str(len(body)).encode() + b"\n" + body + b"\n"
        + b"b" * 40 + b" blob " + str(len(blob)).encode() + b"\n" + blob + b"\n"
    )  # fmt: skip
    monkeypatch.setattr(configstore, "dump_objects", lambda _repo: dump)
    (tmp_path / ".git").mkdir()
    out: list = []
    LeakGate(Harvest(), dictionary=frozenset())._scan_git(tmp_path, "config-repo", out)
    assert not [f for f in out if f.severity == "FATAL"], out
