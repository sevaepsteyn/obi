"""Unit (feature 088): passive speaker attribution for third-party exchanges.

The tracker watches the channel's own etiquette: "go for Y" and "this is Y" name the
speaker outright (high confidence); a hail "X, X, Y" names both parties; between
patterns, sides of an established exchange alternate (low confidence). Pure and
clock-free — timestamps ride in with each observation.
"""

from __future__ import annotations

from obi.radio.exchange import (
    FORM_ALTERNATION,
    FORM_ANSWER,
    FORM_HAIL,
    FORM_SELF_ID,
    Attribution,
    ExchangeTracker,
    addressed_elsewhere,
)

# The roster carries the callsigns as actually SPOKEN — the field hail was
# "IT support, IT support, Waterworks", so both "IT" and "IT Support" are entries
# (exact short matches always outscore fuzzy long ones, so they never collide).
ROSTER = ["IT", "IT Support", "Waterworks", "Mira", "Cat", "Sawdust"]
S = 1_000_000


def _tracker() -> ExchangeTracker:
    return ExchangeTracker(roster=ROSTER)


def _obs(
    tracker: ExchangeTracker,
    text: str,
    *,
    start_s: float,
    dur_s: float = 3.0,
    intelligible: bool = True,
) -> Attribution:
    return tracker.observe(
        started_us=int(start_s * S),
        ended_us=int((start_s + dur_s) * S),
        text=text,
        intelligible=intelligible,
    )


def test_go_for_names_the_speaker() -> None:
    attribution = _obs(_tracker(), "waterworks go for it", start_s=0)
    assert attribution == Attribution(
        speaker="it", confidence="high", addressee="waterworks", form=FORM_ANSWER
    )


def test_go_for_precedes_the_hail_rule() -> None:
    # "IT, go for waterworks" must never be parsed as a hail from IT.
    attribution = _obs(_tracker(), "it go for waterworks", start_s=0)
    assert attribution == Attribution(
        speaker="waterworks", confidence="high", addressee="it", form=FORM_ANSWER
    )


def test_this_is_names_the_speaker() -> None:
    # Nobody is addressed, so this is a bare self-identification — never a directed form.
    attribution = _obs(_tracker(), "yeah this is sawdust we're on our way", start_s=0)
    assert attribution == Attribution(
        speaker="sawdust", confidence="high", addressee=None, form=FORM_SELF_ID
    )


def test_hail_names_the_caller() -> None:
    # The field hail verbatim: multi-word hailed name, repeated, then the caller.
    attribution = _obs(_tracker(), "it support it support waterworks", start_s=0)
    assert attribution == Attribution(
        speaker="waterworks", confidence="high", addressee="it support", form=FORM_HAIL
    )


def test_simple_hail_names_the_caller() -> None:
    attribution = _obs(_tracker(), "mira mira cat", start_s=0)
    assert attribution == Attribution(
        speaker="cat", confidence="high", addressee="mira", form=FORM_HAIL
    )


def test_fuzzy_roster_canonicalization() -> None:
    attribution = _obs(_tracker(), "water works go for it", start_s=0)
    assert attribution.speaker == "it"
    assert attribution.addressee == "waterworks"
    # A leading name before "this is" IS the hailed station (feature 114) — the marker
    # says who is speaking, the name in front of it says who they are speaking to.
    attribution2 = _obs(_tracker(), "it it this is water works", start_s=0)
    assert attribution2 == Attribution(
        speaker="waterworks", confidence="high", addressee="it", form=FORM_HAIL
    )


def test_unmatched_handle_is_raw_and_low() -> None:
    attribution = _obs(_tracker(), "go for zorp", start_s=0)
    assert attribution == Attribution(
        speaker="zorp", confidence="low", addressee=None, form=FORM_ANSWER
    )


def test_alternation_inside_the_window() -> None:
    tracker = _tracker()
    _obs(tracker, "it support it support waterworks", start_s=0)  # waterworks spoke
    _obs(tracker, "waterworks go for it", start_s=5)  # it spoke
    a3 = _obs(tracker, "hi there we have some keyboards", start_s=10)
    assert a3 == Attribution(speaker="waterworks", confidence="low", form=FORM_ALTERNATION)
    a4 = _obs(tracker, "copy that come on over", start_s=16)
    assert a4 == Attribution(speaker="it", confidence="low", form=FORM_ALTERNATION)


def test_alternation_expires_outside_the_window() -> None:
    tracker = _tracker()
    _obs(tracker, "it support it support waterworks", start_s=0)
    _obs(tracker, "waterworks go for it", start_s=5)
    stale = _obs(tracker, "hello anyone there", start_s=70)  # > 45 s later
    assert stale == Attribution(speaker=None, confidence=None)


def test_alternation_needs_two_established_parties() -> None:
    tracker = _tracker()
    _obs(tracker, "this is cat", start_s=0)  # one party known, no counterpart
    following = _obs(tracker, "some more words", start_s=4)
    assert following == Attribution(speaker=None, confidence=None)


def test_unintelligible_gets_alternation_only() -> None:
    tracker = _tracker()
    _obs(tracker, "it support it support waterworks", start_s=0)
    _obs(tracker, "waterworks go for it", start_s=5)
    garbled = _obs(tracker, "asc asc asc", start_s=10, intelligible=False)
    assert garbled == Attribution(
        speaker="waterworks", confidence="low", form=FORM_ALTERNATION
    )
    # …but a pattern inside unintelligible text is never trusted:
    tracker2 = _tracker()
    pattern = _obs(tracker2, "this is cat", start_s=0, intelligible=False)
    assert pattern == Attribution(speaker=None, confidence=None)


def test_high_confidence_outsider_reanchors_the_exchange() -> None:
    tracker = _tracker()
    _obs(tracker, "it support it support waterworks", start_s=0)
    _obs(tracker, "waterworks go for it", start_s=5)
    _obs(tracker, "mira mira cat", start_s=12)  # a new pair takes the channel
    following = _obs(tracker, "go ahead", start_s=16)
    assert following == Attribution(  # cat's counterpart
        speaker="mira", confidence="low", form=FORM_ALTERNATION
    )


def test_double_key_same_speaker_is_marked_low() -> None:
    # The honesty of the confidence field: alternation can be wrong when one side keys
    # twice in a row — which is exactly why it is 'low', never 'high'.
    tracker = _tracker()
    _obs(tracker, "it support it support waterworks", start_s=0)
    _obs(tracker, "waterworks go for it", start_s=5)
    misattributed = _obs(tracker, "sorry say again the last part", start_s=10)
    assert misattributed.confidence == "low"


def test_cold_start_has_no_alternation() -> None:
    tracker = _tracker()
    first = _obs(tracker, "well that was fun", start_s=0)
    assert first == Attribution(speaker=None, confidence=None)
    anchored = _obs(tracker, "this is mira", start_s=3)
    assert anchored.speaker == "mira" and anchored.confidence == "high"


def test_caller_alias_canonicalizes_a_named_speaker() -> None:
    # Field 2026-08-20: "seven" is a known mis-hearing of Mira — an operator-curated
    # alias identifies it in callsign position with pattern-rule (high) confidence.
    tracker = ExchangeTracker(roster=ROSTER, aliases={"seven": "Mira"})
    att = _obs(tracker, "this is seven with traffic for the noc", start_s=0.0)
    assert (att.speaker, att.confidence) == ("mira", "high")


# -- feature 114: is this traffic for somebody else? -------------------------------------

VARIANTS = ["obi", "obie", "obee", "o b", "opie", "hobi"]


def _verdict(text: str, *, roster: list[str] | None = None) -> Attribution:
    tracker = ExchangeTracker(
        roster=ROSTER if roster is None else roster, name_variants=VARIANTS
    )
    return tracker.observe(started_us=0, ended_us=3 * S, text=text, intelligible=True)


def _elsewhere(text: str, *, roster: list[str] | None = None) -> bool:
    return addressed_elsewhere(_verdict(text, roster=roster), VARIANTS)


def test_third_party_hail_and_answer_are_addressed_elsewhere() -> None:
    # The two field misfires, in the shapes the channel actually produces.
    assert _elsewhere("vertigo vertigo vertigo this is mira")
    assert _elsewhere("go for vertigo")
    assert _elsewhere("mira mira cat")


def test_a_hail_to_obi_is_never_addressed_elsewhere() -> None:
    # FR-006. obi's own hail must survive the addressing parser untouched.
    assert _verdict("obi obi mira").addressee == "obi"
    assert not _elsewhere("obi obi mira")
    assert not _elsewhere("obi obi this is mira")


def test_an_answer_naming_obi_is_never_addressed_elsewhere() -> None:
    # FR-007's two exits: obi as the station answering, and obi as the station answered.
    assert not _elsewhere("go for obi")
    assert not _elsewhere("obi go for waterworks")


def test_self_identification_and_alternation_never_suppress() -> None:
    # FR-016: "this is Mira, set the portal to red" is a caller talking TO obi. And an
    # alternation guess carries no addressing evidence at all, only a name.
    assert not _elsewhere("this is mira set the portal to red")
    tracker = ExchangeTracker(roster=ROSTER, name_variants=VARIANTS)
    tracker.observe(started_us=0, ended_us=3 * S, text="it it waterworks", intelligible=True)
    tracker.observe(
        started_us=5 * S, ended_us=8 * S, text="waterworks go for it", intelligible=True
    )
    guess = tracker.observe(
        started_us=10 * S, ended_us=13 * S, text="we have some keyboards", intelligible=True
    )
    assert guess.form == FORM_ALTERNATION
    assert not addressed_elsewhere(guess, VARIANTS)


def test_a_passing_mention_of_another_station_still_composes() -> None:
    # FR-005: only the hail and answer FORMS suppress. Naming a station mid-request does
    # not — the widened 109 gate must keep answering these.
    assert not _elsewhere("set the portal to the same color as vertigo did")
    assert not _elsewhere("what is the status of the link to sawdust")


def test_go_for_it_is_an_idiom_unless_it_is_on_the_roster() -> None:
    # FR-017 / research D4. Both directions matter: the bare idiom must not silence a
    # caller, and the real IT camp must still be recognized when the roster knows it.
    assert not _elsewhere("yeah go for it", roster=["Mira", "Cat"])
    assert _elsewhere("waterworks go for it")  # ROSTER carries "IT"


def test_a_single_unmatched_leading_name_is_not_a_hail() -> None:
    # FR-009: the doubled-name shape is the signal. One leading word is ordinary speech.
    assert _verdict("zorp zorp zorp this is mira").form == FORM_HAIL
    assert _verdict("okay mira").form != FORM_HAIL
