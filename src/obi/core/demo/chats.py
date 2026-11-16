"""Curated chat previews for the static demo (feature 136, research R6/R7).

The demo's chat window replays REAL past obi conversations, but only:

* the operator's OWN conversations: terminal chat, voice (CLI and web/XR), and Slack where the
  asker is in ``[slack] allowlist``. Radio, MCP and other people are never offered (FR-009/010);
* each one rewritten through 134's private mapping and rewriter, so names, addresses, people and
  times match the fictional dataset. Tool results are clipped, and prompts and system text never
  appear;
* each one passing 134's leak gate on its own (refused ⇒ approval impossible);
* each one approved by the operator, PINNED to the sha256 of the exact rewritten text. If a
  later rewrite differs (the mapping grew), it ships nothing until it is re-approved.

Conversations are reconstructed from the live ``audit.db``, read-only: ``human_message``
in/out with the ``tool_call`` rows between them, plus the model name and usage from
``llm_call``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from obi.core.demo import ProfileError, open_source_db

CONVERSATIONAL_SOURCES = ("chat", "voice", "slack")
RECORDING_FORMAT = 1
_PACE_MS = 900
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_HOSTLIKE = re.compile(r"(?<![\w-])[a-z0-9]+(?:-[a-z0-9]+)+(?![\w-])")


# --- reconstruction --------------------------------------------------------------------------


@dataclass
class Step:
    tool: str
    arguments: Any
    result: str
    error: bool
    after_ms: int


@dataclass
class Turn:
    question: str
    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    t0_us: int = 0  # when the question arrived (tool steps are paced relative to it)


@dataclass
class Candidate:
    session_id: str
    surface: str
    started: datetime
    peer: str | None
    turns: list[Turn] = field(default_factory=list)

    @property
    def tools(self) -> list[str]:
        return [s.tool for t in self.turns for s in t.steps]


def _operator_ids(raw_config: dict[str, Any]) -> set[str]:
    slack = raw_config.get("slack", {}) or {}
    return {str(x) for x in slack.get("allowlist", []) or []}


def _is_operators(source: str, peer: str | None, operators: set[str]) -> bool:
    if source in ("chat", "voice"):
        return True  # local terminal / the operator's own machine or LAN UI
    return source == "slack" and peer is not None and peer in operators


def candidates(
    state_dir: Path, raw_config: dict[str, Any], *, since: timedelta = timedelta(days=90)
) -> list[Candidate]:
    """The operator's own past conversations, oldest first (read-only on the live store)."""

    db = state_dir / "audit.db"
    if not db.exists():
        return []
    operators = _operator_ids(raw_config)
    since_us = int((datetime.now(UTC) - since).timestamp() * 1_000_000)
    conn = open_source_db(db)
    try:
        rows = conn.execute(
            "SELECT ts_us, kind, source, session_id, payload FROM events"
            " WHERE ts_us >= ? AND session_id IS NOT NULL"
            " AND kind IN ('human_message', 'tool_call', 'llm_call')"
            f" AND source IN ({','.join('?' * len(CONVERSATIONAL_SOURCES))})"
            " ORDER BY session_id, ts_us, id",
            (since_us, *CONVERSATIONAL_SOURCES),
        ).fetchall()
    finally:
        conn.close()

    by_session: dict[str, Candidate] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        session = str(row["session_id"])
        cand = by_session.get(session)
        ts = datetime.fromtimestamp(row["ts_us"] / 1e6, tz=UTC)
        if cand is None:
            cand = by_session[session] = Candidate(session, str(row["source"]), ts, None)
        if row["kind"] == "human_message":
            if payload.get("direction") == "in":
                cand.peer = cand.peer or payload.get("peer")
                cand.turns.append(
                    Turn(question=str(payload.get("content", "")), t0_us=int(row["ts_us"]))
                )
            elif payload.get("direction") == "out" and cand.turns:
                turn = cand.turns[-1]
                turn.answer = str(payload.get("content", ""))
                turn.usage = {
                    "input_tokens": int(payload.get("input_tokens") or 0),
                    "output_tokens": int(payload.get("output_tokens") or 0),
                    "model_calls": int(payload.get("model_calls") or 0),
                }
        elif row["kind"] == "tool_call" and cand.turns:
            t0 = cand.turns[-1].t0_us or int(row["ts_us"])
            cand.turns[-1].steps.append(
                Step(
                    tool=str(payload.get("tool", "")),
                    arguments=payload.get("arguments", {}),
                    result=str(payload.get("result", "")),
                    error=bool(payload.get("is_error")),
                    after_ms=max(0, (row["ts_us"] - t0) // 1000),
                )
            )
        elif row["kind"] == "llm_call" and cand.turns and not cand.turns[-1].model:
            cand.turns[-1].model = str(payload.get("model", ""))

    out = []
    for cand in by_session.values():
        cand.turns = [t for t in cand.turns if t.question and t.answer]
        if cand.turns and _is_operators(cand.surface, cand.peer, operators):
            out.append(cand)
    return sorted(out, key=lambda c: c.started)


# --- rewrite → recording ---------------------------------------------------------------------


def rec_id(seed: bytes, session_id: str) -> str:
    return "rec-" + hmac.new(seed, session_id.encode(), hashlib.sha256).hexdigest()[:8]


def _clip(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore") + " …[trimmed]"


def to_recording(cand: Candidate, rewriter: Any, *, seed: bytes, clip_bytes: int) -> dict[str, Any]:
    """The public, rewritten playback form (data-model §Recording) — no prompts, ever."""

    from obi.chat.agent import TurnUsage
    from obi.core.pricing import estimate_cost

    turns = []
    for turn in cand.turns:
        steps = []
        tools = []
        for step in turn.steps:
            arguments = rewriter.json(step.arguments)
            result = rewriter.text(_clip(step.result, clip_bytes))
            steps.append(
                {"tool": step.tool, "arguments": arguments, "result": result,
                 "error": step.error, "after_ms": min(step.after_ms, 1500)}
            )  # fmt: skip
            tools.append(
                {"name": step.tool, "arguments": arguments, "result": result,
                 "truncated": result.endswith("…[trimmed]"), "error": step.error}
            )  # fmt: skip
        usage = TurnUsage(
            input_tokens=turn.usage.get("input_tokens", 0),
            output_tokens=turn.usage.get("output_tokens", 0),
            model_calls=turn.usage.get("model_calls", 0),
        )
        turns.append(
            {
                "question": rewriter.text(turn.question),
                "steps": steps,
                "answer": rewriter.text(turn.answer),
                "report": {
                    "model": turn.model,
                    "tools": tools,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "model_calls": usage.model_calls,
                    },
                    "cost_usd": estimate_cost(turn.model, usage) if turn.model else None,
                },
            }
        )
    first = turns[0]["question"] if turns else ""
    recording = {
        "format_version": RECORDING_FORMAT,
        "id": rec_id(seed, cand.session_id),
        "title": first if len(first) <= 80 else first[:77] + "…",
        "surface": cand.surface,
        "turns": turns,
    }
    # Scan the string values themselves: dumped JSON glues "\n" onto the next word ("ncore-gw-1").
    recording["entities"] = sorted(set().union(*(_entities(s) for s in _strings_of(turns))))
    return recording


def _strings_of(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings_of(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings_of(v)]
    return []


#: RouterOS words that look like device names but are settings ("immediate-gw", "ap-bridge").
_NOT_DEVICES = frozenset({"immediate-gw", "ap-bridge", "station-bridge", "check-gateway"})


def _entities(text: str) -> set[str]:
    found = {m.group(0) for m in _IPV4.finditer(text)}
    found |= {m.group(0) for m in _HOSTLIKE.finditer(text.lower()) if m.group(0)[0].isalpha()}
    return found


def recording_hash(recording: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(recording, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# --- checks ----------------------------------------------------------------------------------


@dataclass
class Check:
    status: str  # ok | refused | needs_review
    reasons: list[str] = field(default_factory=list)


def dataset_entities(dataset: Path) -> set[str]:
    """Every fictional device name and address the demo boards show (the SC-004 universe)."""

    names: set[str] = set()
    state = dataset / "state" / "obi"
    blobs = [state / "inventory.json", *sorted((state / "ar" / "snapshots").glob("*.json")),
             state / "ar" / "links.json"]  # fmt: skip
    for path in blobs:
        if path.exists():
            text = path.read_text(encoding="utf-8").lower()
            names |= {m.group(0) for m in _IPV4.finditer(text)}
            names |= {m.group(0) for m in _HOSTLIKE.finditer(text)}
    repo = state / "config-repo"
    if repo.is_dir():  # devices with config backups are on the CFG board
        names |= {rsc.stem.lower() for rsc in repo.glob("*.rsc")}
    return names


_INTERFACE_PREFIXES = ("ether", "sfp", "vlan", "wlan", "bond", "bridge", "wireguard", "wg", "lte",
                       "peer", "ospf", "qsfp", "combo")  # fmt: skip
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _without_tool_names(recording: dict[str, Any]) -> dict[str, Any]:
    """The recording with obi's own tool NAMES blanked for the scan: they are code vocabulary
    (``inventory_list``), not network data. Arguments and results stay fully gated."""

    copy: dict[str, Any] = json.loads(json.dumps(recording))
    for turn in copy.get("turns", []):
        for step in turn.get("steps", []):
            if _TOOL_NAME.match(str(step.get("tool", ""))):
                step["tool"] = ""
        for tool in (turn.get("report") or {}).get("tools", []):
            if _TOOL_NAME.match(str(tool.get("name", ""))):
                tool["name"] = ""
    return copy


def check(recording: dict[str, Any], gate: Any, known: set[str]) -> Check:
    """The leak gate on this recording alone, then the entity check (research R7)."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        (root / "recording.json").write_text(json.dumps(_without_tool_names(recording)),
                                             encoding="utf-8")  # fmt: skip
        result = gate.scan(root)
    if result.fatal:
        categories = sorted({f"{f.category}/{f.check}" for f in result.fatal})
        return Check("refused", categories)
    # Device names only: an address or a hyphenated config word ("arp-timeout") is not a board
    # entity, and an interface named after its link ("ether1-noc-ptp-…") is not a device.
    roles = {"gw", "ap", "sw", "bh", "ptp"}
    devices = {e for e in recording.get("entities", []) if not _IPV4.fullmatch(e)
               and not e.startswith(_INTERFACE_PREFIXES) and e not in _NOT_DEVICES
               and any(part in roles for part in e.split("-"))}  # fmt: skip
    missing = sorted(devices - known)
    if missing:
        return Check("needs_review", [f"{len(missing)} device(s) not on the demo boards"])
    return Check("ok")


# --- approvals (private) ---------------------------------------------------------------------


def approvals_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "demo" / "chat-approvals.json"


def load_approvals() -> dict[str, dict[str, Any]]:
    path = approvals_path()
    if not path.exists():
        return {}
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ProfileError(f"{path} must be owner-only (chmod 600)")
    data: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def save_approvals(approvals: dict[str, dict[str, Any]]) -> None:
    path = approvals_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(dict(sorted(approvals.items())), handle, indent=1)
    os.replace(tmp, path)


# --- the curation session (CLI + export share it) --------------------------------------------


@dataclass
class Curation:
    rewriter: Any
    gate: Any
    seed: bytes
    known: set[str]
    clip_bytes: int
    raw_config: dict[str, Any]
    state_dir: Path

    def recording(self, cand: Candidate) -> dict[str, Any]:
        return to_recording(cand, self.rewriter, seed=self.seed, clip_bytes=self.clip_bytes)


def open_curation(raw_config: dict[str, Any], config_path: Path, dataset: Path,
                  *, clip_bytes: int = 2048) -> Curation:  # fmt: skip
    """Rewriter + gate from 134's private mapping (names match the dataset) — local only."""

    from obi.core.demo.gate import LeakGate
    from obi.core.demo.profile import load_profile
    from obi.core.demo.rewrite import Rewriter
    from obi.core.demo.snapshot import prepare, public_values_of

    profile = load_profile(raw_config, config_path=config_path,
                           overrides={"output_dir": str(dataset)})  # fmt: skip
    _window, harvest, mapping, shift, relocator = prepare(raw_config, profile)
    rewriter = Rewriter(mapping, harvest, shift, relocator)
    rewriter.prose_sites = site_words(harvest)
    return Curation(
        rewriter=rewriter,
        gate=LeakGate(harvest, exempt_values=public_values_of(dataset)),
        seed=mapping.seed,
        known=dataset_entities(dataset),
        clip_bytes=clip_bytes,
        raw_config=raw_config,
        state_dir=profile.state_dir,
    )


def site_words(harvest: Any) -> frozenset[str]:
    """The leading word of every real device name with a role token (``showers`` from
    ``sitea-gw``, ``siteb`` from ``siteb-internet-gw``): in a conversation about the network
    those words name sites even when they are ordinary English."""

    from obi.core.demo.naming import GENERIC_TOKENS

    roles = {"gw", "sw", "ap"}
    out: set[str] = set()
    for name in harvest.names:
        parts = re.findall(r"[a-z]+", name.casefold())
        if (
            len(parts) >= 2
            and roles & set(parts)
            and parts[0] not in GENERIC_TOKENS
            and parts[0] in harvest.place_words
            and len(parts[0]) >= 3
        ):
            out.add(parts[0])
    return frozenset(out)


@dataclass
class Listing:
    rec_id: str
    candidate: Candidate
    recording: dict[str, Any]
    check: Check
    status: str  # new | refused | needs_review | approved | stale


def listings(cur: Curation, *, since: timedelta = timedelta(days=90)) -> list[Listing]:
    approvals = load_approvals()
    out = []
    for cand in candidates(cur.state_dir, cur.raw_config, since=since):
        recording = cur.recording(cand)
        verdict = check(recording, cur.gate, cur.known)
        approved = approvals.get(cand.session_id)
        reviewed = verdict.status == "needs_review" and bool(
            approved and approved.get("accepted_review")
        )
        if verdict.status != "ok" and not reviewed:
            status = verdict.status
        elif approved is None:
            status = "new"
        elif approved.get("sha256") == recording_hash(recording):
            status = "approved"
        else:
            status = "stale"
        out.append(Listing(recording["id"], cand, recording, verdict, status))
    return out


def find(cur: Curation, rec: str, *, since: timedelta = timedelta(days=365)) -> Listing | None:
    return next((x for x in listings(cur, since=since) if x.rec_id == rec), None)


def approve(listing: Listing, *, accept_review: bool = False) -> None:
    """Pin the recording. ``accept_review``: the operator read a ``needs_review`` one (it names a
    device the boards don't show) and accepts it. A ``refused`` one can never be approved."""

    from obi.core.demo import LeakGateError

    allowed = {"ok", "needs_review"} if accept_review else {"ok"}
    if listing.check.status not in allowed:
        raise LeakGateError(
            f"{listing.rec_id} cannot be approved: {listing.check.status} "
            f"({', '.join(listing.check.reasons)})"
        )
    approvals = load_approvals()
    approvals[listing.candidate.session_id] = {
        "rec_id": listing.rec_id,
        "sha256": recording_hash(listing.recording),
        "accepted_review": listing.check.status == "needs_review",
        "approved_at": datetime.now(UTC).isoformat(),
    }
    save_approvals(approvals)


def revoke(listing: Listing) -> bool:
    approvals = load_approvals()
    removed = approvals.pop(listing.candidate.session_id, None) is not None
    if removed:
        save_approvals(approvals)
    return removed


def bundle_recordings(
    raw_config: dict[str, Any], config_path: Path, view: Any, staging: Path
) -> tuple[int, list[str]]:
    """Write every APPROVED, hash-matching, clean recording into the bundle; report the rest."""

    approvals = load_approvals()
    if not approvals:
        return 0, []
    cur = open_curation(raw_config, config_path, view.dataset)
    shipped: list[dict[str, Any]] = []
    left_out: list[str] = []
    for item in listings(cur, since=timedelta(days=3650)):
        if item.candidate.session_id not in approvals:
            continue
        if item.status == "approved":
            shipped.append(item.recording)
        else:
            left_out.append(f"{item.rec_id}: {item.status}")
    chat = staging / "data" / "chat"
    chat.mkdir(parents=True, exist_ok=True)
    for recording in shipped:
        (chat / f"{recording['id']}.json").write_text(
            json.dumps(recording, ensure_ascii=False), encoding="utf-8"
        )
    index = [{"id": r["id"], "title": r["title"], "surface": r["surface"],
              "turns": len(r["turns"])} for r in shipped]  # fmt: skip
    (chat / "index.json").write_text(json.dumps({"recordings": index}), encoding="utf-8")
    return len(shipped), left_out
