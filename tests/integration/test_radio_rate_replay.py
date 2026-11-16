"""Integration (feature 093, US1): the 2026-08-24 call #4 incident shape, end to end.

SC-001: a multi-request call whose courtesy standbys share the minute window with its
answers must still air EVERY answer. This runs the full pipeline — scripted channel
audio → capture → trigger → call machine → TxExecutor → store — with a LIVE-stage
executor over fakes. Deliberately NOT the shadow stage: the G2 stage gate returns
``shadow_logged`` before the rate check runs and the windows only fill on real
key-ups, so shadow walks can never exercise the G9 backstop (analysis I1).

The three standbys are seeded through the same executor/gate before the scripted call
(the harness's inline spawn seam composes atomically, so a mid-compose standby cannot
interleave naturally); what matters to G9 is that they sit in the same minute window
as the call's ack and answers — exactly the live incident's rate math (radio.db
outbound id 20: suppressed at 6/6, three of the six standbys).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.integration.test_radio_exchange import _harness, _script, _tr


class AlwaysClearMonitor:
    """The channel-clear gate is not under test here — G5/G6 have their own tests."""

    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        return True


# Hail, then three questions, each with a gap wide enough for obi's own transmission;
# a 1000-block (50 s) tail lets the call expire (timeout > call_timeout_s=45).
_THREE_QUESTION_CALL = _script(
    (10, False),
    (20, True),  # "obi obi this is nono"
    (60, False),
    (20, True),  # q1 (awaiting_request: any intelligible request composes)
    (60, False),
    (20, True),  # q2 (post-answer follow-ups need the request shape — "?")
    (60, False),
    (20, True),  # q3
    (1000, False),
)
_THREE_QUESTION_TRANSCRIPTS = [
    _tr("obi obi this is nono"),
    _tr("status of the noc gateway"),
    _tr("and the latency to google?"),
    _tr("what about the chestnut link?"),
]


def test_incident_shape_all_three_answers_air(tmp_path: Path) -> None:
    answers = iter(["noc-gw is up, over", "about nine milliseconds, over", "chestnut is up, over"])

    def composer(request: str, **_kw: Any) -> str:
        return next(answers)

    h = _harness(
        tmp_path,
        _THREE_QUESTION_CALL,
        _THREE_QUESTION_TRANSCRIPTS,
        composer=composer,
        monitor_override=AlwaysClearMonitor(),
        pre_tx=[("standby", "stand by")] * 3,
    )
    # Every answer aired. Before 093, the three standbys + ack + two answers filled the
    # default 6/minute window and the THIRD answer resolved suppressed_rate — the caller
    # heard "stand by" and then nothing.
    assert h.outbound() == [
        ("ack", "transmitted"),
        ("answer", "transmitted"),
        ("answer", "transmitted"),
        ("answer", "transmitted"),
    ]
    # RF actually keyed for all seven transmissions (3 seeded standbys + the call's 4).
    assert h.ptt.key_calls == 7 and not h.ptt.keyed
    # The exchange ended normally: the caller drifted off and the call expired silently.
    assert h.calls() == [("timeout", "timeout")]
