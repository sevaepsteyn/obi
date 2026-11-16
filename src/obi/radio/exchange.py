"""Passive speaker attribution for third-party channel exchanges (feature 088 follow-up).

The channel's own etiquette names speakers out loud: "waterworks, go for IT" is IT
answering; "this is sawdust" is sawdust; a hail "IT, IT, waterworks" is waterworks
calling IT. Between those patterns, the two sides of an active exchange alternate. The
:class:`ExchangeTracker` watches every received transmission (the field case: the
IT/waterworks keyboard exchange, 2026-08-20) and emits a best-guess speaker with an
honest confidence: ``high`` when the speaker was NAMED on the air, ``low`` when merely
inferred by alternation (a double-key breaks alternation — which is exactly why it can
never be ``high``).

Pure and clock-free (the calls.py idiom): timestamps ride in with each observation, no
I/O, single-threaded by contract (only the RX consumer thread observes). State lives for
the process; a restart merely disables alternation until the next on-air pattern
re-anchors the exchange.

Feature 114 makes the verdict complete rather than merely descriptive. Each attribution
now also carries WHO the transmission was directed at and WHICH shape said so, and
:func:`addressed_elsewhere` turns that into the one question the call protocol asks: is
this traffic between other stations? Field incident 2026-08-29 (radio.db call 118): obi
answered "Larch, Larch, Larch, Go, Quill" and "Go for Kestrel" — a Quill↔Kestrel exchange —
because this module recognized both and nobody was listening.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from obi.radio.trigger import _MAX_HANDLE_TOKENS, _squeeze, _tokens, match_caller

# Alternation is only trusted while the exchange is clearly still going.
_DEFAULT_WINDOW_S = 45.0

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

#: The shapes the channel's etiquette makes explicit. Only the two DIRECTED forms name a
#: station the transmission is FOR, so only they can suppress a reply (feature 114): a
#: bare self-identification and an alternation guess carry no addressing evidence at all.
FORM_HAIL = "hail"  # "X[, X][, X] (this is) Y" — Y calling X
FORM_ANSWER = "answer"  # "[X,] go for Y" — Y answering X's hail
FORM_SELF_ID = "self_id"  # "this is Y", nobody addressed
FORM_ALTERNATION = "alternation"  # neither party named; inferred from the exchange

#: Words that fill a name slot grammatically but never name a station. Applied ONLY to a
#: handle that matched no roster entry: this channel really does have a station called
#: "IT" ("Go for IT" runs through the whole stored history), so a roster hit always
#: outranks the stopword reading. The residual cost is a MISSED suppression when a real
#: station's name is an unrostered stopword — the safe direction, since a miss is today's
#: behavior while a false positive silences a live caller.
_NON_NAMES = frozenset(
    {
        "it", "that", "this", "them", "him", "her", "you", "me", "us", "one", "now",
        "the", "a", "an", "and", "but", "so", "yeah", "yes", "no", "ok", "okay", "uh",
        "um", "ah", "hey", "well", "all", "any", "some", "sure", "here", "there", "we",
        "they", "he", "she", "i", "my", "your", "our", "is", "are", "was", "be", "to",
        "of", "in", "on", "at", "for", "with", "about", "just", "like", "what", "who",
        "when", "where", "why", "how", "not", "over", "out", "copy", "roger", "thanks",
        "please",
    }
)


@dataclass(frozen=True)
class Attribution:
    """The tracker's verdict for one transmission."""

    speaker: str | None
    confidence: str | None  # 'high' | 'low' | None
    # Feature 114: who the wording directs this at, and which shape said so. ``None``
    # when the transmission named nobody — the common case for a request mid-call.
    addressee: str | None = None
    form: str | None = None


def _is_self(name: str | None, name_variants: Sequence[str]) -> bool:
    """Whether a heard handle IS obi, by any configured name variant."""

    if name is None:
        return False
    squeezed = _squeeze(name)
    return bool(squeezed) and any(_squeeze(v) == squeezed for v in name_variants)


def addressed_elsewhere(attribution: Attribution, name_variants: Sequence[str]) -> bool:
    """Whether this transmission is directed at a station that is not obi.

    The call protocol's whole question (feature 114). Note what is deliberately NOT an
    input: the open call's own caller. Obi never hails anyone, so a hail-ANSWER heard
    inside obi's window is directed at whoever did the hailing — someone else — even when
    the call's own caller speaks it. That is exactly the second field misfire (tx 3228,
    "Go for Vertigo", spoken by the station whose call it was), so a "the speaker is our
    caller, therefore it is for us" exemption would preserve the bug it must fix.
    """

    if attribution.form == FORM_HAIL:
        return not _is_self(attribution.addressee, name_variants)
    if attribution.form == FORM_ANSWER:
        return not (
            _is_self(attribution.addressee, name_variants)
            or _is_self(attribution.speaker, name_variants)
        )
    return False


class ExchangeTracker:
    def __init__(
        self,
        *,
        roster: Sequence[str],
        window_s: float = _DEFAULT_WINDOW_S,
        aliases: Mapping[str, str] | None = None,
        name_variants: Sequence[str] = (),
    ) -> None:
        self._roster = list(roster)
        # Operator-curated mis-hearings ("quilt" → Quill) — certain in callsign position.
        self._aliases = dict(aliases or {})
        # obi's own name variants (feature 114): needed to tell a hail TO obi from one
        # that merely happened inside its window. Empty is safe but blunt — a hail to obi
        # then reads as third-party traffic, so every live caller supplies them.
        self._name_variants = list(name_variants)
        self._window_us = int(window_s * 1_000_000)
        self._party_a: str | None = None
        self._party_b: str | None = None
        self._last_speaker: str | None = None
        self._last_seen_us: int | None = None

    # -- pattern helpers ----------------------------------------------------------------

    def _handle_at(self, tokens: list[str], index: int) -> tuple[str | None, bool, int]:
        """(handle, roster_matched, tokens_consumed) for the ≤3 tokens at ``index``.

        Best-scoring roster prefix wins (the trigger's span logic); with no roster match
        the FIRST token alone is the raw handle — honest best-guess, marked low later.
        The consumed span is what the repeat scan advances by: a fuzzy hit can canonicalize
        two heard tokens into one name ("water works" → "waterworks"), and stepping by the
        canonical name's length would walk off the repetition.
        """

        rest = tokens[index:]
        if not rest:
            return (None, False, 0)
        for span in range(min(_MAX_HANDLE_TOKENS, len(rest)), 0, -1):
            matched = match_caller(" ".join(rest[:span]), self._roster, self._aliases)
            if matched is not None:
                return (matched, True, span)
        return (rest[0], False, 1)

    def _station_at(self, tokens: list[str], index: int) -> tuple[str | None, bool, int]:
        """``_handle_at``, but an unrostered ordinary word is no station at all."""

        handle, matched, span = self._handle_at(tokens, index)
        if handle is not None and not matched and handle.lower() in _NON_NAMES:
            return (None, False, 0)
        return (handle, matched, span)

    def _parse_pattern(
        self, tokens: list[str]
    ) -> tuple[str | None, bool, str | None, str | None]:
        """(speaker, roster_matched, addressee, form) from the wording, or Nones."""

        # Rule 1 — "[X,] go for Y": Y is SPEAKING (answering X's hail). Checked before the
        # hail rule so "IT, go for waterworks" is never read as a hail from IT.
        for j in range(len(tokens) - 1):
            if tokens[j : j + 2] == ["go", "for"]:
                speaker, matched, _span = self._station_at(tokens, j + 2)
                if speaker is None:
                    # "go for it" is the English idiom, not station IT answering — the
                    # stoplist yields to the roster, so a real IT still parses (114 D4).
                    break
                addressee = None
                if j > 0:
                    leading, lead_matched, _ = self._handle_at(tokens, 0)
                    # A leading name is the hailer being answered. Accept it when the
                    # roster knows it, or when it is obi — "Obi, go for Vertigo" is a
                    # caller responding to obi, not traffic to stay out of.
                    if lead_matched or _is_self(leading, self._name_variants):
                        addressee = leading
                return (speaker, matched, addressee, FORM_ANSWER)
        # Rule 2 — the hail "X[, X][, X] (this is) Y": a leading station name, repeated or
        # marked by "this is", then the caller identifying themselves.
        addressee, addressee_matched, _span = self._station_at(tokens, 0)
        if addressee is not None:
            index, repeats = 0, 0
            while index < len(tokens):  # consume the (possibly repeated) hailed name
                nxt, _nxt_matched, span = self._handle_at(tokens, index)
                if nxt != addressee or not span:
                    break
                index += span
                repeats += 1
            if 0 < index < len(tokens):
                rest = tokens[index:]
                explicit = rest[:2] == ["this", "is"]
                body = rest[2:] if explicit else rest
                # An UNROSTERED hailed name needs corroboration — the repeated-name shape,
                # or the explicit marker. Feature 109 made this argument for obi's own weak
                # variants ("the doubling is what makes a common-word variant safe"); the
                # hailed name earns it the same way. Without this the field incident stands:
                # "Vertigo" is off the roster, and even on it "Verde" scores 0.67 against
                # it, under the matcher's 0.75 bar.
                if (addressee_matched or repeats >= 2 or explicit) and (
                    0 < len(body) <= _MAX_HANDLE_TOKENS
                ):
                    caller = match_caller(" ".join(body), self._roster, self._aliases)
                    if caller is not None and caller != addressee:
                        return (caller, True, addressee, FORM_HAIL)
        # Rule 3 — bare self-identification "this is Y", nobody addressed.
        for j in range(len(tokens) - 1):
            if tokens[j : j + 2] == ["this", "is"]:
                speaker, matched, _ = self._handle_at(tokens, j + 2)
                return (speaker, matched, None, FORM_SELF_ID)
        return (None, False, None, None)

    # -- the observation API ------------------------------------------------------------

    def observe(
        self, *, started_us: int, ended_us: int, text: str, intelligible: bool
    ) -> Attribution:
        """Attribute one received transmission and update the exchange state."""

        if intelligible:
            tokens = _tokens(text)
            speaker, matched, addressee, form = self._parse_pattern(tokens)
            if speaker is not None:
                # obi is never seated as an exchange party: its own transmissions never
                # reach the tracker, so alternating across it would attribute the NEXT
                # receive to obi. Dropping the addressee here keeps the stored speaker
                # column behaving exactly as it did before feature 114.
                self._note_speaker(
                    speaker,
                    None if _is_self(addressee, self._name_variants) else addressee,
                    matched,
                    ended_us,
                )
                return Attribution(
                    speaker=speaker,
                    confidence=CONFIDENCE_HIGH if matched else CONFIDENCE_LOW,
                    addressee=addressee,
                    form=form,
                )
        # Rule 4 — alternation: both parties established and the exchange still fresh.
        if (
            self._party_a is not None
            and self._party_b is not None
            and self._last_speaker is not None
            and self._last_seen_us is not None
            and started_us - self._last_seen_us <= self._window_us
        ):
            speaker = self._party_b if self._last_speaker == self._party_a else self._party_a
            self._last_speaker = speaker
            self._last_seen_us = ended_us
            return Attribution(
                speaker=speaker, confidence=CONFIDENCE_LOW, form=FORM_ALTERNATION
            )
        return Attribution(speaker=None, confidence=None)

    def _note_speaker(
        self, speaker: str, addressee: str | None, matched: bool, ended_us: int
    ) -> None:
        fresh = (
            self._last_seen_us is not None
            and ended_us - self._last_seen_us <= self._window_us
        )
        if addressee is not None:
            self._party_a, self._party_b = speaker, addressee
        elif matched and speaker not in (self._party_a, self._party_b):
            # A named outsider re-anchors the exchange: pair them with the previous
            # speaker when the exchange is fresh, else start over with one known party.
            previous = self._last_speaker if fresh else None
            self._party_a = speaker
            self._party_b = previous if previous != speaker else None
        self._last_speaker = speaker
        self._last_seen_us = ended_us
