"""Unit (feature 088, US2): the call state machine — every transition in data-model.md.

Pure logic, injectable time (every event takes ``now_us``); effects come back as data and
the fake runner in these tests just collects them. No I/O anywhere (FR-036).
"""

from __future__ import annotations

import re

import pytest

from obi.radio.calls import (
    CallMachine,
    CloseEffect,
    ComposeEffect,
    OpenCallEffect,
    TransmitEffect,
    Turn,
    _with_out_sign_off,
)

S = 1_000_000  # µs per second


def _machine(**overrides: object) -> CallMachine:
    defaults: dict[str, object] = {
        "ack_text": "go for obi",
        "say_again_text": "ten nine, say again",
        "unavailable_text": "unable to comply, obi out",
        "call_timeout_s": 45,
        "compose_deadline_s": 20,
        "operator_handles": ["nono"],
    }
    defaults.update(overrides)
    return CallMachine(**defaults)  # type: ignore[arg-type]


def _call(machine: CallMachine, now: int = 0, caller: str | None = "nono") -> list[object]:
    outcome = machine.on_transmission(
        now, fired=True, caller=caller, text=f"obi obi this is {caller}", intelligible=True
    )
    return list(outcome.effects)


def test_idle_directed_call_opens_and_acks() -> None:
    machine = _machine()
    effects = _call(machine)
    assert effects == [
        OpenCallEffect(caller="nono", tier="operator"),
        TransmitEffect(kind="ack", text="go for obi"),
    ]
    assert machine.state == "acking"


def test_unknown_caller_gets_safe_tier() -> None:
    machine = _machine()
    effects = _call(machine, caller="stranger")
    assert effects[0] == OpenCallEffect(caller="stranger", tier="safe")


def test_operator_handle_match_is_case_insensitive() -> None:
    machine = _machine(operator_handles=["NoNo"])
    effects = _call(machine)
    assert effects[0] == OpenCallEffect(caller="nono", tier="operator")


def test_named_second_caller_supersedes_from_any_state() -> None:
    # Feature 109 (operator rule): the window belongs to the caller UNLESS someone makes
    # another call — a full named hail supersedes even while the first ack is in flight.
    machine = _machine()
    _call(machine)
    outcome = machine.on_transmission(
        1 * S, fired=True, caller="yoyo", text="obi obi this is yoyo",
        intelligible=True, caller_known=True,
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert OpenCallEffect(caller="yoyo", tier="safe") in effects
    assert any(isinstance(e, TransmitEffect) and e.kind == "ack" for e in effects)
    assert machine.state == "acking"


def test_anonymous_refire_during_acking_stays_busy() -> None:
    # An anonymous tail-less fire while the ack is in flight is NOT another call — it is
    # most likely our caller's audio; the ack already going out covers them.
    machine = _machine()
    _call(machine)
    outcome = machine.on_transmission(
        1 * S, fired=True, caller=None, text="obi obi", intelligible=True
    )
    assert list(outcome.effects) == []
    assert outcome.trigger_reason_override == "busy_with_call"


def test_full_happy_path_call() -> None:
    machine = _machine()
    _call(machine, now=0)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    assert machine.state == "awaiting_request"
    outcome = machine.on_transmission(
        5 * S, fired=False, caller=None, text="is the mountain link up", intelligible=True
    )
    assert list(outcome.effects) == [
        ComposeEffect(request="is the mountain link up", caller="nono", tier="operator")
    ]
    assert machine.state == "composing"
    effects = machine.on_compose_result(9 * S, "mountain link is up, over")
    assert effects == [TransmitEffect(kind="answer", text="mountain link is up, over")]
    machine.on_tx_result(12 * S, kind="answer", disposition="transmitted")
    assert machine.state == "open"


def _answered(machine: CallMachine) -> None:
    """Drive a machine through hail → ack → request → answer, into state 'open'."""

    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="first", intelligible=True)
    machine.on_compose_result(8 * S, "answer one, over")
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    assert machine.state == "open"


def test_post_answer_intelligible_traffic_composes_in_window() -> None:
    # Feature 109 (operator decision 2026-08-28): the window belongs to the caller —
    # every intelligible in-window transmission composes unless it is clearing/sign-off,
    # a fresh hail, or garble. This reverses the 2026-08-20 14:09 silent-close rule
    # IN-WINDOW only (12+ dropped follow-ups in three days); the idle channel is
    # unchanged. The per-call answer budget bounds the accepted chatter risk.
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S, fired=False, caller=None, text="and the chestnut link", intelligible=True
    )
    assert list(outcome.effects) == [
        ComposeEffect(request="and the chestnut link", caller="nono", tier="operator")
    ]
    assert outcome.trigger_reason_override == "in_window_composed"
    assert machine.state == "composing"


def test_post_answer_garble_draws_a_say_again_then_the_repeat_composes() -> None:
    # Field 2026-08-20 15:27: a follow-up question whisper-mangled into a Cyrillic blob
    # got silence. Post-answer garble is most likely the caller — ten-nine it (once)
    # and re-arm the request expectation; the repeat composes even without a "?".
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(15 * S, fired=False, caller=None, text="", intelligible=False)
    assert list(outcome.effects) == [
        TransmitEffect(kind="say_again", text="ten nine, say again")
    ]
    assert machine.state == "awaiting_request"
    outcome = machine.on_transmission(
        20 * S,
        fired=False,
        caller=None,
        text="status of the magnolia to manzanita link",
        intelligible=True,
    )
    assert list(outcome.effects) == [
        ComposeEffect(
            request="status of the magnolia to manzanita link", caller="nono", tier="operator"
        )
    ]


def test_post_answer_garble_with_say_again_spent_closes() -> None:
    # 093: the close now happens ON the air — the second garble draws the canned
    # "unable to copy, out" and the CloseEffect arrives with its TX result.
    machine = _machine()
    _answered(machine)
    machine.on_transmission(15 * S, fired=False, caller=None, text="", intelligible=False)
    machine.on_tx_result(17 * S, kind="say_again", disposition="transmitted")
    outcome = machine.on_transmission(20 * S, fired=False, caller=None, text="", intelligible=False)
    assert list(outcome.effects) == [
        TransmitEffect(kind="garble_close", text="unable to copy, out")
    ]
    effects = machine.on_tx_result(22 * S, kind="garble_close", disposition="transmitted")
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "garble" and close.note


def test_soliciting_answer_keeps_the_reply_expectation() -> None:
    # Field 2026-08-20 14:29: obi answered "…say again on that third name, over" and
    # the machine closed 'complete' on the clarification it had itself asked for. An
    # answer that solicits a reply (a question / "say again") returns the machine to
    # awaiting_request: the next transmission composes as the caller's reply.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(
        5 * S,
        fired=False,
        caller=None,
        text="status of the manzanita mountain luke",
        intelligible=True,
    )
    machine.on_compose_result(8 * S, "both routers up — say again on that third name, over")
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    assert machine.state == "awaiting_request"
    outcome = machine.on_transmission(
        14 * S, fired=False, caller=None,
        text="i meant the seventy gigahertz siklu link", intelligible=True,
    )
    assert list(outcome.effects) == [
        ComposeEffect(
            request="i meant the seventy gigahertz siklu link", caller="nono", tier="operator"
        )
    ]
    # A DECLARATIVE second answer then releases the expectation: back to 'open'.
    machine.on_compose_result(16 * S, "siklu link is up, over")
    machine.on_tx_result(18 * S, kind="answer", disposition="transmitted")
    assert machine.state == "open"


def test_post_answer_question_composes_as_a_follow_up() -> None:
    # Field 2026-08-20 15:13: "Thank you, what about the magnolia to manzanita link?" right after
    # the answer went unanswered. A post-answer QUESTION is addressed to us — compose.
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S, fired=False, caller=None,
        text="thank you, what about the magnolia to manzanita link?", intelligible=True,
    )
    assert list(outcome.effects) == [
        ComposeEffect(
            request="thank you, what about the magnolia to manzanita link?",
            caller="nono",
            tier="operator",
        )
    ]


def test_post_answer_fresh_hail_supersedes_into_a_new_call() -> None:
    # Field 2026-08-20 15:10: "Obi, Obi, User-41" right after another caller's answer was
    # busy-rejected. Post-answer, any fresh hail is new-call intent: the finished call
    # closes superseded and the new caller gets an ack (or a direct compose with tail).
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S, fired=True, caller=None, text="obi obi user-41", intelligible=True
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert any(isinstance(e, OpenCallEffect) for e in effects)
    assert any(isinstance(e, TransmitEffect) and e.kind == "ack" for e in effects)
    assert machine.state == "acking"
    # Variant: a different caller's one-breath hail composes directly, no ack.
    machine2 = _machine()
    _answered(machine2)
    outcome = machine2.on_transmission(
        15 * S, fired=True, caller="yoyo", text="obi obi this is yoyo radio check",
        intelligible=True, caller_known=True, request_tail="radio check",
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert ComposeEffect(request="radio check", caller="yoyo", tier="safe") in effects


def test_post_answer_follow_up_needs_a_rehail() -> None:
    # The addressed follow-up still works: "obi, and the chestnut link" fires the trigger
    # and rides the same-caller re-call path into a fresh compose.
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S,
        fired=True,
        caller=None,
        text="obi and the chestnut link",
        intelligible=True,
        request_tail="and the chestnut link",
    )
    assert list(outcome.effects) == [
        ComposeEffect(request="and the chestnut link", caller="nono", tier="operator")
    ]


def test_caller_clears_the_call_silently() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.on_compose_result(8 * S, "all good, over")
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    outcome = machine.on_transmission(
        12 * S, fired=False, caller=None, text="copy that, nono clear", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]
    assert machine.state == "idle"


def test_timeout_closes_silently_with_no_transmission() -> None:
    # FR-010: obi NEVER transmits a bare "clear" — expiry emits a Close effect only.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    assert machine.tick(2 * S + 44 * S) == []
    effects = machine.tick(2 * S + 46 * S)
    assert effects == [CloseEffect(closure="timeout", note=None)]
    assert machine.state == "idle"
    assert not any(isinstance(e, TransmitEffect) for e in effects)


def test_say_again_at_most_once_then_garble_close() -> None:
    # 093: the give-up is transmitted ("unable to copy, out"), the close deferred to
    # the TX result; the operator Slack note survives unchanged.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(5 * S, fired=False, caller=None, text="", intelligible=False)
    assert list(outcome.effects) == [TransmitEffect(kind="say_again", text="ten nine, say again")]
    machine.on_tx_result(7 * S, kind="say_again", disposition="transmitted")
    assert machine.state == "awaiting_request"
    outcome = machine.on_transmission(9 * S, fired=False, caller=None, text="", intelligible=False)
    assert list(outcome.effects) == [
        TransmitEffect(kind="garble_close", text="unable to copy, out")
    ]
    effects = machine.on_tx_result(11 * S, kind="garble_close", disposition="transmitted")
    assert len(effects) == 1
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "garble"
    assert close.note is not None  # Slack note (FR-011)
    assert machine.state == "idle"


def test_compose_failure_transmits_canned_unavailability_then_closes() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    effects = machine.on_compose_result(8 * S, None)  # model unreachable
    assert effects == [TransmitEffect(kind="unavailability", text="unable to comply, obi out")]
    effects = machine.on_tx_result(10 * S, kind="unavailability", disposition="transmitted")
    assert len(effects) == 1
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "failure" and close.note
    assert machine.state == "idle"


def test_compose_deadline_via_tick() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    # A long compose first emits the once-per-call standby courtesy (2026-08-20)…
    assert machine.tick(5 * S + 19 * S) == [TransmitEffect(kind="standby", text="stand by")]
    assert machine.tick(5 * S + 19 * S + 1) == []  # …exactly once
    effects = machine.tick(5 * S + 21 * S)
    assert effects == [TransmitEffect(kind="unavailability", text="unable to comply, obi out")]
    # A compose result landing after the deadline is discarded, not double-answered.
    assert machine.on_compose_result(5 * S + 22 * S, "late answer") == []


def test_dropped_ack_closes_dropped_with_note() -> None:
    machine = _machine()
    _call(machine)
    effects = machine.on_tx_result(60 * S, kind="ack", disposition="dropped_busy")
    assert len(effects) == 1
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "dropped" and close.note
    assert machine.state == "idle"


def test_dropped_answer_closes_dropped_with_note() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.on_compose_result(8 * S, "answer, over")
    effects = machine.on_tx_result(70 * S, kind="answer", disposition="dropped_busy")
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "dropped" and close.note
    assert machine.state == "idle"


def test_shadow_dispositions_walk_the_full_flow() -> None:
    # In shadow stages nothing is keyed, but the machine must still produce the answer
    # draft — shadow_logged/shadow_dmed count as success for flow purposes (SC-002 tuning).
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="shadow_logged")
    assert machine.state == "awaiting_request"
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    assert machine.state == "composing"


def test_transmissions_during_composing_are_ignored() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    outcome = machine.on_transmission(
        6 * S, fired=False, caller=None, text="other traffic", intelligible=True
    )
    assert list(outcome.effects) == []
    assert machine.state == "composing"


def test_idle_non_call_traffic_produces_nothing() -> None:
    machine = _machine()
    outcome = machine.on_transmission(
        0, fired=False, caller=None, text="yoyo this is nono", intelligible=True
    )
    assert list(outcome.effects) == [] and outcome.trigger_reason_override is None
    assert machine.state == "idle"


def test_ack_is_personalized_only_on_certain_identification() -> None:
    # Operator direction (2026-08-19): "mira, go for obi" when roster-identified;
    # plain "go for obi" for raw/unknown handles and anonymous calls.
    machine = _machine()
    outcome = machine.on_transmission(
        0, fired=True, caller="mira", text="obi obi mira", intelligible=True, caller_known=True
    )
    assert TransmitEffect(kind="ack", text="mira, go for obi") in outcome.effects

    machine2 = _machine()
    outcome = machine2.on_transmission(
        0,
        fired=True,
        caller="net control",
        text="obi this is net control",
        intelligible=True,
        caller_known=False,
    )
    assert TransmitEffect(kind="ack", text="go for obi") in outcome.effects

    machine3 = _machine()
    outcome = machine3.on_transmission(
        0, fired=True, caller=None, text="obi obi", intelligible=True
    )
    assert TransmitEffect(kind="ack", text="go for obi") in outcome.effects


def test_one_breath_call_skips_the_ack_and_composes_directly() -> None:
    # Operator direction 2026-08-20 (Cat's field test): when the question rode in with
    # the hail, "go for obi" is noise — compose immediately; the standby (now fired at
    # compose start) is the acknowledgment.
    machine = _machine()
    outcome = machine.on_transmission(
        0, fired=True, caller="mira", text="obi obi this is mira what's the uptime",
        intelligible=True, caller_known=True, request_tail="what's the uptime",
    )
    assert not any(
        isinstance(e, TransmitEffect) and e.kind == "ack" for e in outcome.effects
    )
    assert ComposeEffect(request="what's the uptime", caller="mira", tier="safe") in (
        outcome.effects
    )
    assert machine.state == "composing"


def _burn_answers(machine: CallMachine, rounds: int) -> None:
    """Consume ``rounds`` of the answer budget. Request 1 rides awaiting_request; the
    rest are fired re-hails with tails — the only follow-up path now that post-answer
    unaddressed traffic ends the call (operator direction 2026-08-20)."""

    machine.on_transmission(10 * S, fired=False, caller=None, text="question 1", intelligible=True)
    machine.on_compose_result(12 * S, "answer, over")
    machine.on_tx_result(14 * S, kind="answer", disposition="transmitted")
    for n in range(2, rounds + 1):
        machine.on_transmission(
            (n * 10) * S, fired=True, caller=None, text=f"obi question {n}",
            intelligible=True, request_tail=f"question {n}",
        )
        machine.on_compose_result((n * 10 + 2) * S, "answer, over")
        machine.on_tx_result((n * 10 + 4) * S, kind="answer", disposition="transmitted")


def test_answer_budget_closes_silently_then_frees_the_channel() -> None:
    # Operator direction: no infinite retries — after max_answers_per_call composed
    # answers the call closes with NO transmission, and the next call opens fresh.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller=None, text="obi question 3",
        intelligible=True, request_tail="question 3",
    )
    assert len(outcome.effects) == 1
    close = outcome.effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "abandoned" and close.note
    assert not any(isinstance(e, TransmitEffect) for e in outcome.effects)  # SILENT
    assert machine.state == "idle"
    # The channel is free: a new call acks immediately.
    outcome = machine.on_transmission(
        45 * S, fired=True, caller="nono", text="obi obi this is nono", intelligible=True
    )
    assert any(isinstance(e, TransmitEffect) for e in outcome.effects)


def test_clearing_is_honored_even_mid_compose_or_mid_answer() -> None:
    # Field 2026-08-19: "this is mira, um, stop, channel clear" arrived while obi was
    # transmitting junk-answer #4 and was swallowed — the call stuck open and the next
    # real call was rejected busy. The caller can always call it off.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="question", intelligible=True)
    assert machine.state == "composing"
    outcome = machine.on_transmission(
        6 * S, fired=False, caller=None, text="um stop channel clear", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]
    assert machine.state == "idle"
    # The in-flight compose result lands on an idle machine and is dropped.
    assert machine.on_compose_result(8 * S, "too late, over") == []


# --- L4: same-caller re-call while a call is open (field 2026-08-19 22:45:14) --------------


def test_recall_with_question_composes_in_awaiting_request() -> None:
    # The exact field case: after the ack, the caller re-hails WITH the question
    # ("Obi, what is the uptime of the NOC gateway?") — anonymous re-fire carrying a
    # request tail. It was discarded busy_with_call; it must compose.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S,
        fired=True,
        caller=None,
        text="obi what is the uptime of the noc gateway",
        intelligible=True,
        request_tail="what is the uptime of the noc gateway",
    )
    assert list(outcome.effects) == [
        ComposeEffect(
            request="what is the uptime of the noc gateway", caller="nono", tier="operator"
        )
    ]
    assert outcome.trigger_reason_override is None
    assert machine.state == "composing"


def test_recall_same_named_caller_with_tail_supersedes_and_composes() -> None:
    # Feature 129: re-stating the callsign is the long-form new-call opener — the open
    # call closes superseded and the request composes in a FRESH call (before 129 it
    # composed in-call, spending the old call's budget). The caller still gets the
    # answer in one breath; only the call accounting changes.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=True, caller="nono", text="obi obi this is nono radio check",
        intelligible=True, caller_known=True, request_tail="radio check",
    )
    assert list(outcome.effects) == [
        CloseEffect(closure="superseded", note=None),
        OpenCallEffect(caller="nono", tier="operator"),
        ComposeEffect(request="radio check", caller="nono", tier="operator"),
    ]


def test_recall_different_named_caller_supersedes_with_tail() -> None:
    # Feature 109: a different NAMED caller's one-breath hail mid-call supersedes and
    # composes directly (was busy-swallowed — "Obi, Obi, Mirar" went unanswered).
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=True, caller="yoyo", text="obi obi this is yoyo radio check",
        intelligible=True, caller_known=True, request_tail="radio check",
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert OpenCallEffect(caller="yoyo", tier="safe") in effects
    assert ComposeEffect(request="radio check", caller="yoyo", tier="safe") in effects
    assert machine.state == "composing"


def test_bare_recall_same_caller_draws_say_again() -> None:
    # Feature 109 US2 (radio.db call 75: "Obi, NASA, power, blue" got 25 s of dead
    # air): an addressed mid-call transmission with no extractable request draws the
    # canned say-again — never silence toward the call's own caller.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=True, caller="nono", text="obi obi this is nono",
        intelligible=True, caller_known=True,
    )
    assert list(outcome.effects) == [
        TransmitEffect(kind="say_again", text="ten nine, say again")
    ]
    assert outcome.trigger_reason_override == "say_again_no_tail"
    assert machine.state == "awaiting_request"


def test_anonymous_no_tail_refire_awaiting_request_draws_say_again() -> None:
    # The exact call-75 shape: trigger fired, handle parse mangled, nothing usable.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=True, caller=None, text="obi nasa power blue", intelligible=True
    )
    assert list(outcome.effects) == [
        TransmitEffect(kind="say_again", text="ten nine, say again")
    ]
    assert outcome.trigger_reason_override == "say_again_no_tail"


def test_no_tail_say_again_respects_the_spent_budget() -> None:
    # The once-per-call say-again budget is shared: a second unextractable addressed
    # transmission closes ON the air per 093, never in silence.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(
        5 * S, fired=True, caller="nono", text="obi obi this is nono",
        intelligible=True, caller_known=True,
    )
    machine.on_tx_result(7 * S, kind="say_again", disposition="transmitted")
    outcome = machine.on_transmission(
        10 * S, fired=True, caller="nono", text="obi obi this is nono",
        intelligible=True, caller_known=True,
    )
    assert list(outcome.effects) == [
        TransmitEffect(kind="garble_close", text="unable to copy, out")
    ]
    effects = machine.on_tx_result(12 * S, kind="garble_close", disposition="transmitted")
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "garble"


def test_same_caller_request_mid_compose_is_held_then_answered() -> None:
    # Feature 109 US2 (radio.db call 47: the re-hail carrying the request was swallowed
    # while an answer was composing): hold ONE pending request; when the in-flight
    # answer settles, the held request composes.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="first", intelligible=True)
    assert machine.state == "composing"
    outcome = machine.on_transmission(
        6 * S, fired=True, caller=None, text="obi set portal dancing shadows lava",
        intelligible=True, request_tail="set portal dancing shadows lava",
    )
    assert list(outcome.effects) == []
    assert outcome.trigger_reason_override == "held_for_settle"
    machine.on_compose_result(8 * S, "answer one, over")
    effects = machine.on_tx_result(
        10 * S, kind="answer", disposition="transmitted", text="answer one, over"
    )
    assert effects == [
        ComposeEffect(
            request="set portal dancing shadows lava",
            caller="nono",
            tier="operator",
            history=(Turn(request="first", answer="answer one, over"),),
        )
    ]
    assert machine.state == "composing"


def test_held_request_latest_wins() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="first", intelligible=True)
    machine.on_transmission(
        6 * S, fired=True, caller=None, text="obi try one", intelligible=True,
        request_tail="try one",
    )
    machine.on_transmission(
        7 * S, fired=True, caller=None, text="obi try two actually", intelligible=True,
        request_tail="try two actually",
    )
    machine.on_compose_result(8 * S, "answer one, over")
    effects = machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    composes = [e for e in effects if isinstance(e, ComposeEffect)]
    assert [c.request for c in composes] == ["try two actually"]


def test_held_request_dies_with_the_call() -> None:
    # A clear mid-compose discards the held request along with everything else.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="first", intelligible=True)
    machine.on_transmission(
        6 * S, fired=True, caller=None, text="obi try one", intelligible=True,
        request_tail="try one",
    )
    machine.on_transmission(7 * S, fired=False, caller=None, text="nono out", intelligible=True)
    assert machine.state == "idle"
    assert machine.on_compose_result(8 * S, "late, over") == []


def test_expects_reply_makes_a_bare_ack_compose() -> None:
    # Feature 109 US4 (radio.db call 72): obi's answer invited a reply; the caller's
    # "affirmative" must be treated as that reply, not as a clear.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="set lava", intelligible=True)
    machine.on_compose_result(
        8 * S, "on a solid pattern it won't show — want an animated effect? over",
        expects_reply=True,
    )
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    outcome = machine.on_transmission(
        14 * S, fired=False, caller=None, text="affirmative", intelligible=True
    )
    assert any(isinstance(e, ComposeEffect) for e in outcome.effects)
    assert machine.state == "composing"


def test_without_the_flag_a_bare_ack_still_clears() -> None:
    # The fail-safe: an unset flag is exactly the pre-109 behavior.
    machine = _machine()
    _answered(machine)  # declarative answer, no flag
    outcome = machine.on_transmission(
        14 * S, fired=False, caller=None, text="affirmative", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]


def test_final_token_clear_outranks_the_flag() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="set lava", intelligible=True)
    machine.on_compose_result(8 * S, "want an animated effect? over", expects_reply=True)
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    outcome = machine.on_transmission(
        14 * S, fired=False, caller=None, text="negative, nono out", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]
    assert machine.state == "idle"


def test_flag_dies_with_the_next_compose() -> None:
    # The flag belongs to ONE answer: once the reply composes a fresh (unflagged)
    # answer, bare acks clear again.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="set lava", intelligible=True)
    machine.on_compose_result(8 * S, "want an animated effect? over", expects_reply=True)
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    machine.on_transmission(14 * S, fired=False, caller=None, text="affirmative", intelligible=True)
    machine.on_compose_result(16 * S, "effect set to dancing shadows, over")
    machine.on_tx_result(18 * S, kind="answer", disposition="transmitted")
    outcome = machine.on_transmission(
        22 * S, fired=False, caller=None, text="thanks", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]


def test_flag_never_extends_expiry() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="set lava", intelligible=True)
    machine.on_compose_result(8 * S, "want an animated effect? over", expects_reply=True)
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    effects = machine.tick(10 * S + 46 * S)  # call_timeout_s=45
    assert [e for e in effects if isinstance(e, CloseEffect)][0].closure == "timeout"
    assert not any(isinstance(e, TransmitEffect) for e in effects)


def test_held_request_during_answering_too() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="first", intelligible=True)
    machine.on_compose_result(8 * S, "answer one, over")
    assert machine.state == "answering"
    outcome = machine.on_transmission(
        9 * S, fired=True, caller=None, text="obi and the chestnut link", intelligible=True,
        request_tail="and the chestnut link",
    )
    assert outcome.trigger_reason_override == "held_for_settle"
    effects = machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    composes = [e for e in effects if isinstance(e, ComposeEffect)]
    assert [c.request for c in composes] == ["and the chestnut link"]


def test_recall_in_open_state_composes_follow_up() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.on_compose_result(8 * S, "all good, over")
    machine.on_tx_result(10 * S, kind="answer", disposition="transmitted")
    outcome = machine.on_transmission(
        15 * S, fired=True, caller=None, text="obi and how is the chestnut link doing",
        intelligible=True, request_tail="and how is the chestnut link doing",
    )
    assert list(outcome.effects) == [
        ComposeEffect(request="and how is the chestnut link doing", caller="nono", tier="operator")
    ]


def test_recall_compose_respects_answer_budget() -> None:
    # A NON-hail follow-up over budget still abandons silently (the junk-loop belt)…
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller=None, text="obi one more question please",
        intelligible=True, request_tail="one more question please",
    )
    assert len(outcome.effects) == 1
    close = outcome.effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "abandoned"
    assert machine.state == "idle"


# --- STANDBY while composing (operator request 2026-08-20, the radio 'thinking…') ----------


def _machine_with_standby(**overrides: object) -> CallMachine:
    defaults: dict[str, object] = {"standby_after_s": 4}
    defaults.update(overrides)
    return _machine(**defaults)


def test_standby_transmits_once_when_composing_runs_long() -> None:
    machine = _machine_with_standby()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    assert machine.state == "composing"
    assert machine.tick(5 * S + 3 * S) == []  # not yet
    effects = machine.tick(5 * S + 5 * S)
    assert effects == [TransmitEffect(kind="standby", text="stand by")]
    assert machine.state == "composing"  # still waiting on the model
    assert machine.tick(5 * S + 7 * S) == []  # once per call, never nags
    # The standby's tx result never disturbs the compose in flight.
    assert machine.on_tx_result(5 * S + 8 * S, kind="standby", disposition="transmitted") == []
    effects = machine.on_compose_result(5 * S + 12 * S, "all good, over")
    assert effects == [TransmitEffect(kind="answer", text="all good, over")]


def test_no_standby_when_the_answer_is_fast() -> None:
    machine = _machine_with_standby()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.on_compose_result(7 * S, "quick answer, over")  # before standby_after_s
    assert machine.tick(20 * S) == []  # nothing pending — never a stray standby


def test_standby_zero_fires_on_the_first_tick() -> None:
    # standby_after_s=0 (operator direction 2026-08-20): "stand by" airs as soon as
    # composing starts — an answer always takes seconds anyway.
    machine = _machine_with_standby(standby_after_s=0)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    assert machine.tick(5 * S) == [TransmitEffect(kind="standby", text="stand by")]
    assert machine.tick(6 * S) == []  # still once per call


def test_each_composed_request_draws_its_own_standby() -> None:
    # Field 2026-08-20 15:59: the follow-up question ran 18 s of tool calls in silence —
    # once-per-CALL standby predates standby-as-acknowledgment. Every consumed request
    # gets one (and only one) "stand by".
    machine = _machine_with_standby()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="one", intelligible=True)
    assert machine.tick(10 * S) != []  # standby for the first compose
    assert machine.tick(11 * S) == []  # but only once per compose
    machine.on_compose_result(12 * S, "answer one, over")
    machine.on_tx_result(14 * S, kind="answer", disposition="transmitted")
    machine.on_transmission(
        16 * S, fired=False, caller=None, text="and the manzanita link?", intelligible=True
    )
    assert machine.state == "composing"  # the post-answer question composed
    assert machine.tick(22 * S) == [
        TransmitEffect(kind="standby", text="stand by")
    ]  # the follow-up compose draws its own standby


def test_compose_deadline_still_wins_after_standby() -> None:
    machine = _machine_with_standby()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.tick(10 * S)  # standby out
    effects = machine.tick(5 * S + 21 * S)  # compose deadline (20 s) expires
    assert effects == [TransmitEffect(kind="unavailability", text="unable to comply, obi out")]


def test_full_rehail_over_budget_supersedes_into_a_fresh_call() -> None:
    # Field 2026-08-20 10:03: after two answers were burned on a mis-heard site name, the
    # caller RE-HAILED with the question ("Obi, Obi, this is Mira, what's the status of
    # the NOC gateway?") and got silence — the budget belt strangled a live conversation.
    # A full hail IS new-call intent: the exhausted call closes and a fresh one opens.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller="nono", text="obi obi this is nono what is the noc status",
        intelligible=True, caller_known=True, request_tail="what is the noc status",
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert any(isinstance(e, OpenCallEffect) for e in effects)
    # One-breath discipline: no ack — the fresh call composes the question directly.
    assert ComposeEffect(
        request="what is the noc status", caller="nono", tier="operator"
    ) in effects
    assert machine.state == "composing"


def test_anonymous_doubled_rehail_over_budget_supersedes() -> None:
    # Field 2026-09-01, radio.db call 187 (vertigo): four answers spent — one of them on a
    # "say again" for a mis-heard request — then "Obi, Obi, revert the tower to the
    # previous state" met PURE SILENCE. The doubled name is this channel's new-call
    # opener, but a one-breath request cannot carry a parsed callsign (``detect`` fills
    # ``caller`` only from a bare roster handle or the "this is <name>" form), so gating
    # the budget escape hatch on the callsign left it dead for exactly this traffic.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller=None, text="obi obi revert the tower to the previous state",
        intelligible=True, request_tail="revert the tower to the previous state",
        doubled=True,
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert any(isinstance(e, OpenCallEffect) for e in effects)
    assert not any(isinstance(e, CloseEffect) and e.closure == "abandoned" for e in effects)
    assert machine.state == "composing"


def test_anonymous_doubled_rehail_keeps_the_established_identity() -> None:
    # The re-hail is the SAME station carrying on: the fresh call inherits the caller and
    # tier the old one established, rather than demoting a known operator to anonymous/safe.
    machine = _machine(max_answers_per_call=2)
    _call(machine)  # opens as "nono", an operator handle
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller=None, text="obi obi one more thing",
        intelligible=True, request_tail="one more thing", doubled=True,
    )
    assert OpenCallEffect(caller="nono", tier="operator") in list(outcome.effects)
    assert ComposeEffect(request="one more thing", caller="nono", tier="operator") in list(
        outcome.effects
    )


def test_single_name_refire_over_budget_still_abandons() -> None:
    # The belt that stops a junk loop is unchanged: only the DOUBLED opener is new-call
    # intent. A single-name re-fire ("obi, one more question") is a continuation, and over
    # budget it still closes silently.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller=None, text="obi one more question please",
        intelligible=True, request_tail="one more question please", doubled=False,
    )
    assert list(outcome.effects) == [
        e for e in outcome.effects if isinstance(e, CloseEffect) and e.closure == "abandoned"
    ]
    assert machine.state == "idle"


# --- a new call starts a new budget (feature 129, field 2026-09-02 call 227) --------------


def test_doubled_rehail_with_budget_remaining_supersedes_into_a_fresh_call() -> None:
    # Field 2026-09-02, radio.db call 227: "Obi, Obi, where is the art piece called
    # Broken Dreams?" arrived with 3 of 4 answers spent and was folded in as answer #4,
    # so the terminal sign-off turned a clarifying question into "…over and out?".
    # Operator ruling: the doubled name is the formal new-call opener — fresh call,
    # fresh budget, EVERY time, not only over an exhausted call.
    machine = _machine(max_answers_per_call=4)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 3)
    outcome = machine.on_transmission(
        60 * S, fired=True, caller=None, text="obi obi where is broken dreams",
        intelligible=True, request_tail="where is broken dreams", doubled=True,
    )
    effects = list(outcome.effects)
    closes = [i for i, e in enumerate(effects) if isinstance(e, CloseEffect)]
    opens = [i for i, e in enumerate(effects) if isinstance(e, OpenCallEffect)]
    assert len(closes) == 1 and effects[closes[0]].closure == "superseded"  # type: ignore[union-attr]
    assert len(opens) == 1 and closes[0] < opens[0]  # close the old call BEFORE the open
    assert ComposeEffect(
        request="where is broken dreams", caller="nono", tier="operator"
    ) in effects
    # The fresh call's first answer is 1 of 4 — no terminal sign-off on a question.
    answer = machine.on_compose_result(62 * S, "closest match is X, that the one, over")
    assert answer == [
        TransmitEffect(kind="answer", text="closest match is X, that the one, over")
    ]
    machine.on_tx_result(64 * S, kind="answer", disposition="transmitted")
    # The solicited reply composes — no silent abandoned close (the 227 failure mode).
    followup = machine.on_transmission(
        66 * S, fired=False, caller=None, text="yes that's the one", intelligible=True
    )
    assert any(isinstance(e, ComposeEffect) for e in followup.effects)


def test_single_name_refire_with_budget_remaining_stays_a_continuation() -> None:
    # The junk-loop belt is untouched: "obi, one more question" is a continuation,
    # spending the SAME call's budget — no close, no fresh open.
    machine = _machine(max_answers_per_call=4)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=True, caller=None, text="obi one more question",
        intelligible=True, request_tail="one more question", doubled=False,
    )
    effects = list(outcome.effects)
    assert not any(isinstance(e, (CloseEffect, OpenCallEffect)) for e in effects)
    assert any(isinstance(e, ComposeEffect) for e in effects)
    # It IS bound by the shared budget: this compose is answer #3, the next-but-one
    # continuation meets the belt (pinned by the over-budget tests above).


def test_callsign_rehail_with_budget_remaining_supersedes() -> None:
    # "Obi, this is Mira, next question" re-states identity — the long-form new-call
    # opener gets the same fresh call whatever the old call's counter says.
    machine = _machine(max_answers_per_call=4)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 1)
    outcome = machine.on_transmission(
        30 * S, fired=True, caller="nono", text="obi this is nono what is the noc status",
        intelligible=True, caller_known=True, request_tail="what is the noc status",
        doubled=False,
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert OpenCallEffect(caller="nono", tier="operator") in effects
    assert ComposeEffect(
        request="what is the noc status", caller="nono", tier="operator"
    ) in effects


def test_anonymous_doubled_rehail_under_budget_keeps_identity() -> None:
    # Same rule as the over-budget hatch always had: an anonymous doubled re-hail is
    # the same station carrying on — identity and tier carry into the fresh call.
    machine = _machine(max_answers_per_call=4)
    _call(machine)  # opens as "nono", an operator handle
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 1)
    outcome = machine.on_transmission(
        30 * S, fired=True, caller=None, text="obi obi one more thing",
        intelligible=True, request_tail="one more thing", doubled=True,
    )
    assert OpenCallEffect(caller="nono", tier="operator") in list(outcome.effects)


def test_held_doubled_hail_drains_into_a_fresh_call() -> None:
    # Feature 129 US3: a formal hail mid-answer holds (one slot — obi finishes what it
    # is saying), but at the settle drain it opens a FRESH call instead of spending the
    # old call's budget.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="question 1", intelligible=True)
    machine.on_compose_result(7 * S, "answer 1, over")
    assert machine.state == "answering"
    held = machine.on_transmission(
        8 * S, fired=True, caller=None, text="obi obi where is broken dreams",
        intelligible=True, request_tail="where is broken dreams", doubled=True,
    )
    assert held.effects == () and held.trigger_reason_override == "held_for_settle"
    effects = machine.on_tx_result(9 * S, kind="answer", disposition="transmitted")
    closes = [i for i, e in enumerate(effects) if isinstance(e, CloseEffect)]
    opens = [i for i, e in enumerate(effects) if isinstance(e, OpenCallEffect)]
    assert len(closes) == 1 and effects[closes[0]].closure == "superseded"  # type: ignore[union-attr]
    assert len(opens) == 1 and closes[0] < opens[0]
    assert ComposeEffect(
        request="where is broken dreams", caller="nono", tier="operator"
    ) in effects
    # Fresh budget: this answer would have been the OLD call's #2 of 2 (terminal,
    # "over and out"); in the fresh call it is #1 and keeps its plain "over".
    answer = machine.on_compose_result(11 * S, "held answer, over")
    assert answer == [TransmitEffect(kind="answer", text="held answer, over")]


def test_held_single_name_refire_drains_into_the_same_call() -> None:
    # The 109 D3.2 walk is unchanged for a continuation: held, then composed in THIS
    # call, bound by its budget.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="question 1", intelligible=True)
    machine.on_compose_result(7 * S, "answer 1, over")
    machine.on_transmission(
        8 * S, fired=True, caller=None, text="obi also the uptime",
        intelligible=True, request_tail="also the uptime", doubled=False,
    )
    effects = machine.on_tx_result(9 * S, kind="answer", disposition="transmitted")
    assert not any(isinstance(e, (CloseEffect, OpenCallEffect)) for e in effects)
    assert any(isinstance(e, ComposeEffect) for e in effects)
    # Same call, so this compose is answer #2 of 2 — the terminal sign-off applies.
    answer = machine.on_compose_result(11 * S, "uptime is 4 days, over")
    assert answer == [
        TransmitEffect(kind="answer", text="uptime is 4 days, over and out")
    ]


def test_held_formal_hail_dies_with_a_failure_close() -> None:
    # Plan D3, pinned: the hold is best-effort continuation, not a delivery queue — when
    # the in-flight answer closes the call (compose failure here), the held request and
    # its new-call intent die with it.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="question 1", intelligible=True)
    machine.on_transmission(
        6 * S, fired=True, caller=None, text="obi obi where is broken dreams",
        intelligible=True, request_tail="where is broken dreams", doubled=True,
    )
    machine.on_compose_result(7 * S, None)  # compose failed → unavailability signs off
    effects = machine.on_tx_result(9 * S, kind="unavailability", disposition="transmitted")
    assert any(isinstance(e, CloseEffect) and e.closure == "failure" for e in effects)
    assert not any(isinstance(e, (ComposeEffect, OpenCallEffect)) for e in effects)
    assert machine.state == "idle"


# --- post-answer follow-ups (field 2026-08-20 17:32) --------------------------------------


def _post_answer(text: str) -> tuple[CallMachine, list[object]]:
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        12 * S, fired=False, caller=None, text=text, intelligible=True
    )
    return machine, list(outcome.effects)


def test_post_answer_request_without_a_question_mark_still_composes() -> None:
    # "Okay, but I want the latency from NOC to google.com." — a fully-specified request
    # that the "?"-only test read as chatter, closing the call and answering nothing.
    machine, effects = _post_answer("Okay, but I want the latency from NOC to google.com.")
    assert any(isinstance(e, ComposeEffect) for e in effects)
    assert machine.state == "composing"


def test_post_answer_question_still_composes() -> None:
    machine, effects = _post_answer("Thank you, what about the magnolia to manzanita link?")
    assert any(isinstance(e, ComposeEffect) for e in effects)
    assert machine.state == "composing"


def test_post_answer_chatter_now_composes_in_window() -> None:
    # Feature 109: in-window chatter about obi IS answered now — the operator accepted
    # this trade-off explicitly (2026-08-28) to stop dropping real follow-ups; the
    # answer budget and window expiry bound it. OUTSIDE a window (idle) nothing changed.
    machine, effects = _post_answer("Yeah, obi has been pretty useful today.")
    assert any(isinstance(e, ComposeEffect) for e in effects)
    assert machine.state == "composing"


def test_post_answer_clearing_still_closes_the_call() -> None:
    machine, effects = _post_answer("Copy, thank you, channel clear.")
    assert not any(isinstance(e, ComposeEffect) for e in effects)
    assert machine.state == "idle"


# --- feature 109 US1: the window belongs to the caller ------------------------------------


def test_post_answer_imperative_composes() -> None:
    # radio.db call 68 (2026-08-28 21:05): "Set portal to 100%." silently closed the
    # call — no "?" and no marker phrase. Imperatives are how portal-era callers talk.
    machine, effects = _post_answer("Set portal to 100%.")
    assert any(isinstance(e, ComposeEffect) for e in effects)
    assert machine.state == "composing"


def test_post_answer_verbless_request_composes() -> None:
    # radio.db call 70 (Mira): "Thank you. Thank you. Just for status of the tamarack to
    # mountain, 70 gigahertz, link." — no verb at all, still a request.
    machine, effects = _post_answer(
        "Thank you. Thank you. Just for status of the tamarack to mountain, 70 gigahertz, link."
    )
    assert any(isinstance(e, ComposeEffect) for e in effects)


def test_post_answer_yes_plus_imperative_composes() -> None:
    # radio.db call 72: "Yes, set the effect to dancing shadows." answered obi's own
    # caveat and was dropped.
    machine, effects = _post_answer("Yes, set the effect to dancing shadows.")
    assert any(isinstance(e, ComposeEffect) for e in effects)


def test_post_answer_sign_off_closes_cleared() -> None:
    # radio.db call 5: "Mira, back on IT." is a sign-off, not a request — under the
    # widened gate it must close the call, not draw an answer at someone walking away.
    machine, effects = _post_answer("Mira, back on IT.")
    assert effects == [CloseEffect(closure="cleared", note=None)]
    assert machine.state == "idle"


def test_post_answer_moving_sign_off_closes() -> None:
    machine, effects = _post_answer("Copy, moving back to artery.")
    assert effects == [CloseEffect(closure="cleared", note=None)]


def test_sign_off_reason_is_recorded() -> None:
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S, fired=False, caller=None, text="heading back to the noc", intelligible=True
    )
    assert outcome.trigger_reason_override == "sign_off_close"


def test_config_sign_off_phrase_closes() -> None:
    machine = _machine(sign_offs=["gone riding"])
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S, fired=False, caller=None, text="thanks obi, gone riding", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]


def test_sign_off_shape_inside_a_request_still_composes() -> None:
    # "back to" mid-sentence is a REQUEST shape ("set the portal back to red"), not a
    # sign-off — the sign-off shape must open the transmission (token 0 or 1).
    machine, effects = _post_answer("Set the portal back to red.")
    assert any(isinstance(e, ComposeEffect) for e in effects)


def test_awaiting_request_sign_off_closes_too() -> None:
    # The caller hails, gets the ack, then walks: "moving to the gate" must close, not
    # compose an answer to a departure.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=False, caller=None, text="moving to the gate", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]
    assert outcome.trigger_reason_override == "sign_off_close"


def test_post_answer_compose_respects_the_budget() -> None:
    # The widened gate widens WHAT composes, never HOW MUCH: over budget, the silent
    # abandoned close is unchanged.
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    _burn_answers(machine, 2)
    outcome = machine.on_transmission(
        40 * S, fired=False, caller=None, text="set the portal purple", intelligible=True
    )
    assert len(outcome.effects) == 1
    close = outcome.effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "abandoned"


def test_named_hail_supersedes_mid_compose() -> None:
    # radio.db call 47 companion: while an answer is composing, another NAMED caller's
    # full hail is another call — supersede (the in-flight compose result is dropped by
    # the runner's 091 call-id race guard, exercised in its own tests).
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    assert machine.state == "composing"
    outcome = machine.on_transmission(
        6 * S, fired=True, caller="yoyo", text="obi obi this is yoyo radio check",
        intelligible=True, caller_known=True, request_tail="radio check",
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert ComposeEffect(request="radio check", caller="yoyo", tier="safe") in effects


def test_named_hail_supersedes_mid_answering() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.on_compose_result(8 * S, "all good, over")
    assert machine.state == "answering"
    outcome = machine.on_transmission(
        9 * S, fired=True, caller="yoyo", text="obi obi this is yoyo",
        intelligible=True, caller_known=True,
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert any(isinstance(e, TransmitEffect) and e.kind == "ack" for e in effects)
    assert machine.state == "acking"


# -- feature 091: the per-call transcript (call memory) --------------------------------


def _exchange(machine: CallMachine, n: int, request: str, aired: str) -> None:
    """One follow-up turn via re-hail with tail: compose 'draft', air ``aired``."""

    machine.on_transmission(
        (n * 10) * S, fired=True, caller=None, text=f"obi {request}",
        intelligible=True, request_tail=request,
    )
    machine.on_compose_result((n * 10 + 2) * S, f"draft for {request}, over")
    machine.on_tx_result(
        (n * 10 + 4) * S, kind="answer", disposition="transmitted", text=aired
    )


def test_answer_tx_success_appends_the_aired_text_not_the_draft() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(
        5 * S, fired=False, caller=None, text="rtt noc to manzanita", intelligible=True
    )
    machine.on_compose_result(8 * S, "a long draft the cap will cut, over")
    machine.on_tx_result(
        10 * S, kind="answer", disposition="transmitted", text="a long draft the cap"
    )
    assert machine.transcript == [
        Turn(request="rtt noc to manzanita", answer="a long draft the cap")
    ]


def test_shadow_dispositions_append_to_the_transcript_too() -> None:
    for disposition in ("shadow_logged", "shadow_dmed"):
        machine = _machine()
        _call(machine)
        machine.on_tx_result(2 * S, kind="ack", disposition=disposition)
        machine.on_transmission(5 * S, fired=False, caller=None, text="q", intelligible=True)
        machine.on_compose_result(8 * S, "a, over")
        machine.on_tx_result(10 * S, kind="answer", disposition=disposition, text="a, over")
        assert machine.transcript == [Turn(request="q", answer="a, over")], disposition


def test_answer_tx_without_text_appends_nothing_and_does_not_raise() -> None:
    machine = _machine()
    _answered(machine)  # legacy-shaped tx result: no text kwarg
    assert machine.transcript == []
    assert machine.state == "open"


def test_non_answer_tx_results_never_touch_the_transcript() -> None:
    machine = _machine(standby_after_s=1)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted", text="go for obi")
    machine.on_transmission(5 * S, fired=False, caller=None, text="mumble", intelligible=False)
    machine.on_tx_result(6 * S, kind="say_again", disposition="transmitted", text="ten nine")
    machine.on_transmission(
        7 * S, fired=False, caller=None, text="real question", intelligible=True
    )
    machine.tick(9 * S)  # standby fires while composing
    machine.on_tx_result(9 * S, kind="standby", disposition="transmitted", text="stand by")
    assert machine.transcript == []


def test_dropped_answer_appends_nothing() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="q", intelligible=True)
    machine.on_compose_result(8 * S, "a, over")
    machine.on_tx_result(
        10 * S, kind="answer", disposition="blocked_channel_busy", text="a, over"
    )
    assert machine.transcript == []


def test_transcript_trims_to_max_history_turns_keeping_newest() -> None:
    machine = _machine(max_history_turns=2, max_answers_per_call=10)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="q1", intelligible=True)
    machine.on_compose_result(6 * S, "draft, over")
    machine.on_tx_result(7 * S, kind="answer", disposition="transmitted", text="a1")
    _exchange(machine, 2, "q2", "a2")
    _exchange(machine, 3, "q3", "a3")
    assert machine.transcript == [Turn(request="q2", answer="a2"), Turn(request="q3", answer="a3")]


def test_compose_effect_carries_a_snapshot_excluding_the_live_request() -> None:
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    first = machine.on_transmission(
        5 * S, fired=False, caller=None, text="q1", intelligible=True
    )
    compose1 = next(e for e in first.effects if isinstance(e, ComposeEffect))
    assert compose1.history == ()  # nothing before the first turn
    machine.on_compose_result(6 * S, "draft one, over")
    machine.on_tx_result(7 * S, kind="answer", disposition="transmitted", text="aired one, over")
    second = machine.on_transmission(
        20 * S, fired=True, caller=None, text="obi q2", intelligible=True, request_tail="q2"
    )
    compose2 = next(e for e in second.effects if isinstance(e, ComposeEffect))
    assert compose2.history == (Turn(request="q1", answer="aired one, over"),)
    # A snapshot, not a view: later machine turns never mutate an emitted effect.
    _exchange(machine, 3, "q3", "a3")
    assert compose2.history == (Turn(request="q1", answer="aired one, over"),)


def _one_turn_open(**overrides: object) -> CallMachine:
    """A machine in state 'open' with one completed turn in the transcript."""

    machine = _machine(**overrides)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="q1", intelligible=True)
    machine.on_compose_result(6 * S, "a1, over")
    machine.on_tx_result(7 * S, kind="answer", disposition="transmitted", text="a1, over")
    assert machine.state == "open" and len(machine.transcript) == 1
    return machine


def test_every_closure_reason_discards_the_transcript() -> None:
    def cleared() -> CallMachine:
        m = _one_turn_open()
        m.on_transmission(
            9 * S, fired=False, caller=None, text="copy, nono clear", intelligible=True
        )
        return m

    def timeout() -> CallMachine:
        m = _one_turn_open()
        m.tick(120 * S)
        return m

    def sign_off() -> CallMachine:
        # Feature 109: the silent 'complete' close is gone (in-window traffic composes);
        # the sign-off close is the new caller-ended silent closure in its place.
        m = _one_turn_open()
        m.on_transmission(
            9 * S, fired=False, caller=None, text="copy, moving to the gate", intelligible=True
        )
        return m

    def garble() -> CallMachine:
        m = _one_turn_open()
        m.on_transmission(9 * S, fired=False, caller=None, text="", intelligible=False)
        m.on_tx_result(10 * S, kind="say_again", disposition="transmitted")
        m.on_transmission(12 * S, fired=False, caller=None, text="", intelligible=False)
        # 093: the close now rides the garble_close TX result.
        m.on_tx_result(13 * S, kind="garble_close", disposition="transmitted")
        return m

    def failure() -> CallMachine:
        m = _one_turn_open()
        m.on_transmission(
            9 * S, fired=False, caller=None, text="and the chestnut one?", intelligible=True
        )
        m.on_compose_result(11 * S, None)
        m.on_tx_result(12 * S, kind="unavailability", disposition="transmitted")
        return m

    def dropped() -> CallMachine:
        m = _one_turn_open()
        m.on_transmission(
            9 * S, fired=False, caller=None, text="and the chestnut one?", intelligible=True
        )
        m.on_compose_result(11 * S, "a2, over")
        m.on_tx_result(12 * S, kind="answer", disposition="blocked_channel_busy", text="a2, over")
        return m

    def superseded() -> CallMachine:
        m = _one_turn_open()
        m.on_transmission(
            9 * S, fired=True, caller="yoyo", text="obi obi this is yoyo", intelligible=True
        )
        return m

    def abandoned() -> CallMachine:
        m = _one_turn_open(max_answers_per_call=1)
        m.on_transmission(
            9 * S, fired=False, caller=None, text="and the chestnut one?", intelligible=True
        )
        return m

    for scenario in (cleared, timeout, sign_off, garble, failure, dropped, superseded, abandoned):
        machine = scenario()
        assert machine.transcript == [], scenario.__name__


def test_supersede_with_carried_question_composes_with_empty_history() -> None:
    # The over-budget named re-hail closes the old call and composes the carried
    # question on a FRESH one — its ComposeEffect must not see the dead call's turns.
    machine = _one_turn_open(max_answers_per_call=1)
    outcome = machine.on_transmission(
        9 * S, fired=True, caller="nono", text="obi obi this is nono what about chestnut",
        intelligible=True, request_tail="what about chestnut", caller_known=True,
    )
    compose = next(e for e in outcome.effects if isinstance(e, ComposeEffect))
    assert compose.history == ()
    closes = [e for e in outcome.effects if isinstance(e, CloseEffect)]
    assert [c.closure for c in closes] == ["superseded"]


def test_clear_during_compose_discards_transcript_and_pending_turn() -> None:
    machine = _one_turn_open()
    machine.on_transmission(
        9 * S, fired=False, caller=None, text="and chestnut?", intelligible=True
    )
    assert machine.state == "composing"
    machine.on_transmission(10 * S, fired=False, caller=None, text="nono out", intelligible=True)
    assert machine.state == "idle" and machine.transcript == []
    # The in-flight result lands on an idle machine: no effects, and the NEXT call's
    # first compose still sees empty history.
    assert machine.on_compose_result(11 * S, "late, over") == []
    _call(machine, now=20 * S)
    machine.on_tx_result(21 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(22 * S, fired=False, caller=None, text="q", intelligible=True)
    compose = next(e for e in outcome.effects if isinstance(e, ComposeEffect))
    assert compose.history == ()


def test_say_again_leaves_no_unanswered_residue_in_history() -> None:
    # US3: the garbled attempt never becomes a turn; the repeat pairs with ITS answer.
    machine = _one_turn_open()
    machine.on_transmission(9 * S, fired=False, caller=None, text="", intelligible=False)
    machine.on_tx_result(10 * S, kind="say_again", disposition="transmitted")
    outcome = machine.on_transmission(
        12 * S, fired=False, caller=None, text="q2 repeated", intelligible=True
    )
    compose = next(e for e in outcome.effects if isinstance(e, ComposeEffect))
    assert compose.history == (Turn(request="q1", answer="a1, over"),)
    machine.on_compose_result(13 * S, "a2, over")
    machine.on_tx_result(14 * S, kind="answer", disposition="transmitted", text="a2, over")
    assert machine.transcript == [
        Turn(request="q1", answer="a1, over"),
        Turn(request="q2 repeated", answer="a2, over"),
    ]


def test_failed_compose_turn_never_enters_history() -> None:
    machine = _one_turn_open()
    machine.on_transmission(
        9 * S, fired=False, caller=None, text="what about q2?", intelligible=True
    )
    effects = machine.on_compose_result(11 * S, None)  # model unreachable
    assert [e.kind for e in effects if isinstance(e, TransmitEffect)] == ["unavailability"]
    # The failed turn contributed nothing — history still holds only the completed one.
    assert machine.transcript == [Turn(request="q1", answer="a1, over")]
    machine.on_tx_result(12 * S, kind="unavailability", disposition="transmitted")
    assert machine.state == "idle" and machine.transcript == []


def test_compose_deadline_expiry_appends_nothing_and_the_late_answer_is_dropped() -> None:
    machine = _one_turn_open()
    machine.on_transmission(
        9 * S, fired=False, caller=None, text="what about q2?", intelligible=True
    )
    standby = machine.tick(14 * S)  # the courtesy fires first while composing runs long
    assert [e.kind for e in standby if isinstance(e, TransmitEffect)] == ["standby"]
    effects = machine.tick(40 * S)  # past the 20 s compose deadline
    assert [e.kind for e in effects if isinstance(e, TransmitEffect)] == ["unavailability"]
    assert machine.on_compose_result(41 * S, "too late, over") == []
    assert machine.transcript == [Turn(request="q1", answer="a1, over")]
    machine.on_tx_result(42 * S, kind="unavailability", disposition="transmitted")
    assert machine.transcript == []


# -- feature 091 FR-012: the knowably-last answer signs off terminally -----------------


def test_budget_spending_answer_signs_off_over_and_out() -> None:
    machine = _machine(max_answers_per_call=2)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="q1", intelligible=True)
    first = machine.on_compose_result(6 * S, "a1, over.")
    assert first == [TransmitEffect(kind="answer", text="a1, over.")]  # not last: verbatim
    machine.on_tx_result(7 * S, kind="answer", disposition="transmitted", text="a1, over.")
    machine.on_transmission(
        9 * S, fired=False, caller=None, text="what about q2?", intelligible=True
    )
    second = machine.on_compose_result(10 * S, "a2, over.")
    # A follow-up would meet the silent budget close — "over" would be a false promise.
    assert second == [TransmitEffect(kind="answer", text="a2, over and out.")]


def test_terminal_sign_off_appends_when_the_model_dropped_over() -> None:
    machine = _machine(max_answers_per_call=1)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="q", intelligible=True)
    effects = machine.on_compose_result(6 * S, "five by five")
    assert effects == [TransmitEffect(kind="answer", text="five by five, over and out")]


def test_terminal_sign_off_is_idempotent() -> None:
    machine = _machine(max_answers_per_call=1)
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="q", intelligible=True)
    effects = machine.on_compose_result(6 * S, "all links up, over and out")
    assert effects == [TransmitEffect(kind="answer", text="all links up, over and out")]


# -- feature 093 FR-006: the canned-phrase sign-off helper (bare ", out") --------------


def test_out_sign_off_appends_when_missing() -> None:
    assert _with_out_sign_off("unable to copy") == "unable to copy, out"


def test_out_sign_off_leaves_compliant_text_alone() -> None:
    assert _with_out_sign_off("unable to copy, out") == "unable to copy, out"
    assert _with_out_sign_off("obi Out.") == "obi Out."
    assert _with_out_sign_off("clearing out!") == "clearing out!"
    # "over and out" already ends with the word "out" — untouched, never stacked.
    assert _with_out_sign_off("all good, over and out") == "all good, over and out"


def test_out_sign_off_never_stacks() -> None:
    assert _with_out_sign_off(_with_out_sign_off("no copy")) == "no copy, out"


def test_out_sign_off_needs_the_word_not_the_substring() -> None:
    # "timeout" ends in the letters but not the WORD "out".
    assert _with_out_sign_off("hit a timeout") == "hit a timeout, out"


def test_out_sign_off_empty_text_unchanged() -> None:
    assert _with_out_sign_off("") == ""


# -- feature 093 FR-008: the unavailability phrase carries the sign-off ----------------
# (The shipped default "unable to comply, obi out" is pinned unchanged by
# test_compose_failure_transmits_canned_unavailability_then_closes above; the silent
# closes of FR-010 are pinned by the cleared/complete/timeout/supersede/dropped tests.)


def test_unavailability_gains_the_out_sign_off_when_config_lacks_it() -> None:
    machine = _machine(unavailable_text="unable to comply right now")
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    effects = machine.on_compose_result(8 * S, None)  # model unreachable
    assert effects == [
        TransmitEffect(kind="unavailability", text="unable to comply right now, out")
    ]


# -- feature 093 US3: the on-air garble close (FR-011/FR-012, contracts/call-protocol.md) --


def _garbled_out(machine: CallMachine) -> list[object]:
    """Drive to the second unintelligible request (say-again spent) at t=9 s."""

    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="", intelligible=False)
    machine.on_tx_result(7 * S, kind="say_again", disposition="transmitted")
    outcome = machine.on_transmission(9 * S, fired=False, caller=None, text="", intelligible=False)
    return list(outcome.effects)


def test_garble_close_path_a_transmits_then_defers_the_close() -> None:
    machine = _machine()
    effects = _garbled_out(machine)
    assert effects == [TransmitEffect(kind="garble_close", text="unable to copy, out")]
    # No CloseEffect yet: the runner's 091 race guard would drop a post-close TX.
    assert machine.state == "answering"


def test_garble_close_path_b_post_answer() -> None:
    machine = _machine()
    _answered(machine)
    machine.on_transmission(15 * S, fired=False, caller=None, text="", intelligible=False)
    machine.on_tx_result(17 * S, kind="say_again", disposition="transmitted")
    outcome = machine.on_transmission(20 * S, fired=False, caller=None, text="", intelligible=False)
    assert list(outcome.effects) == [
        TransmitEffect(kind="garble_close", text="unable to copy, out")
    ]
    assert machine.state == "answering"


@pytest.mark.parametrize(
    "disposition",
    ["transmitted", "shadow_logged", "shadow_dmed", "suppressed_rate", "dropped_busy",
     "blocked_tx_off"],
)
def test_garble_close_closes_on_any_disposition(disposition: str) -> None:
    # FR-012: the close depends on neither the phrase airing nor how the gate resolved.
    machine = _machine()
    _garbled_out(machine)
    effects = machine.on_tx_result(11 * S, kind="garble_close", disposition=disposition)
    assert len(effects) == 1
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "garble" and close.note
    assert machine.state == "idle"


def test_garble_close_text_is_configurable_and_signed_off() -> None:
    machine = _machine(garble_close_text="nothing heard")
    effects = _garbled_out(machine)
    assert effects == [TransmitEffect(kind="garble_close", text="nothing heard, out")]


def test_caller_clearing_mid_garble_close_wins() -> None:
    machine = _machine()
    _garbled_out(machine)
    outcome = machine.on_transmission(
        10 * S, fired=False, caller=None, text="thank you, channel clear", intelligible=True
    )
    assert list(outcome.effects) == [CloseEffect(closure="cleared", note=None)]
    # The late TX result lands on an idle machine and is dropped.
    assert machine.on_tx_result(12 * S, kind="garble_close", disposition="transmitted") == []


def test_named_hail_mid_garble_close_supersedes() -> None:
    # Feature 109: another NAMED caller during the in-flight garble close is another
    # call — supersede; the garble-close TX result lands on the fresh call harmlessly
    # (no 'answering' state to match) and the deferred note dies with the old call.
    machine = _machine()
    _garbled_out(machine)
    outcome = machine.on_transmission(
        10 * S, fired=True, caller="yoyo", text="obi obi this is yoyo",
        intelligible=True, caller_known=True,
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert machine.state == "acking"
    assert machine.on_tx_result(11 * S, kind="garble_close", disposition="transmitted") == []


def test_garble_close_watchdog_force_closes_without_tx() -> None:
    # Analysis U1: the TX result never arrives — the call timeout force-closes via
    # tick(), transmitting nothing. Entered at 9 s, call_timeout_s=45 ⇒ expiry at 54 s.
    machine = _machine()
    _garbled_out(machine)
    assert machine.tick(9 * S + 44 * S) == []
    effects = machine.tick(9 * S + 46 * S)
    assert len(effects) == 1
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "garble" and close.note
    assert not any(isinstance(e, TransmitEffect) for e in effects)
    assert machine.state == "idle"


def test_unavailability_watchdog_force_closes_failure() -> None:
    # The same watchdog covers the unavailability variant of the deferred close.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    machine.on_compose_result(8 * S, None)  # unavailability in flight; deadline 8+45 s
    assert machine.tick(8 * S + 44 * S) == []
    effects = machine.tick(8 * S + 46 * S)
    close = effects[0]
    assert isinstance(close, CloseEffect) and close.closure == "failure" and close.note
    assert machine.state == "idle"


def test_close_note_cleared_on_reset() -> None:
    machine = _machine()
    _garbled_out(machine)
    machine.on_tx_result(11 * S, kind="garble_close", disposition="transmitted")
    assert machine._close_note is None  # every closure funnels through _reset


# -- feature 093 SC-002/SC-003: the sign-off invariant over the closure taxonomy -------
# contracts/call-protocol.md: every close obi itself performs follows a transmission
# ending in the word "out"; every caller/time-ended close puts nothing new on the air.
# The regex is the contract, not intent detection — an answer that naturally ends with
# the word "out" satisfies it by definition (analysis U2).

_OUT_RE = re.compile(r"\bout[.!?]*$", re.IGNORECASE)


def _invariant_cases() -> list[tuple[str, str | None, list[object], bool]]:
    """Each case: (name, final on-air text or None, closing event's effects, obi-final?)."""

    cases: list[tuple[str, str | None, list[object], bool]] = []

    # obi-final: budget-spending answer, then the silent 'abandoned' close.
    m = _machine(max_answers_per_call=1)
    _call(m)
    m.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    m.on_transmission(5 * S, fired=False, caller=None, text="q1", intelligible=True)
    final = m.on_compose_result(6 * S, "a1, over")[0].text  # type: ignore[union-attr]
    m.on_tx_result(7 * S, kind="answer", disposition="transmitted", text=final)
    close = m.on_transmission(
        9 * S, fired=False, caller=None, text="what about q2?", intelligible=True
    ).effects
    cases.append(("abandoned-after-budget-final", final, list(close), True))

    # obi-final: compose failure → unavailability.
    m = _machine()
    _call(m)
    m.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    m.on_transmission(5 * S, fired=False, caller=None, text="q", intelligible=True)
    final = m.on_compose_result(6 * S, None)[0].text  # type: ignore[union-attr]
    fx = m.on_tx_result(8 * S, kind="unavailability", disposition="transmitted")
    cases.append(("failure", final, fx, True))

    # obi-final: both garble paths, plus the watchdog flavor.
    m = _machine()
    final = _garbled_out(m)[0].text  # type: ignore[union-attr]
    fx = m.on_tx_result(11 * S, kind="garble_close", disposition="transmitted")
    cases.append(("garble-request", final, fx, True))

    m = _machine()
    _answered(m)
    m.on_transmission(15 * S, fired=False, caller=None, text="", intelligible=False)
    m.on_tx_result(17 * S, kind="say_again", disposition="transmitted")
    final = list(
        m.on_transmission(20 * S, fired=False, caller=None, text="", intelligible=False).effects
    )[0].text  # type: ignore[union-attr]
    fx = m.on_tx_result(22 * S, kind="garble_close", disposition="transmitted")
    cases.append(("garble-post-answer", final, fx, True))

    m = _machine()
    final = _garbled_out(m)[0].text  # type: ignore[union-attr]
    cases.append(("garble-watchdog", final, m.tick(9 * S + 46 * S), True))

    # Caller/time-ended: silent, always.
    m = _machine()
    _answered(m)
    close = m.on_transmission(
        15 * S, fired=False, caller=None, text="thank you, channel clear", intelligible=True
    ).effects
    cases.append(("cleared", None, list(close), False))

    m = _machine()
    _answered(m)
    close = m.on_transmission(
        15 * S, fired=False, caller=None, text="thanks", intelligible=True
    ).effects
    cases.append(("cleared-bare-ack", None, list(close), False))

    m = _machine()
    _answered(m)
    close = m.on_transmission(
        15 * S, fired=False, caller=None, text="copy, moving to the gate", intelligible=True
    ).effects
    cases.append(("sign-off", None, list(close), False))

    m = _machine()
    _answered(m)
    cases.append(("timeout", None, m.tick(120 * S), False))

    m = _machine()
    _answered(m)
    close = m.on_transmission(
        16 * S, fired=True, caller="yoyo", text="obi obi this is yoyo", intelligible=True
    ).effects
    cases.append(("superseded", None, list(close), False))

    m = _machine()
    _call(m)
    fx = m.on_tx_result(70 * S, kind="ack", disposition="dropped_busy")
    cases.append(("dropped-ack", None, fx, False))

    return cases


def test_sign_off_invariant_over_the_closure_taxonomy() -> None:
    for name, final, closing_effects, obi_final in _invariant_cases():
        closes = [i for i, e in enumerate(closing_effects) if isinstance(e, CloseEffect)]
        assert closes, f"{name}: the closing event produced no CloseEffect"
        # Nothing airs in the closing event before the close itself (a supersede may
        # ack the NEW call after it — that transmission belongs to the new exchange).
        before = closing_effects[: closes[0]]
        assert not any(isinstance(e, TransmitEffect) for e in before), name
        if obi_final:
            assert final is not None and _OUT_RE.search(final), f"{name}: {final!r}"


# -- feature 114: third-party addressing -------------------------------------------------


def test_third_party_traffic_closes_the_open_window_silently() -> None:
    # Field 2026-08-29 (radio.db call 118): after obi answered Vertigo, Mira hailed
    # VERTIGO inside the still-open window and obi answered "Copy Mira, over." The
    # window belongs to the caller, but not to the whole channel.
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S,
        fired=False,
        caller=None,
        text="vertigo vertigo vertigo this is mira",
        intelligible=True,
        addressed_elsewhere=True,
    )
    effects = list(outcome.effects)
    assert effects == [CloseEffect(closure="third_party", note=None)]
    assert outcome.trigger_reason_override == "third_party_traffic"
    assert machine.state == "idle"


def test_third_party_traffic_closes_while_awaiting_the_request() -> None:
    # The ack went out and the channel moved on before the caller ever stated business.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    assert machine.state == "awaiting_request"
    outcome = machine.on_transmission(
        6 * S, fired=False, caller=None, text="go for vertigo",
        intelligible=True, addressed_elsewhere=True,
    )
    assert list(outcome.effects) == [CloseEffect(closure="third_party", note=None)]
    assert outcome.trigger_reason_override == "third_party_traffic"


def test_third_party_traffic_never_transmits() -> None:
    # The whole safety claim of feature 114 in one assertion: this path can only ever
    # REMOVE a transmission, from any state it is consulted in.
    for build in (_call, _answered):
        machine = _machine()
        build(machine)
        outcome = machine.on_transmission(
            20 * S, fired=False, caller=None, text="go for vertigo",
            intelligible=True, addressed_elsewhere=True,
        )
        assert not any(isinstance(e, TransmitEffect) for e in outcome.effects)


def test_third_party_traffic_mid_compose_leaves_the_answer_alone() -> None:
    # The in-flight answer is owed to the caller who asked for it — closing here would
    # cancel it (the runner's 091 call-id guard drops results for a dead call). Record
    # the reason, change nothing else; the window closes on the next one, or times out.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(5 * S, fired=False, caller=None, text="status", intelligible=True)
    assert machine.state == "composing"
    outcome = machine.on_transmission(
        7 * S, fired=False, caller=None, text="go for vertigo",
        intelligible=True, addressed_elsewhere=True,
    )
    assert list(outcome.effects) == []
    assert outcome.trigger_reason_override == "third_party_traffic"
    assert machine.state == "composing"
    # …and the answer still lands.
    effects = machine.on_compose_result(9 * S, "answer, over")
    assert any(isinstance(e, TransmitEffect) and e.kind == "answer" for e in effects)


def test_third_party_flag_is_ignored_when_the_trigger_fired() -> None:
    # FR-018: a transmission whose opening names obi belongs to the trigger and supersede
    # paths. A mangled hail TO obi ("Hopi, hopi, Vox.") reads as a third-party hail to the
    # addressing parser, and the trigger's verdict must win.
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S,
        fired=True,
        caller="vox",
        text="hopi hopi vox",
        intelligible=True,
        caller_known=True,
        addressed_elsewhere=True,
    )
    effects = list(outcome.effects)
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert any(isinstance(e, TransmitEffect) and e.kind == "ack" for e in effects)


def test_third_party_traffic_on_an_idle_channel_changes_nothing() -> None:
    # FR-012: the idle channel still requires a hail to obi, and other stations' traffic
    # leaves no gate reason behind — there is no window for it to have interrupted.
    machine = _machine()
    outcome = machine.on_transmission(
        0, fired=False, caller=None, text="go for vertigo",
        intelligible=True, addressed_elsewhere=True,
    )
    assert list(outcome.effects) == []
    assert outcome.trigger_reason_override is None
    assert machine.state == "idle"


def test_in_window_traffic_still_composes_without_the_flag() -> None:
    # The narrowing is bounded: absent an addressing verdict, feature 109's window-belongs
    # rule is untouched.
    machine = _machine()
    _answered(machine)
    outcome = machine.on_transmission(
        15 * S, fired=False, caller=None, text="set the portal to red", intelligible=True
    )
    assert any(isinstance(e, ComposeEffect) for e in outcome.effects)


def test_known_caller_supersedes_a_call_that_never_identified_anyone() -> None:
    # Field 2026-08-29 (radio.db tx 3233): obi's own bogus reply left an ANONYMOUS call
    # sitting in awaiting_request, and Mira's genuine hail drew "ten nine, say again"
    # instead of an ack — because a call with no caller counted as matching everybody.
    # A roster-known caller against a caller-less call is a different station.
    machine = _machine()
    _call(machine, caller=None)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    assert machine.state == "awaiting_request" and machine.caller is None
    outcome = machine.on_transmission(
        5 * S, fired=True, caller="nono", text="obi obi nono",
        intelligible=True, caller_known=True,
    )
    effects = list(outcome.effects)
    assert effects == [
        CloseEffect(closure="superseded", note=None),
        OpenCallEffect(caller="nono", tier="operator"),
        TransmitEffect(kind="ack", text="nono, go for obi"),
    ]
    assert machine.caller == "nono" and machine.tier == "operator"


def test_anonymous_refire_into_a_caller_less_call_is_still_the_same_caller() -> None:
    # FR-011, the leg that WAS argued: an anonymous re-fire is most likely our own
    # caller's audio, so it draws the say-again rather than hijacking the window.
    machine = _machine()
    _call(machine, caller=None)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=True, caller=None, text="obi obi", intelligible=True
    )
    assert outcome.trigger_reason_override == "say_again_no_tail"
    assert not any(isinstance(e, OpenCallEffect) for e in outcome.effects)


def test_unknown_stated_handle_does_not_supersede_a_caller_less_call() -> None:
    # Conservative by design: a handle the roster cannot confirm is most likely a
    # transcription artifact of the caller we already have, not a second station. Only a
    # roster-CERTAIN identification is allowed to take the window.
    machine = _machine()
    _call(machine, caller=None)
    machine.on_tx_result(2 * S, kind="ack", disposition="transmitted")
    outcome = machine.on_transmission(
        5 * S, fired=True, caller="herrar", text="obi obi this is herrar",
        intelligible=True, caller_known=False,
    )
    assert outcome.trigger_reason_override == "say_again_no_tail"
    assert not any(isinstance(e, OpenCallEffect) for e in outcome.effects)
    assert machine.caller is None


# --- feature 124: outbound calls (hailing → awaiting_go → delivering → open) ----------------


from obi.radio.calls import OutboundOutcomeEffect  # noqa: E402
from obi.radio.outbound import GoAheadDecision  # noqa: E402

GO = GoAheadDecision(matched=True, shape="go_for_callee")
NO_GO = GoAheadDecision(matched=False)


def _outbound(
    machine: CallMachine,
    now: int = 0,
    callee: str = "Dana",
    message: str = "the link is up",
    ttl_s: int = 600,
) -> list[object]:
    return list(
        machine.start_outbound(now, callee=callee, message=message, expires_us=now + ttl_s * S)
    )


def _hail_ok(machine: CallMachine, now: int) -> list[object]:
    return list(machine.on_tx_result(now, kind="hail", disposition="transmitted"))


def _rx(machine: CallMachine, now: int, text: str, **kw: object) -> object:
    defaults: dict[str, object] = {
        "fired": False,
        "caller": None,
        "text": text,
        "intelligible": True,
    }
    defaults.update(kw)
    return machine.on_transmission(now, **defaults)  # type: ignore[arg-type]


def test_start_outbound_opens_and_hails() -> None:
    machine = _machine()
    effects = _outbound(machine)
    assert effects == [
        OpenCallEffect(caller="Dana", tier="safe", direction="outbound"),
        TransmitEffect(kind="hail", text="Dana, Dana, obi"),
    ]
    assert machine.state == "hailing"
    assert machine.direction == "outbound"


def test_start_outbound_refused_when_not_idle() -> None:
    machine = _machine()
    _call(machine)  # an inbound call is open
    assert _outbound(machine) == []
    assert machine.state == "acking"  # untouched


def test_operator_callee_gets_operator_tier() -> None:
    machine = _machine()
    effects = _outbound(machine, callee="nono")
    assert effects[0] == OpenCallEffect(caller="nono", tier="operator", direction="outbound")


def test_hail_ok_opens_the_ack_window() -> None:
    machine = _machine()
    _outbound(machine)
    assert _hail_ok(machine, 2 * S) == []
    assert machine.state == "awaiting_go"
    assert machine.outbound_callee() == "Dana"


def test_go_ahead_delivers_the_fixed_message_framed() -> None:
    machine = _machine()
    _outbound(machine, message="the link is up")
    _hail_ok(machine, 2 * S)
    outcome = _rx(machine, 5 * S, "go for dana", go_ahead=GO)
    assert list(outcome.effects) == [
        TransmitEffect(kind="delivery", text="the link is up, over")
    ]
    assert outcome.trigger_reason_override == "outbound_go_ahead:go_for_callee"
    assert machine.state == "delivering"


def test_operator_framing_is_idempotent() -> None:
    machine = _machine()
    _outbound(machine, message="meet me at the NOC, over and out")
    _hail_ok(machine, 2 * S)
    outcome = _rx(machine, 5 * S, "go", go_ahead=GoAheadDecision(True, "bare_go"))
    assert list(outcome.effects) == [
        TransmitEffect(kind="delivery", text="meet me at the NOC, over and out")
    ]


def test_delivery_success_settles_and_becomes_a_normal_open_call() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 2 * S)
    _rx(machine, 5 * S, "go for dana", go_ahead=GO)
    effects = machine.on_tx_result(
        7 * S, kind="delivery", disposition="transmitted", text="the link is up, over"
    )
    assert effects == [OutboundOutcomeEffect(outcome="delivered", attempts=0)]
    assert machine.state == "open"
    assert machine.transcript[-1].answer == "the link is up, over"
    # The callee's reply composes under the ordinary 109 window rules…
    outcome = _rx(machine, 9 * S, "what was the outage duration")
    assert any(isinstance(e, ComposeEffect) for e in outcome.effects)
    # …and after obi's answer settles, a bare acknowledgment closes silently, with no
    # second request-outcome (the request settled at delivery).
    machine.on_compose_result(10 * S, "about an hour, over")
    machine.on_tx_result(
        11 * S, kind="answer", disposition="transmitted", text="about an hour, over"
    )
    outcome = _rx(machine, 12 * S, "copy that")
    assert [type(e) for e in outcome.effects] == [CloseEffect]
    assert machine.state == "idle"


def test_delivery_drop_fails_the_request() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 2 * S)
    _rx(machine, 5 * S, "go", go_ahead=GoAheadDecision(True, "bare_go"))
    effects = machine.on_tx_result(70 * S, kind="delivery", disposition="dropped_busy")
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1 and outcomes[0].outcome == "failed"
    assert any(isinstance(e, CloseEffect) and e.closure == "dropped" for e in effects)
    assert machine.state == "idle"


def test_outbound_shadow_dispositions_walk_the_full_flow() -> None:
    machine = _machine()
    _outbound(machine)
    assert machine.on_tx_result(2 * S, kind="hail", disposition="shadow_logged") == []
    assert machine.state == "awaiting_go"
    _rx(machine, 5 * S, "go", go_ahead=GoAheadDecision(True, "bare_go"))
    effects = machine.on_tx_result(7 * S, kind="delivery", disposition="shadow_dmed")
    assert effects == [OutboundOutcomeEffect(outcome="delivered", attempts=0)]


# --- US2: retries, standby, decline, TTL ---------------------------------------------------


def test_elapsed_windows_rehail_then_close_unanswered() -> None:
    machine = _machine(outbound_ack_window_s=30, outbound_max_rehails=2)
    _outbound(machine)
    _hail_ok(machine, 0)
    # First window elapses → re-hail #1.
    effects = machine.tick(31 * S)
    assert effects == [TransmitEffect(kind="hail", text="Dana, Dana, obi")]
    _hail_ok(machine, 32 * S)
    # Second window elapses → re-hail #2.
    effects = machine.tick(63 * S)
    assert effects == [TransmitEffect(kind="hail", text="Dana, Dana, obi")]
    _hail_ok(machine, 64 * S)
    # Third window elapses → attempts spent: unanswered, nothing more on the air.
    effects = machine.tick(95 * S)
    assert not any(isinstance(e, TransmitEffect) for e in effects)
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert outcomes == [
        OutboundOutcomeEffect(
            outcome="unanswered", detail="no go-ahead after 3 hails", attempts=3
        )
    ]
    assert any(isinstance(e, CloseEffect) and e.closure == "unanswered" for e in effects)
    assert machine.state == "idle"


def test_payload_never_airs_without_a_go_ahead() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 0)
    all_effects: list[object] = []
    for now in (31 * S, 63 * S, 95 * S):
        all_effects += machine.tick(now)
        if machine.state == "hailing":
            all_effects += _hail_ok(machine, now + S)
    assert not any(
        isinstance(e, TransmitEffect) and e.kind == "delivery" for e in all_effects
    )


def test_busy_hail_requeues_without_burning_a_retry() -> None:
    machine = _machine()
    _outbound(machine)
    effects = machine.on_tx_result(60 * S, kind="hail", disposition="dropped_busy")
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert outcomes == [
        OutboundOutcomeEffect(
            outcome="requeue", detail="channel busy — the hail never aired", attempts=0
        )
    ]
    assert any(isinstance(e, CloseEffect) and e.closure == "requeued" for e in effects)
    assert machine.state == "idle"


def test_blocked_hail_fails_terminally() -> None:
    # tx-off/serial-absent must NOT requeue-loop: report it and stop.
    machine = _machine()
    _outbound(machine)
    effects = machine.on_tx_result(2 * S, kind="hail", disposition="blocked_tx_off")
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1 and outcomes[0].outcome == "failed"


def test_standby_pauses_rehails_and_a_later_go_ahead_delivers() -> None:
    machine = _machine(outbound_ack_window_s=30)
    _outbound(machine, ttl_s=600)
    _hail_ok(machine, 0)
    outcome = _rx(machine, 10 * S, "standby, obi")
    assert outcome.trigger_reason_override == "outbound_standby"
    assert machine.state == "standing_by"
    # The window deadline no longer re-hails…
    assert machine.tick(40 * S) == []
    # …and a go-ahead minutes later still delivers.
    outcome = _rx(machine, 300 * S, "obi, go for dana", go_ahead=GO)
    assert any(
        isinstance(e, TransmitEffect) and e.kind == "delivery" for e in outcome.effects
    )


def test_standby_that_never_resolves_expires_at_ttl() -> None:
    machine = _machine()
    _outbound(machine, ttl_s=600)
    _hail_ok(machine, 0)
    _rx(machine, 10 * S, "stand by one")
    effects = machine.tick(601 * S)
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1 and outcomes[0].outcome == "expired"
    assert machine.state == "idle"


def test_ttl_beats_the_retry_budget() -> None:
    machine = _machine(outbound_ack_window_s=30, outbound_max_rehails=2)
    _outbound(machine, ttl_s=45)  # TTL expires inside the first re-hail window
    _hail_ok(machine, 0)
    effects = machine.tick(46 * S)
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1 and outcomes[0].outcome == "expired"
    assert not any(isinstance(e, TransmitEffect) for e in effects)


def test_decline_closes_declined_with_the_heard_text() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 0)
    outcome = _rx(machine, 5 * S, "negative, tied up at the gate")
    outcomes = [e for e in outcome.effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1
    assert outcomes[0].outcome == "declined"
    assert "tied up at the gate" in (outcomes[0].detail or "")
    assert machine.state == "idle"


def test_garble_in_window_keeps_waiting() -> None:
    machine = _machine(outbound_ack_window_s=30)
    _outbound(machine)
    _hail_ok(machine, 0)
    outcome = _rx(machine, 5 * S, "", intelligible=False)
    assert outcome.effects == ()
    assert machine.state == "awaiting_go"
    # …and the say-again budget was NOT spent on the window.
    assert machine.say_again_used is False
    # The re-hail schedule is undisturbed.
    assert machine.tick(31 * S) == [TransmitEffect(kind="hail", text="Dana, Dana, obi")]


# --- US3: recognition amid real channel traffic --------------------------------------------


def test_go_ahead_for_another_station_keeps_waiting() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 0)
    # "go for Mike" parses as a hail-answer to someone else: addressed_elsewhere=True,
    # detector unmatched. The attempt neither delivers nor closes (O8/D4).
    outcome = _rx(machine, 5 * S, "go for mike", go_ahead=NO_GO, addressed_elsewhere=True)
    assert outcome.effects == ()
    assert outcome.trigger_reason_override == "third_party_traffic"
    assert machine.state == "awaiting_go"


def test_go_ahead_outranks_the_third_party_read_inside_the_window() -> None:
    # "go for Dana" answering OUR hail arrives with addressed_elsewhere=True (the 114
    # rule was written on 'obi never hails anyone') — inside our window the go-ahead
    # wins; exchange.py itself is untouched.
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 0)
    outcome = _rx(machine, 5 * S, "go for dana", go_ahead=GO, addressed_elsewhere=True)
    assert any(
        isinstance(e, TransmitEffect) and e.kind == "delivery" for e in outcome.effects
    )


def test_callee_hailing_back_is_the_go_ahead() -> None:
    machine = _machine()
    _outbound(machine, callee="dana")
    _hail_ok(machine, 0)
    outcome = _rx(
        machine, 5 * S, "obi obi dana", fired=True, caller="dana", caller_known=True,
        go_ahead=GoAheadDecision(True, "hail_back"),
    )
    assert any(
        isinstance(e, TransmitEffect) and e.kind == "delivery" for e in outcome.effects
    )
    assert machine.state == "delivering"


def test_foreign_named_hail_supersedes_and_requeues() -> None:
    machine = _machine()
    _outbound(machine, callee="Dana")
    _hail_ok(machine, 0)
    outcome = _rx(
        machine, 5 * S, "obi obi this is yoyo", fired=True, caller="yoyo", caller_known=True
    )
    effects = list(outcome.effects)
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1 and outcomes[0].outcome == "requeue"
    assert any(isinstance(e, CloseEffect) and e.closure == "superseded" for e in effects)
    assert OpenCallEffect(caller="yoyo", tier="safe") in effects
    assert any(isinstance(e, TransmitEffect) and e.kind == "ack" for e in effects)
    assert machine.direction == "inbound"  # the new call is an ordinary inbound one


def test_anonymous_hail_supersedes_and_requeues() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 0)
    outcome = _rx(machine, 5 * S, "obi obi herrar", fired=True, caller=None)
    effects = list(outcome.effects)
    outcomes = [e for e in effects if isinstance(e, OutboundOutcomeEffect)]
    assert len(outcomes) == 1 and outcomes[0].outcome == "requeue"
    assert any(isinstance(e, TransmitEffect) and e.kind == "ack" for e in effects)


def test_fired_hail_mid_delivery_is_busy_not_supersede() -> None:
    machine = _machine()
    _outbound(machine)
    _hail_ok(machine, 0)
    _rx(machine, 5 * S, "go", go_ahead=GoAheadDecision(True, "bare_go"))
    assert machine.state == "delivering"
    outcome = _rx(
        machine, 6 * S, "obi obi this is yoyo", fired=True, caller="yoyo", caller_known=True
    )
    assert outcome.effects == ()
    assert machine.state == "delivering"
    # The committed delivery still completes and settles.
    effects = machine.on_tx_result(
        8 * S, kind="delivery", disposition="transmitted", text="the link is up, over"
    )
    assert effects == [OutboundOutcomeEffect(outcome="delivered", attempts=0)]


def test_inbound_behavior_without_a_hail_pending_is_unchanged() -> None:
    # Control (O8): the loosened shapes exist only inside an outbound window. On an
    # idle machine, "go for dana" / bare "go" produce exactly nothing.
    machine = _machine()
    for text in ("go for dana", "go"):
        outcome = machine.on_transmission(
            0, fired=False, caller=None, text=text, intelligible=True
        )
        assert outcome.effects == ()
        assert machine.state == "idle"


def test_inbound_open_window_still_composes_a_bare_go() -> None:
    # Regression pin: pre-124, an intelligible non-clearing transmission in an inbound
    # open window composes (109) — a bare "go" included. That must not change.
    machine = _machine()
    _call(machine)
    machine.on_tx_result(1 * S, kind="ack", disposition="transmitted")
    machine.on_transmission(
        2 * S, fired=False, caller=None, text="status of the NOC", intelligible=True
    )
    machine.on_compose_result(3 * S, "NOC is green, over")
    machine.on_tx_result(4 * S, kind="answer", disposition="transmitted", text="NOC is green, over")
    outcome = machine.on_transmission(5 * S, fired=False, caller=None, text="go", intelligible=True)
    assert any(isinstance(e, ComposeEffect) for e in outcome.effects)
