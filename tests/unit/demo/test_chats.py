"""Chat curation: candidates, rewrite, gate, approvals (feature 136, T025/T027/T029/T031)."""

from __future__ import annotations

import copy
import json
import stat
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from obi.core.demo import LeakGateError, ProfileError
from obi.core.demo import chats as chats_mod
from obi.core.demo.chats import (
    Curation,
    approvals_path,
    approve,
    candidates,
    check,
    listings,
    load_approvals,
    open_curation,
    revoke,
)
from tests.fixtures.demo.chat_history import OTHER_USER, add_conversations
from tests.fixtures.demo.harness import build_dataset, offline_terrain

FOREVER = timedelta(days=36_500)


@dataclass
class Env:
    root: Path
    state: Path
    dataset: Path
    config: Path
    raw: dict  # type: ignore[type-arg]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    dataset, config, _truth = build_dataset(tmp_path)
    state = tmp_path / "xdg" / "obi"
    add_conversations(state / "audit.db")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    offline_terrain(monkeypatch)
    yield Env(tmp_path, state, dataset, config, tomllib.loads(config.read_text()))


@pytest.fixture
def cur(env: Env) -> Curation:
    return open_curation(env.raw, env.config, env.dataset)


# --- T025 candidates -------------------------------------------------------------------------


def test_only_the_operators_own_conversations(env: Env) -> None:
    before = (env.state / "audit.db").stat().st_mtime_ns
    found = candidates(env.state, env.raw, since=FOREVER)
    sessions = {c.session_id for c in found}
    assert sessions == {
        "chat-0a1b2c3d4e5f",
        "chat-web-ar-11111111-2222-3333-4444-555555555555",
        "slack-C0FAKECHAN-1756000000.000100",
        "chat-0f0f0f0f0f0f",
    }
    assert all(c.peer != OTHER_USER for c in found)  # another person's Slack chat
    assert (env.state / "audit.db").stat().st_mtime_ns == before  # read-only
    turn = next(c for c in found if c.session_id == "chat-0a1b2c3d4e5f").turns[0]
    assert turn.answer and turn.steps[0].tool == "uisp_links" and turn.model


# --- T027 rewrite ----------------------------------------------------------------------------


def test_recording_is_rewritten_clipped_and_promptless(cur: Curation, env: Env) -> None:
    cand = next(c for c in candidates(env.state, env.raw, since=FOREVER)
                if c.session_id == "chat-0f0f0f0f0f0f")  # fmt: skip
    cur.clip_bytes = 20
    rec = cur.recording(cand)
    text = json.dumps(rec)
    for real in ("10.99.0.1", "northfield-gw", "SYSTEM PROMPT", "chat-0f0f0f0f0f0f"):
        assert real not in text
    step = rec["turns"][0]["steps"][0]
    assert step["result"].endswith("…[trimmed]") and rec["turns"][0]["report"]["tools"]
    assert rec["id"].startswith("rec-") and len(rec["id"]) == 12
    assert set(rec["turns"][0]["report"]) == {"model", "tools", "usage", "cost_usd"}


# --- T029 gate + entities --------------------------------------------------------------------


@pytest.mark.parametrize(
    "token", ["10.99.0.1", "northfield-gw", "HZ90A1B2C3D", "fakecommunity", "U0FAKE0001",
              "45.0123,-100.0456", "Quillon"],
)  # fmt: skip
def test_a_leak_injected_after_rewrite_is_refused(cur: Curation, env: Env, token: str) -> None:
    cand = candidates(env.state, env.raw, since=FOREVER)[0]
    rec = copy.deepcopy(cur.recording(cand))
    rec["turns"][0]["answer"] += f" {token}"
    verdict = check(rec, cur.gate, cur.known)
    assert verdict.status == "refused" and verdict.reasons
    assert all(token not in r for r in verdict.reasons)


def test_unknown_device_needs_review(cur: Curation, env: Env) -> None:
    rec = cur.recording(candidates(env.state, env.raw, since=FOREVER)[0])
    rec["entities"] = [*rec["entities"], "nowhere-gw"]
    assert check(rec, cur.gate, cur.known).status == "needs_review"


# --- T031 approvals --------------------------------------------------------------------------


def test_approve_pin_stale_revoke(cur: Curation, monkeypatch: pytest.MonkeyPatch) -> None:
    items = listings(cur, since=FOREVER)
    clean = next(x for x in items if x.check.status == "ok")
    approve(clean)
    path = approvals_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert next(x for x in listings(cur, since=FOREVER) if x.rec_id == clean.rec_id).status == (
        "approved"
    )
    real = chats_mod.to_recording

    def drifted(*a, **kw):  # type: ignore[no-untyped-def]
        rec = real(*a, **kw)
        rec["turns"][0]["answer"] += " (changed)"
        return rec

    monkeypatch.setattr(chats_mod, "to_recording", drifted)
    assert next(x for x in listings(cur, since=FOREVER) if x.rec_id == clean.rec_id).status == (
        "stale"
    )
    monkeypatch.setattr(chats_mod, "to_recording", real)
    assert revoke(clean) and clean.candidate.session_id not in load_approvals()


def test_refused_cannot_be_approved_and_bad_perms_refused(cur: Curation) -> None:
    item = listings(cur, since=FOREVER)[0]
    item.check = chats_mod.Check("refused", ["ip/ip"])
    with pytest.raises(LeakGateError):
        approve(item)
    approvals_path().parent.mkdir(parents=True, exist_ok=True)
    approvals_path().write_text("{}")
    approvals_path().chmod(0o644)
    with pytest.raises(ProfileError):
        load_approvals()


def test_tool_names_are_code_and_config_words_are_not_devices(cur: Curation, env: Env) -> None:
    rec = cur.recording(candidates(env.state, env.raw, since=FOREVER)[0])
    rec["turns"][0]["steps"][0]["tool"] = "office_list"  # a dictionary-word place in a tool NAME
    rec["entities"] = [*rec["entities"], "arp-timeout", "ether1-noc-ptp-x-60", "172.16.0.9"]
    verdict = check(rec, cur.gate, cur.known)
    assert verdict.status == "ok", verdict.reasons
    rec["turns"][0]["answer"] += " see orchard-gw"  # …but in data it is still refused
    assert check(rec, cur.gate, cur.known).status == "refused"


def test_needs_review_approves_only_with_accept_review(cur: Curation) -> None:
    item = listings(cur, since=FOREVER)[0]
    item.check = chats_mod.Check("needs_review", ["1 device(s) not on the demo boards"])
    with pytest.raises(LeakGateError):
        approve(item)
    approve(item, accept_review=True)
    assert load_approvals()[item.candidate.session_id]["accepted_review"] is True
    item.check = chats_mod.Check("refused", ["ip/ip"])
    with pytest.raises(LeakGateError):
        approve(item, accept_review=True)


def test_site_words_are_rewritten_in_chat_prose(cur: Curation) -> None:
    # "orchard" is a dictionary-word site in the synthetic network (orchard-ap).
    assert "orchard" in cur.rewriter.prose_sites
    out = cur.rewriter.text("the backhaul toward orchard was slow")
    assert "orchard" not in out.casefold()
