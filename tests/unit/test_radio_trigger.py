"""Unit (feature 088, US2): deterministic directed-call detection (FR-007).

Table-driven over transcript shapes. No model in the trigger path — this is pure text
logic, and every decision carries the reason recorded for shadow-stage tuning.
"""

from __future__ import annotations

import pytest

from obi.radio.trigger import TriggerDecision, detect

VARIANTS = ["obi", "obie", "obee", "o b", "opie"]


def _detect(text: str, *, intelligible: bool = True) -> TriggerDecision:
    return detect(text, variants=VARIANTS, intelligible=intelligible)


@pytest.mark.parametrize(
    ("text", "caller"),
    [
        ("Obi, Obi, this is nono", "nono"),
        ("obi this is nono", "nono"),
        ("Obie obie this is yoyo", "yoyo"),
        ("O B this is nono", "nono"),
        ("obi obi obi this is nono", "nono"),  # tripled call, playa enthusiasm
        ("uh obi this is nono", "nono"),  # one leading filler token tolerated
        ("obi this is net control", "net"),  # unknown handle: first token only (2026-08-19)
        ("Obi, radio check", None),  # name in call position, no handle stated
        ("obi obi", None),
    ],
)
def test_directed_calls_fire(text: str, caller: str | None) -> None:
    decision = _detect(text)
    assert decision.fired, text
    assert decision.reason == "name_at_start"
    assert decision.caller == caller


@pytest.mark.parametrize(
    ("text", "doubled"),
    [
        ("obi obi this is nono", True),
        ("obi obi", True),
        ("obi obi obi this is nono", True),  # tripled call, playa enthusiasm
        ("Obi, Obi, revert the tower to the previous state", True),  # call 187, no callsign
        ("uh obi obi sawdust", True),  # one leading filler token tolerated
        ("obi this is nono", False),
        ("obi radio check", False),
        ("Obi, what is the utilization of the primary link?", False),  # mid-call re-fire
    ],
)
def test_doubled_hail_is_reported(text: str, doubled: bool) -> None:
    # The REPEATED name is this channel's formal new-call opener; a single name mid-call
    # is a continuation. The call machine needs the distinction to tell new-call intent
    # from a follow-up once the answer budget is spent (field 2026-09-01, call 187) — the
    # consumption loop used to swallow the repeat and discard the fact.
    decision = detect(text, variants=VARIANTS, known_callers=ROSTER)
    assert decision.fired, text
    assert decision.doubled is doubled, text


def test_weak_variant_hail_is_doubled_by_construction() -> None:
    # A weak variant only opens a hail in the doubled shape (feature 109), so the flag is
    # always set there — the second name is consumed as a name, never read as the handle.
    decision = detect(
        "ovi ovi mira", variants=VARIANTS, variants_weak=["ovi"], known_callers=ROSTER
    )
    assert decision.fired and decision.doubled and decision.caller_known


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("tell obi later", "mid_sentence_mention"),
        ("can you ask obi about the link", "mid_sentence_mention"),
        ("the mountain link is fine", "no_name"),
        ("", "no_name"),
        ("toby this is nono", "no_name"),  # not a configured variant
        ("mobius this is nono", "no_name"),  # substring of a token is not a match
    ],
)
def test_non_calls_do_not_fire(text: str, reason: str) -> None:
    decision = _detect(text)
    assert not decision.fired, text
    assert decision.reason == reason
    assert decision.caller is None


def test_unintelligible_never_fires_even_with_a_perfect_call() -> None:
    decision = _detect("obi obi this is nono", intelligible=False)
    assert not decision.fired
    assert decision.reason == "unintelligible"


def test_custom_variants_are_respected() -> None:
    decision = detect("toby this is nono", variants=["toby"], intelligible=True)
    assert decision.fired and decision.caller == "nono"


def test_punctuation_and_case_are_normalized() -> None:
    decision = _detect("OBI!  Obi... THIS IS NoNo.")
    assert decision.fired and decision.caller == "nono"


# --- fuzzy caller matching against the channel roster (layer 2, field lesson 2026-08-19) ---

ROSTER = ["Mira", "Cat", "Sawdust", "Za-zu", "Peter", "ZFX", "Tabe", "Winter"]


def test_match_caller_exact_and_squeezed() -> None:
    from obi.radio.trigger import match_caller

    assert match_caller("mira", ROSTER) == "mira"
    assert match_caller("za zu", ROSTER) == "za-zu"  # squeezed forms unify punctuation
    assert match_caller("z f x", ROSTER) == "zfx"  # initialisms transcribed as letters


def test_match_caller_near_miss() -> None:
    from obi.radio.trigger import match_caller

    assert match_caller("mara", ROSTER) == "mira"
    assert match_caller("saw dust", ROSTER) == "sawdust"


def test_match_caller_rejects_distant_and_empty() -> None:
    from obi.radio.trigger import match_caller

    assert match_caller("shabar", ROSTER) is None  # pre-prompt whisper guess: too far
    assert match_caller("radio check", ROSTER) is None
    assert match_caller("", ROSTER) is None
    assert match_caller("mira", []) is None


def test_detect_canonicalizes_this_is_caller() -> None:
    decision = detect(
        "obi obi this is mara", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller == "mira"


def test_detect_keeps_raw_caller_when_roster_has_no_match() -> None:
    decision = detect(
        "obi obi this is net control", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    # Unknown handles collapse to the first token — never a greedy grab, and a short
    # unmatched tail ("control") is never treated as a request.
    assert decision.fired and decision.caller == "net" and decision.request_tail is None


def test_detect_bare_roster_handle_after_name() -> None:
    # "obi obi sawdust" — the caller states the handle without "this is"; the roster
    # disambiguates a handle from the start of a request.
    decision = detect(
        "obi obi sawdust", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller == "sawdust"


def test_detect_bare_request_is_not_stolen_as_a_handle() -> None:
    decision = detect(
        "obi radio check", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller is None


# --- the live call forms (operator direction 2026-08-19) + personalized-ack certainty ------


def test_single_name_bare_handle_form() -> None:
    decision = detect("obi mira", variants=VARIANTS, intelligible=True, known_callers=ROSTER)
    assert decision.fired and decision.caller == "mira" and decision.caller_known


def test_doubled_name_bare_handle_form() -> None:
    decision = detect("obi obi mira", variants=VARIANTS, intelligible=True, known_callers=ROSTER)
    assert decision.fired and decision.caller == "mira" and decision.caller_known


def test_filler_this_is_form() -> None:
    decision = detect(
        "hey obi this is mira", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller == "mira" and decision.caller_known


def test_anonymous_call_form() -> None:
    decision = detect("obi obi", variants=VARIANTS, intelligible=True, known_callers=ROSTER)
    assert decision.fired and decision.caller is None and not decision.caller_known


def test_unmatched_this_is_handle_is_not_known() -> None:
    # Raw handle kept, but caller_known stays False — the ack must never read a
    # mis-transcription back at the channel.
    decision = detect(
        "obi obi this is net control", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller == "net" and not decision.caller_known


# --- one-breath calls + non-greedy handle extraction (field, 2026-08-19 evening) -----------


def test_this_is_takes_first_token_not_the_request() -> None:
    decision = detect(
        "obi obi this is mira what's the uptime of the noc gateway",
        variants=VARIANTS, intelligible=True, known_callers=ROSTER,
    )
    assert decision.caller == "mira" and decision.caller_known
    assert decision.request_tail == "what's the uptime of the noc gateway"


def test_this_is_prefers_the_longest_roster_prefix() -> None:
    decision = detect(
        "obi this is ash actual radio check",
        variants=VARIANTS, intelligible=True,
        known_callers=[*ROSTER, "Ash actual", "Not ash"],
    )
    assert decision.caller == "ash actual" and decision.caller_known
    assert decision.request_tail == "radio check"


def test_unmatched_first_token_is_the_raw_caller() -> None:
    decision = detect(
        "obi this is zorp radio check", variants=VARIANTS, intelligible=True,
        known_callers=ROSTER,
    )
    assert decision.caller == "zorp" and not decision.caller_known
    # A 2-token tail from an UNKNOWN caller is not trusted as a request — obi acks and
    # waits (fail toward silence); a roster-identified caller gets any-length tails.
    assert decision.request_tail is None


def test_long_bare_remainder_is_the_request_itself() -> None:
    decision = detect(
        "obi what's the uptime of the noc gateway",
        variants=VARIANTS, intelligible=True, known_callers=ROSTER,
    )
    assert decision.caller is None
    assert decision.request_tail == "what's the uptime of the noc gateway"


def test_short_mangled_remainder_is_never_a_request() -> None:
    # "obi obi herrar" — a mis-heard callsign: ack plainly, wait, never compose to it.
    decision = detect(
        "obi obi herrar", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller is None and decision.request_tail is None


# --- short request-opener remainders (feature 116, field 2026-08-30 call 146) --------------


@pytest.mark.parametrize(
    ("text", "tail"),
    [
        # The 02:58 miss: 3 tokens, no roster match, opens with a question word.
        ("Obi, Obi, where is Tooma?", "where is tooma"),
        ("obi obi where's tooma", "where's tooma"),
        # Imperative openers are requests too (operator direction 2026-08-30).
        ("Obi, Obi, set portal red.", "set portal red"),
        ("obi obi check noc uptime", "check noc uptime"),
    ],
)
def test_short_request_opener_remainder_is_the_request(text: str, tail: str) -> None:
    decision = detect(text, variants=VARIANTS, intelligible=True, known_callers=ROSTER)
    assert decision.fired and decision.caller is None and not decision.caller_known
    assert decision.request_tail == tail


def test_roster_near_miss_outranks_the_opener_heuristic() -> None:
    # "Will" is both a question opener and a near-miss of roster callsign "Jill" —
    # identification wins: personalized ack and wait, never compose to a callsign.
    decision = detect(
        "obi obi will", variants=VARIANTS, intelligible=True, known_callers=[*ROSTER, "Jill"]
    )
    assert (decision.caller, decision.caller_known) == ("jill", True)
    assert decision.request_tail is None


def test_unknown_caller_short_interrogative_tail_composes() -> None:
    # Form-A counterpart: an unknown caller's short tail is trusted when it opens with
    # a request word ("this is net control" stays untrusted — "control" is no opener).
    decision = detect(
        "obi this is bob where's tooma",
        variants=VARIANTS, intelligible=True, known_callers=ROSTER,
    )
    assert decision.caller == "bob" and not decision.caller_known
    assert decision.request_tail == "where's tooma"


def test_caller_alias_identifies_a_known_mis_hearing() -> None:
    # Field 2026-08-20: "Obi, Obi, Seven." — "seven" scores 0.67 against Mira, below
    # the fuzzy threshold; the operator-curated alias makes it a certain identification.
    aliases = {"seven": "Mira"}
    decision = detect(
        "obi obi seven",
        variants=VARIANTS, intelligible=True, known_callers=ROSTER, caller_aliases=aliases,
    )
    assert (decision.caller, decision.caller_known) == ("mira", True)
    # Same alias in the "this is" form, with a one-breath request tail.
    decision = detect(
        "obi obi this is seven what's the status of the noc gateway",
        variants=VARIANTS, intelligible=True, known_callers=ROSTER, caller_aliases=aliases,
    )
    assert (decision.caller, decision.caller_known) == ("mira", True)
    assert decision.request_tail == "what's the status of the noc gateway"
    # The alias applies in callsign position ONLY — "seven" inside a request survives.
    decision = detect(
        "obi obi this is cat is port seven up",
        variants=VARIANTS, intelligible=True, known_callers=ROSTER, caller_aliases=aliases,
    )
    assert (decision.caller, decision.caller_known) == ("cat", True)
    assert decision.request_tail == "is port seven up"
    # Without the alias the mangled callsign stays unidentified (plain ack, wait).
    decision = detect(
        "obi obi seven", variants=VARIANTS, intelligible=True, known_callers=ROSTER
    )
    assert decision.fired and decision.caller is None


def test_repetition_blob_without_spaces_is_hallucination() -> None:
    from obi.radio.runner import is_repetition_hallucination

    assert is_repetition_hallucination("adoado" * 40)
    assert is_repetition_hallucination("BeepBeepBeepBeepBeepBeep")
    assert not is_repetition_hallucination("what's the uptime of the noc gateway")
    assert not is_repetition_hallucination("radio check")
    assert not is_repetition_hallucination("papa")  # short real word, too short to loop
    # Field 2026-08-20 15:27: a 446-char Cyrillic "окок…" blob passed the old
    # ASCII-only squeeze (it stripped every letter, leaving nothing to test).
    assert is_repetition_hallucination("ок" * 223)
    assert is_repetition_hallucination("...!!!...")  # punctuation soup is never traffic
    assert not is_repetition_hallucination("статус")  # non-Latin real word, no loop


def test_heard_handle_extracts_the_raw_callsign() -> None:
    from obi.radio.trigger import heard_handle

    # Bare hail forms — the field mis-hears verbatim.
    assert heard_handle("Obi, Obi, sir.", variants=VARIANTS) == "sir"
    assert heard_handle("uh obi obi hever", variants=VARIANTS) == "hever"
    # "this is" form: the first token after it.
    assert heard_handle("obi obi this is shabar what's up", variants=VARIANTS) == "shabar"
    # A long bare remainder is a one-breath request, not a handle.
    assert heard_handle("obi what's the uptime of the noc gateway", variants=VARIANTS) is None
    # A short request-opener remainder is a request too (116) — never an alias candidate.
    assert heard_handle("Obi, Obi, where is Tooma?", variants=VARIANTS) is None
    assert heard_handle("obi obi set portal red", variants=VARIANTS) is None
    # Not a call at all.
    assert heard_handle("tell obi later", variants=VARIANTS) is None


# --- feature 109 US3: transcription tolerance -----------------------------------------

WEAK = ["ovi", "obini", "okie"]


def _detect_weak(text: str, **kwargs: object) -> TriggerDecision:
    return detect(text, variants=VARIANTS, variants_weak=WEAK, intelligible=True, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "caller"),
    [
        ("Ovi, Ovi, Vertigo.", "vertigo"),  # radio.db tx 2702
        ("Obini, Obini, Sabra.", None),  # radio.db tx 1690 — sabra not in roster
        ("Okie, Okie, Krum.", "krum"),  # radio.db tx 2833
    ],
)
def test_weak_variants_fire_on_doubled_hails(text: str, caller: str | None) -> None:
    decision = _detect_weak(text, known_callers=["Vertigo", "Krum"])
    assert decision.fired, text
    assert decision.reason == "name_at_start"
    assert decision.caller == caller


@pytest.mark.parametrize(
    "text",
    [
        # the collision the doubled gate exists for
        "Okie dokie, heading over to the gate now folks.",
        "Okie, set the portal blue.",  # single weak mention is NOT a hail
        "Ovi can you hear me",
    ],
)
def test_single_weak_mention_never_fires(text: str) -> None:
    decision = _detect_weak(text, known_callers=["Krum"])
    assert not decision.fired, text
    assert decision.reason == "no_name"


def test_fuzzy_near_miss_fires_when_doubled() -> None:
    # No explicit weak entry needed: "obbie" scores 0.89 against strong variant "obie".
    decision = detect(
        "Obbie, Obbie, this is nono.", variants=VARIANTS, variants_weak=[], intelligible=True
    )
    assert decision.fired and decision.caller == "nono"


def test_fuzzy_far_word_does_not_fire_even_doubled() -> None:
    # "toby" is below the 0.75 bar against every strong variant — doubling is not a
    # loophole for arbitrary words.
    decision = detect(
        "Toby, Toby, this is nono.", variants=VARIANTS, variants_weak=[], intelligible=True
    )
    assert not decision.fired


def test_leading_junk_token_with_known_caller_fires() -> None:
    # radio.db tx 2585: "Beep, Obi, Kroom." — a leading noise token demoted a real hail
    # to mid_sentence_mention. Any single leading token is tolerated when the remainder
    # identifies a known caller (or uses "this is").
    decision = _detect_weak(
        "Beep, Obi, Kroom.", known_callers=["Krum"], caller_aliases={"kroom": "Krum"}
    )
    assert decision.fired and decision.caller == "krum" and decision.caller_known


def test_leading_junk_with_this_is_form_fires() -> None:
    decision = _detect_weak("Beep, Obi, this is stranger.")
    assert decision.fired and decision.caller == "stranger" and not decision.caller_known


def test_junk_guard_refuses_sentences_about_obi() -> None:
    # "tell obi later" has the name at index 1 too — the guard keeps it a mention, even
    # with a roster configured (the remainder identifies nobody).
    for text in ("tell obi later", "tell obi about the link"):
        decision = _detect_weak(text, known_callers=["Krum"])
        assert not decision.fired, text
        assert decision.reason == "mid_sentence_mention"


def test_weak_never_counts_as_a_mid_sentence_mention() -> None:
    decision = _detect_weak("that okie dokie attitude is great")
    assert not decision.fired
    assert decision.reason == "no_name"


def test_filler_leading_token_still_works_without_roster() -> None:
    # The pre-109 filler tolerance is unchanged: no roster needed for "uh obi …".
    decision = _detect_weak("uh obi this is nono")
    assert decision.fired and decision.caller == "nono"
