"""`obi demo chats list|show|approve|revoke` end to end (feature 136, T033)."""

from __future__ import annotations

import contextlib
import functools
import html
import http.server
import json
import os
import signal
import socketserver
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from obi.core.demo import chats as chats_mod
from tests.fixtures.demo.chat_history import add_conversations
from tests.fixtures.demo.harness import build_dataset, offline_terrain, run_cli

# Runs the demo snapshot/export pipeline (seconds per test) — skip with -m 'not slow'.
pytestmark = pytest.mark.slow


@dataclass
class Env:
    config: Path
    dataset: Path
    audit: Path

    def cli(self, *args: str):  # type: ignore[no-untyped-def]
        return run_cli("demo", "chats", *args, "--config", str(self.config),
                       "--dataset", str(self.dataset))  # fmt: skip


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    dataset, config, _truth = build_dataset(tmp_path)
    audit = tmp_path / "xdg" / "obi" / "audit.db"
    add_conversations(audit)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    offline_terrain(monkeypatch)
    real = chats_mod.to_recording

    def seeded_leak(cand, *a, **kw):  # type: ignore[no-untyped-def]
        # Conversation 7's seeded-leak variant: a real token that slips past the rewrite.
        rec = real(cand, *a, **kw)
        if cand.session_id == "chat-0f0f0f0f0f0f":
            rec["turns"][0]["answer"] += " via 10.99.0.1"
        return rec

    monkeypatch.setattr(chats_mod, "to_recording", seeded_leak)
    yield Env(config, dataset, audit)


def _rows(env: Env) -> list[dict]:  # type: ignore[type-arg]
    result = env.cli("list", "--since", "36500d", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_list_show_approve_revoke(env: Env) -> None:
    rows = _rows(env)
    assert len(rows) == 4 and {r["persona"] for r in rows} == {"admin"}
    refused = [r for r in rows if r["status"] == "refused"]
    assert len(refused) == 1  # conversation 7's seeded leak
    assert "10.99.0.1" not in json.dumps(refused[0]["reasons"])  # reasons are token-free
    clean = next(r for r in rows if r["status"] == "new")

    shown = env.cli("show", clean["id"])
    assert shown.exit_code == 0 and json.loads(shown.stdout)["id"] == clean["id"]

    assert env.cli("approve", refused[0]["id"]).exit_code == 3
    assert env.cli("approve", clean["id"]).exit_code == 0
    assert next(r for r in _rows(env) if r["id"] == clean["id"])["status"] == "approved"
    assert env.cli("revoke", clean["id"]).exit_code == 0
    assert next(r for r in _rows(env) if r["id"] == clean["id"])["status"] == "new"
    assert env.cli("approve", "rec-00000000").exit_code == 2

    with sqlite3.connect(env.audit) as db:
        audited = db.execute(
            "SELECT summary, target, payload FROM events WHERE summary LIKE 'demo_chat_%'"
            " ORDER BY ts_us"
        ).fetchall()
    assert [(s.split(" ")[0], t) for s, t, _p in audited] == [
        ("demo_chat_approve", refused[0]["id"]),
        ("demo_chat_approve", clean["id"]),
        ("demo_chat_revoke", clean["id"]),
    ]
    text = json.dumps(audited)
    assert "chat-0" not in text and "slack-C0" not in text  # never the real session id


def test_bad_since_and_table(env: Env) -> None:
    assert env.cli("list", "--since", "soon").exit_code == 2
    table = env.cli("list", "--since", "36500d", "--surface", "slack")
    assert table.exit_code == 0 and "rec-" in table.output and "slack" in table.output


CHROME = os.environ.get(
    "OBI_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)


def _dump_dom(url: str, profile: Path) -> str:
    """The DOM after virtual time runs out. Headless Chrome may linger after printing it, so read
    until </html> and then kill it (the capture harness's teardown)."""

    command = [CHROME, "--headless=new", f"--user-data-dir={profile}", "--no-first-run",
               "--virtual-time-budget=20000", "--dump-dom", url]  # fmt: skip
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, start_new_session=True)  # fmt: skip
    out: list[str] = []
    timer = threading.Timer(90, proc.kill)
    timer.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            out.append(line)
            if "</html>" in line:
                break
    finally:
        timer.cancel()
        # PermissionError: macOS killpg returns EPERM once the group holds only zombies.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    return "".join(out)


def test_two_approved_recordings_ship_and_autoplay(env: Env, tmp_path: Path) -> None:
    clean = [r for r in _rows(env) if r["status"] == "new"][:2]
    for row in clean:
        assert env.cli("approve", row["id"]).exit_code == 0
    bundle = tmp_path / "bundle"
    result = run_cli("demo", "export", "--config", str(env.config), "--dataset", str(env.dataset),
                     "--out", str(bundle), "--json")  # fmt: skip
    assert result.exit_code == 0, result.output
    index = json.loads((bundle / "data" / "chat" / "index.json").read_text())
    assert sorted(r["id"] for r in index["recordings"]) == sorted(r["id"] for r in clean)
    assert sorted(p.stem for p in (bundle / "data" / "chat").glob("rec-*.json")) == sorted(
        r["id"] for r in clean
    )
    if not Path(CHROME).exists():
        pytest.skip("Chrome not installed")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(bundle))
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dom = _dump_dom(f"http://127.0.0.1:{server.server_address[1]}/?w=talk", tmp_path / "p")
    finally:
        server.shutdown()
        server.server_close()
    first = json.loads((bundle / "data" / "chat" / f"{index['recordings'][0]['id']}.json")
                       .read_text())  # fmt: skip
    assert "recorded preview · 1 of 2" in dom
    assert html.escape(first["turns"][0]["question"], quote=False) in dom
    assert "talk-entry obi" in dom
