"""The Slack-DM member paging tool (feature 130; constitution 3.0.0).

``build_page_send_tool`` constructs the ONE model-facing paging capability — a
:class:`ToolDefinition` that exists in NO toolset builder. The Slack handler injects it
per request, only into a direct-message turn whose sender passed the paging gate
(contracts/sender-gating.md); every other surface is excluded by construction and pinned
by ``tests/unit/test_paging_not_a_tool.py``.

The tool is a VERBATIM RELAY around the feature-102 core: recipients resolve through the
strict roster and must land inside the operator-declared ``[paging.slack] recipients``
allowlist (operator decision 2026-09-03: the WAN team, the operator, and the on-call rotations —
a GROUP is pageable only by being allowlisted, so the on-call rotations work while mass
paging stays impossible), the delivered text is the requester's message behind a fixed
attribution prefix naming them (the model cannot supply or alter the attribution — it is
closed over at build time, from the handler's sender identity, never from tool
arguments), and the transport is the one :class:`PagingClient` path. A per-sender
sliding-hour cap (``[paging.slack] max_pages_per_hour``) bounds a runaway conversation;
suppression is audited, never queued. Handlers never raise — every outcome is a returned
string, error rows starting with ``error`` per the toolset convention.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obi.core.toolset import MEDIUM_RISK, ToolContext, ToolDefinition
from obi.paging.client import PagingClient, record_page
from obi.paging.roster import ResolutionError, RosterEntry, load_roster, resolve, split_tokens

if TYPE_CHECKING:
    from obi.core.config import PagingSettings

#: Cap state beside the roster snapshot (both operator-local paging state): sender id →
#: send timestamps (unix µs), pruned to the trailing hour on every check.
CAP_FILE_NAME = "slack-cap.json"
_HOUR_US = 3_600 * 1_000_000

PAGE_TOOL_NAME = "page_send"

# Field lesson 2026-09-03 (call 09:19Z, audit 2471860-2): describing the allowlist's
# CONTENT ("the WAN team and the on-call rotations") made the model pre-judge a recipient
# as not-listed and refuse WITHOUT calling — an un-audited, wrong denial. The description
# must say the list is invisible to the model and that calling is the only way to find out.
_DESCRIPTION = (
    "Send a page ON THE REQUESTER'S EXPLICIT ASK ONLY. Pass the recipient "
    "name(s) exactly as the requester gave them and the message exactly as the requester "
    "wrote it — never invent, expand, or rephrase either. If the requester did not clearly "
    "ask to page someone, do not call this tool. The operator maintains a pageable-recipient "
    "allowlist that you CANNOT see: never guess or claim who is or isn't pageable — always "
    "call this tool with the requested recipient and relay its answer. It refuses with "
    "exact, relayable guidance when a recipient is not allowed, ambiguous, or unknown."
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": (
                "Comma-separated recipient names or numeric ids, verbatim from the "
                "requester. People only."
            ),
        },
        "message": {
            "type": "string",
            "description": "The message to deliver, verbatim from the requester.",
        },
    },
    "required": ["to", "message"],
}


def _cap_path(settings: PagingSettings) -> Path:
    return settings.roster_path.parent / CAP_FILE_NAME


def _allowed_recipients(
    tokens: list[str], roster: list[RosterEntry] | None
) -> set[tuple[str, str]]:
    """Resolve the ``[paging.slack] recipients`` allowlist to ``(type, id)`` pairs.

    Each token resolves through the SAME strict roster machinery a request uses; a token
    that fails to resolve grants nothing (fail closed — a typo in the allowlist narrows
    it, never widens it). Numeric tokens are person ids (the roster convention); a group
    is allowlisted by NAME.
    """

    allowed: set[tuple[str, str]] = set()
    for token in tokens:
        with contextlib.suppress(ResolutionError):
            resolved = resolve([token], roster)
            allowed.update(("person", pid) for pid in resolved.people)
            allowed.update(("group", gid) for gid in resolved.groups)
    return allowed


def _load_cap(path: Path) -> dict[str, list[int]]:
    with contextlib.suppress(Exception):  # missing/corrupt state reads as empty, never fatal
        raw = json.loads(path.read_text())
        if isinstance(raw, dict):
            return {
                str(sender): [int(t) for t in stamps]
                for sender, stamps in raw.items()
                if isinstance(stamps, list)
            }
    return {}


def _write_cap(path: Path, state: dict[str, list[int]]) -> None:
    """Atomic rewrite (the alerts.py cap-file pattern); a write failure never blocks a page."""

    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)


def build_page_send_tool(
    settings: PagingSettings,
    *,
    sender_id: str,
    sender_display: str,
    client: PagingClient | None = None,
    now_us: Callable[[], int] | None = None,
) -> ToolDefinition:
    """The per-request ``page_send`` tool, closed over the SENDER's identity.

    ``settings.slack`` must be present (the handler's gate guarantees it). ``client`` and
    ``now_us`` are test seams; production uses the real transport and clock.
    """

    slack = settings.slack
    if slack is None:  # pragma: no cover - the handler gates on the section
        raise ValueError("[paging.slack] is not configured")
    max_per_hour = slack.max_pages_per_hour
    paging_client = client or PagingClient(settings)
    clock_us = now_us or (lambda: time.time_ns() // 1_000)
    audit_path = f"slack:{sender_id}"
    prefix = f"from {sender_display} via obi: "

    def _refuse(people: list[str], body: str, reason: str, *, outcome: str = "refused") -> str:
        record_page(
            path=audit_path, people=people, groups=[], body=body, outcome=outcome, error=reason
        )
        return f"error: {reason}"

    def handler(ctx: ToolContext, args: dict[str, Any]) -> str:  # noqa: ARG001 - toolset shape
        to = str(args.get("to") or "").strip()
        message = str(args.get("message") or "").strip()
        if not to:
            return _refuse([], message, "a page needs at least one recipient")
        if not message:
            return _refuse([], "", "a page needs message text")

        roster = load_roster(settings.roster_path)
        try:
            resolved = resolve(split_tokens(to), roster)
        except ResolutionError as exc:
            reason = str(exc)
            if exc.kind == "ambiguous" and exc.candidates:
                names = ", ".join(f"{c.name} ({c.id})" for c in exc.candidates)
                reason = f"{exc} — candidates: {names}"
            elif exc.kind == "no-roster":
                reason = (
                    "no paging roster on this machine — the operator must run 'obi page refresh'"
                )
            return _refuse([], message, reason)
        if not resolved.people and not resolved.groups:
            return _refuse([], message, "a page needs at least one recipient")
        allowed = _allowed_recipients(slack.recipients, roster)
        blocked = [("person", pid) for pid in resolved.people if ("person", pid) not in allowed]
        blocked += [("group", gid) for gid in resolved.groups if ("group", gid) not in allowed]
        if blocked:
            by_id_all = {(e.type, e.id): e.name for e in roster or []}
            names = ", ".join(by_id_all.get(key, key[1]) for key in blocked)
            return _refuse(
                resolved.people,
                message,
                f"not on the pageable list: {names} — pages from Slack may only reach "
                "the recipients the operator has allowlisted (the WAN team and the "
                "on-call rotations); ask the operator to widen [paging.slack] recipients",
            )

        body = prefix + message
        if len(body) > settings.message_max_chars:
            over = len(body) - settings.message_max_chars
            return _refuse(
                resolved.people,
                body,
                f"message too long — {over} chars over the {settings.message_max_chars}-char "
                f"page limit after the {prefix.rstrip()!r} prefix",
            )

        cap_file = _cap_path(settings)
        tick = clock_us()
        state = _load_cap(cap_file)
        recent = [t for t in state.get(sender_id, []) if t > tick - _HOUR_US]
        state[sender_id] = recent
        if len(recent) >= max_per_hour:
            _write_cap(cap_file, state)
            return _refuse(
                resolved.people,
                body,
                f"page cap reached ({max_per_hour}/hour per sender) — try later or "
                "contact the operator",
                outcome="suppressed",
            )

        result = paging_client.send_page(
            people=resolved.people, groups=resolved.groups, message=body
        )
        record_page(
            path=audit_path,
            people=resolved.people,
            groups=resolved.groups,
            body=body,
            outcome="ok" if result.ok else "error",
            error=result.error,
        )
        if not result.ok:
            return f"error page failed: {result.error or 'unknown error'}"
        recent.append(tick)
        state[sender_id] = recent
        _write_cap(cap_file, state)
        by_name = {(e.type, e.id): e.name for e in roster or []}
        recipients = [("person", pid) for pid in resolved.people]
        recipients += [("group", gid) for gid in resolved.groups]
        who = ", ".join(
            f"{by_name[key]} ({key[1]})" if key in by_name else key[1] for key in recipients
        )
        return f"page sent to {who}: {body}"

    return ToolDefinition(
        name=PAGE_TOOL_NAME,
        description=_DESCRIPTION,
        input_schema=_INPUT_SCHEMA,
        handler=handler,
        risk_class=MEDIUM_RISK,
        mutating=True,
    )
