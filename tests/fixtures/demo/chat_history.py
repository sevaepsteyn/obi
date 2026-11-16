"""Synthetic chat history for the static-demo tests (feature 136, T002).

Appends fictional conversations to the 134 synthetic ``audit.db`` in the REAL audit-row shapes
obi writes (``chat/session.py`` human_message, ``core/toolset.py`` tool_call, ``core/llm.py``
llm_call). Every value is fictional; one conversation plants a "real" identifier from the 134
synthetic network inside a tool RESULT, so curation must refuse it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from obi.core import auditstore
from tests.fixtures.demo.build_state import BUSY_START, SLACK_IDS

OTHER_USER = "U0OTHER001"


@dataclass(frozen=True)
class Conversation:
    session: str
    source: str
    peer: str | None
    question: str
    answer: str
    tool: str
    arguments: dict[str, object]
    result: str


CONVERSATIONS: tuple[Conversation, ...] = (
    Conversation("chat-0a1b2c3d4e5f", "chat", None,
                 "is anything down on the backbone right now?",
                 "All backbone links are up. **southgate-gw** shows 3% loss on its 5 GHz hop.",
                 "uisp_links", {"state": "all"},
                 "2 links: northfield-bh-core active, southgate-bh-core degraded"),
    Conversation("chat-web-ar-11111111-2222-3333-4444-555555555555", "voice",
                 "voice-web:ar-111111", "what's the cpu on core-gw-1?",
                 "core-gw-1 is at 12% CPU, 41% memory.",
                 "routeros_status", {"device": "core-gw-1"},
                 "core-gw-1: cpu 12%, mem 41%, uptime 9d"),
    Conversation("slack-C0FAKECHAN-1756000000.000100", "slack", SLACK_IDS[0],
                 "any config changes on eastmoor-gw today?",
                 "One change on eastmoor-gw at 10:00 — an interface comment edit.",
                 "config_history", {"device": "eastmoor-gw"},
                 "1 commit: eastmoor-gw: config change"),
    Conversation("slack-C0FAKECHAN-1756000000.000200", "slack", OTHER_USER,
                 "can you check my camp's uplink?",
                 "Checked: the uplink is fine.",
                 "routeros_ping", {"device": "core-gw-1", "target": "8.8.4.1"},
                 "5/5 replies"),
    Conversation("radio-call-42", "radio", "radio:quill", "obi, link status?",
                 "All links up.", "uisp_links", {}, "all up"),
    Conversation("mcp-abcdefabcdef", "mcp", None, "", "", "inventory_list", {}, "6 devices"),
    Conversation("chat-0f0f0f0f0f0f", "chat", None,
                 "where does the 10.99 range route?",
                 "Via core-gw-1.",
                 "routeros_query", {"device": "core-gw-1", "path": "/ip/route"},
                 # Planted: a raw "real" address and hostname the rewriter must map — and, in
                 # the seeded-leak variant the tests build, a token it cannot (refusal path).
                 "dst 10.99.0.0/16 via 10.99.0.1 on northfield-gw"),
)  # fmt: skip


def add_conversations(
    audit_db: Path, conversations: tuple[Conversation, ...] = CONVERSATIONS
) -> None:
    """Append ``conversations`` to ``audit_db`` (the 134 synthetic store) in real shapes."""

    conn = auditstore.connect(audit_db)
    try:
        for n, conv in enumerate(conversations):
            t0 = BUSY_START + timedelta(hours=3 * n + 1)
            ts = int(t0.timestamp() * 1_000_000)
            rows: list[tuple[object, ...]] = []
            if conv.question:
                rows.append(
                    (
                        ts,
                        t0.isoformat(),
                        "human_message",
                        conv.source,
                        conv.session,
                        None,
                        conv.question[:80],
                        json.dumps(
                            {"direction": "in", "content": conv.question, "peer": conv.peer}
                        ),
                        None,
                        "ok",
                        None,
                    )
                )
            rows.append(
                (
                    ts + 10,
                    t0.isoformat(),
                    "llm_call",
                    conv.source,
                    conv.session,
                    None,
                    "claude-sonnet-5",
                    json.dumps(
                        {
                            "model": "claude-sonnet-5",
                            "system": "SYSTEM PROMPT",
                            "messages": [{"role": "user", "content": conv.question}],
                            "tool_names": [conv.tool],
                            "response": "…",
                            "stop_reason": "tool_use",
                            "input_tokens": 900,
                            "output_tokens": 40,
                        }
                    ),
                    850.0,
                    "ok",
                    None,
                )
            )
            rows.append(
                (
                    ts + 20,
                    t0.isoformat(),
                    "tool_call",
                    conv.source,
                    conv.session,
                    None,
                    f"{conv.tool}",
                    json.dumps(
                        {
                            "tool": conv.tool,
                            "arguments": conv.arguments,
                            "result": conv.result,
                            "is_error": False,
                        }
                    ),
                    120.0,
                    "ok",
                    None,
                )
            )
            if conv.answer:
                rows.append(
                    (
                        ts + 30,
                        t0.isoformat(),
                        "human_message",
                        conv.source,
                        conv.session,
                        None,
                        conv.answer[:80],
                        json.dumps(
                            {
                                "direction": "out",
                                "content": conv.answer,
                                "peer": conv.peer,
                                "tools_used": [conv.tool],
                                "input_tokens": 1800,
                                "output_tokens": 120,
                                "model_calls": 2,
                            }
                        ),
                        None,
                        "ok",
                        None,
                    )
                )
            conn.executemany(
                "INSERT INTO events (ts_us, ts_iso, kind, source, session_id, target, summary,"
                " payload, duration_ms, outcome, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        conn.commit()
    finally:
        conn.close()
