"""Replay harness (feature 109, US5): real stored transcripts as regression fixtures.

Each ``tests/fixtures/radio_replay/*.json`` group replays transcript TEXT (no audio, no
radio.db) through ``trigger.detect`` + an :class:`ExchangeTracker` + a fresh
:class:`CallMachine`, mirroring the runner's decision plumbing: the recorded gate reason
is ``trigger_reason_override or decision.reason``, effects execute in order, and every
TransmitEffect settles immediately with a shadow-success disposition (protocol replay
abstracts the async TX gate away). The tracker is per-group and real, so a fixture's
third-party verdict comes from the wording rather than a hand-set flag (feature 114).
See specs/109-radio-reply-gate/contracts/replay-fixture-schema.md and
specs/114-radio-third-party-hail/contracts/addressing-verdict.md.

Fixture provenance: transcripts are copied verbatim from radio.db at authoring time and
carry their source row ids — a formerly-broken outcome asserted here IS the original
field incident, permanently pinned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from obi.radio.calls import (
    CallMachine,
    CloseEffect,
    ComposeEffect,
    OpenCallEffect,
    OutboundOutcomeEffect,
    TransmitEffect,
)
from obi.radio.exchange import ExchangeTracker, addressed_elsewhere
from obi.radio.outbound import detect_go_ahead
from obi.radio.trigger import detect

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "radio_replay"

S = 1_000_000  # µs per second

# Groups the corpus MUST contain (SC-001/SC-002, contract "Corpus obligations").
# Asserted by test_corpus_obligations once the corpus lands (T025).
REQUIRED_GROUPS = {
    "dropped_followups.json",
    "ignored_chatter.json",
    "dead_air.json",
    "mangled_hails.json",
    "invited_reply.json",
    "evening_2026_08_28.json",
    # Feature 114 (SC-001/SC-002): the 2026-08-29 third-party misfire.
    "third_party_2026_08_29.json",
    # Feature 116: the 2026-08-30 02:58 plain-ack miss on a short question hail.
    "short_requests.json",
    # Feature 124 (SC-002/SC-004/SC-005): obi speaks first — go-ahead recognition amid
    # traffic, and the bounded unanswered walk that never airs the payload.
    "outbound_go_ahead.json",
    "outbound_unanswered.json",
    # Feature 129 (SC-001): the 2026-09-02 call-227 incident — doubled hails folded
    # into one call until the budget-spending answer signed off on a question.
    "new_call_budget.json",
}


def _machine(config: dict[str, Any]) -> CallMachine:
    return CallMachine(
        ack_text="go for obi",
        say_again_text="ten nine, say again",
        unavailable_text="unable to comply, obi out",
        garble_close_text="unable to copy, out",
        call_timeout_s=int(config.get("call_timeout_s", 45)),
        compose_deadline_s=20,
        operator_handles=list(config.get("operator_handles", [])),
        sign_offs=list(config.get("sign_offs", [])),
        standby_text="stand by",
        # Replay settles composes synchronously — a mid-replay standby would inject
        # nondeterministic tx rows into expected effect streams.
        standby_after_s=3600,
        # Feature 124: the outbound walk's knobs, fixture-controlled.
        outbound_ack_window_s=int(config.get("outbound_ack_window_s", 30)),
        outbound_max_rehails=int(config.get("outbound_max_rehails", 2)),
    )


def _note(effect: object) -> str:
    if isinstance(effect, OpenCallEffect):
        return "open_call"
    if isinstance(effect, TransmitEffect):
        return f"tx:{effect.kind}"
    if isinstance(effect, ComposeEffect):
        return "compose"
    if isinstance(effect, CloseEffect):
        return f"close:{effect.closure}"
    if isinstance(effect, OutboundOutcomeEffect):
        return f"outcome:{effect.outcome}"
    raise AssertionError(f"unknown effect type: {effect!r}")


def _run_effects(
    machine: CallMachine,
    now_us: int,
    effects: list[object],
    compose_result: Any,
    notes: list[str],
) -> None:
    """Execute an effect stream the way the runner would, settling TX synchronously.

    ``compose_result`` scripts the composer for THIS transmission's compose (a dict per
    the fixture schema, ``None`` for compose failure, or absent ⇒ a generic answer).
    """

    queue = list(effects)
    while queue:
        effect = queue.pop(0)
        notes.append(_note(effect))
        if isinstance(effect, TransmitEffect):
            queue.extend(
                machine.on_tx_result(
                    now_us, kind=effect.kind, disposition="shadow_logged", text=effect.text
                )
            )
        elif isinstance(effect, ComposeEffect):
            if isinstance(compose_result, dict) and compose_result.get("defer"):
                # The compose stays in flight — a later "settle_compose" entry delivers
                # its result (needed to replay mid-compose incidents like call 47).
                continue
            expects = False
            if compose_result is None:
                answer_text: str | None = None
            elif isinstance(compose_result, dict):
                answer_text = str(compose_result.get("text", "copy, over."))
                expects = bool(compose_result.get("expects_reply", False))
            else:
                answer_text = "copy, over."
            queue.extend(
                machine.on_compose_result(now_us, answer_text, expects_reply=expects)
            )


def _replay_group(group: dict[str, Any]) -> None:
    config = group.get("config", {})
    machine = _machine(config)
    known_callers = [
        *config.get("operator_handles", []),
        *config.get("known_callers", []),
    ]
    name_variants = config.get(
        "name_variants", ["obi", "obie", "obee", "o b", "opie", "hobi"]
    )
    # Feature 114: one tracker per group, exactly as the runner keeps one per process, so
    # fixtures exercise the real addressing recognition instead of a hand-set boolean.
    tracker = ExchangeTracker(
        roster=known_callers,
        aliases=config.get("caller_aliases", {}),
        name_variants=name_variants,
    )
    outbound = group.get("outbound")
    if outbound:
        # Feature 124 prologue: obi speaks first. start_outbound runs before the lines,
        # its hail settling shadow-successfully exactly like every other TransmitEffect,
        # so the replay opens with the ack window armed.
        notes: list[str] = []
        _run_effects(
            machine,
            0,
            list(
                machine.start_outbound(
                    0,
                    callee=str(outbound["callee"]),
                    message=str(outbound["message"]),
                    expires_us=int(outbound.get("ttl_s", 600)) * S,
                )
            ),
            None,
            notes,
        )
        assert notes == ["open_call", "tx:hail"], f"outbound prologue: {notes}"
        assert machine.state == "awaiting_go"
    for i, tx in enumerate(group["transmissions"]):
        label = f"tx[{i}] ({(tx.get('source') or tx.get('text', ''))[:60]!r})"
        now_us = int(float(tx.get("at_s", i * 10)) * S)

        if tx.get("tick"):
            # A pure time-passage entry (124): deadline effects — re-hails, unanswered
            # closes — are asserted like any other effect stream.
            notes = []
            _run_effects(machine, now_us, list(machine.tick(now_us)), None, notes)
            expected = [e for e in tx.get("expect", {}).get("effects", []) if e != "none"]
            if "effects" in tx.get("expect", {}):
                assert notes == expected, f"{label}: effects {notes} != {expected}"
            continue

        # Time passes: expire deadlines exactly as the runner's tick loop would. Tick
        # effects (timeout closes, deferred garble closes) are executed for state
        # correctness but not asserted per-transmission — fixtures assert responses.
        _run_effects(machine, now_us, list(machine.tick(now_us)), None, notes=[])

        if "settle_compose" in tx:
            # A pure event entry: the deferred compose's result arrives now. Cascaded
            # effects (answer TX, a drained held request) are asserted like any other.
            settle = tx["settle_compose"]
            answer = None if settle is None else str(settle.get("text", "copy, over."))
            expects = bool(settle.get("expects_reply", False)) if settle else False
            notes = []
            _run_effects(
                machine,
                now_us,
                list(machine.on_compose_result(now_us, answer, expects_reply=expects)),
                "default",
                notes,
            )
            expected = [e for e in tx.get("expect", {}).get("effects", []) if e != "none"]
            if "effects" in tx.get("expect", {}):
                assert notes == expected, f"{label}: effects {notes} != {expected}"
            continue

        intelligible = bool(tx.get("intelligible", True))
        decision = detect(
            tx["text"],
            variants=name_variants,
            variants_weak=config.get("name_variants_weak", []),
            intelligible=intelligible,
            known_callers=known_callers,
            caller_aliases=config.get("caller_aliases", {}),
        )
        attribution = tracker.observe(
            started_us=now_us,
            ended_us=now_us + 3 * S,
            text=tx["text"],
            intelligible=intelligible,
        )
        # Feature 124: the runner computes the go-ahead read only while an outbound ack
        # window is open (contract O8) — mirrored here so fixtures exercise the real
        # scoping, not a hand-set flag.
        go_ahead = None
        outbound_callee = machine.outbound_callee()
        if outbound_callee is not None and intelligible:
            go_ahead = detect_go_ahead(
                tx["text"],
                callee=outbound_callee,
                name_variants=name_variants,
                aliases=config.get("caller_aliases", {}),
            )
        outcome = machine.on_transmission(
            now_us,
            fired=decision.fired,
            caller=decision.caller,
            text=tx["text"],
            intelligible=intelligible,
            caller_known=decision.caller_known,
            request_tail=decision.request_tail,
            doubled=decision.doubled,
            addressed_elsewhere=addressed_elsewhere(attribution, name_variants),
            go_ahead=go_ahead,
        )
        recorded_reason = outcome.trigger_reason_override or decision.reason

        notes: list[str] = []
        _run_effects(
            machine, now_us, list(outcome.effects), tx.get("compose_result", "default"), notes
        )

        expect = tx.get("expect", {})
        if "fired" in expect:
            assert decision.fired == expect["fired"], f"{label}: fired {decision.fired}"
        if "reason" in expect:
            assert recorded_reason == expect["reason"], (
                f"{label}: reason {recorded_reason!r} != {expect['reason']!r}"
            )
        if "caller" in expect:
            assert decision.caller == expect["caller"], f"{label}: caller {decision.caller!r}"
        if "effects" in expect:
            expected = [e for e in expect["effects"] if e != "none"]
            assert notes == expected, f"{label}: effects {notes} != {expected}"


def _group_files() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize(
    "path", _group_files(), ids=lambda p: p.name if isinstance(p, Path) else str(p)
)
def test_replay_group(path: Path) -> None:
    group = json.loads(path.read_text())
    _replay_group(group)


def test_replay_fixture_dir_exists() -> None:
    # The corpus directory is part of the harness even while empty (T003 lands before
    # the story fixtures); its absence means a broken checkout, not an empty corpus.
    assert FIXTURES_DIR.is_dir()


def test_corpus_obligations() -> None:
    """SC-001/SC-002 stay pinned (contract 'Corpus obligations'): the committed corpus
    holds every documented incident class, so a future gate change cannot silently
    shrink the evidence it is tested against."""

    present = {p.name for p in _group_files()}
    missing = REQUIRED_GROUPS - present
    assert not missing, f"corpus groups missing: {sorted(missing)}"

    def load(name: str) -> list[dict[str, Any]]:
        return json.loads((FIXTURES_DIR / name).read_text())["transmissions"]

    drops = [
        t
        for t in load("dropped_followups.json")
        if t.get("expect", {}).get("reason") == "in_window_composed"
    ]
    assert len(drops) >= 12, "the 12 mined dropped follow-ups must all be pinned"

    dead_air = load("dead_air.json")
    assert sum(1 for t in dead_air if "radio.db tx" in t.get("source", "")) >= 3

    hails = [
        t for t in load("mangled_hails.json") if t.get("expect", {}).get("fired") is True
    ]
    assert len(hails) >= 4, "all four mangled hails must open calls"

    ignored = [
        t for t in load("ignored_chatter.json") if t.get("expect", {}).get("effects") == []
    ]
    assert len(ignored) >= 8, "the must-stay-ignored set anchors the false-positive rate"

    evening = load("evening_2026_08_28.json")
    assert len(evening) >= 20, "the end-to-end evening timeline must stay intact"

    # Feature 114 SC-001: BOTH transmissions obi answered out of the Mira↔Vertigo
    # exchange stay pinned — the hail as an explicit third-party close, its answer as
    # traffic that finds no window open at all. Neither may ever transmit again.
    third_party = load("third_party_2026_08_29.json")
    sourced = [t for t in third_party if "radio.db tx 32" in t.get("source", "")]
    assert len(sourced) >= 4, "the incident's own rows must all carry their source ids"
    silenced = [
        t
        for t in third_party
        if t.get("expect", {}).get("reason") == "third_party_traffic"
    ]
    assert silenced, "the hail Mira sent to Vertigo must stay pinned as suppressed"
    assert all(
        not any(str(e).startswith("tx:") for e in t.get("expect", {}).get("effects", []))
        for t in third_party
        if t.get("expect", {}).get("reason") == "third_party_traffic"
    ), "third-party traffic may never produce a transmission"

    # SC-002: Mira's own hail into a caller-less call must still open a call and ack.
    supersede = [
        t
        for t in third_party
        if "close:superseded" in t.get("expect", {}).get("effects", [])
        and t.get("expect", {}).get("caller") == "mira"
    ]
    assert supersede, "the anonymous-call supersede (tx 3233) must stay pinned"
