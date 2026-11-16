"""Deterministic directed-call detection (feature 088, FR-007).

No model sits in the trigger path: whether obi was called is decided by matching the
transcript's opening tokens against the configured name-variant list (whisper renders
"obi" as obi/obie/obee/"o b"/opie depending on signal quality). A mention of the name
anywhere else never fires — "tell obi later" is somebody's conversation, not a call.

Every decision carries a ``reason`` (the shadow-stage tuning record, stored per
transmission): ``name_at_start`` (fired), ``no_name``, ``mid_sentence_mention``, or
``unintelligible`` (the STT gate already rejected the text — FR-006 makes such segments
ineligible regardless of content).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

# One leading FILLER token is tolerated before the name ("uh obi this is…") — whisper
# frequently hallucinates a short filler at segment start. Only these exact tokens qualify:
# any real word before the name ("tell obi later") is a sentence, not a call.
_FILLER_TOKENS = frozenset({"uh", "um", "ah", "hey", "yeah", "okay", "ok", "so"})

# A stated handle after "this is" is at most this many tokens ("net control").
_MAX_HANDLE_TOKENS = 3

# A short bare-hail remainder that OPENS with one of these is a request, not a mangled
# callsign ("obi obi where's tumble", "obi obi set portal red" — feature 116); roster
# match always wins first, so a real or near-miss callsign still identifies. Two shapes:
# question openers and the imperative verbs obi can actually serve (status/diagnostic
# reads + the WLED portal writes). Whisper's trailing "?" is deliberately NOT a signal —
# "Obi, Obi, Herrar?" (rising-intonation hail) must still ack, never compose.
_REQUEST_OPENERS = frozenset({
    # questions
    "where", "where's", "what", "what's", "when", "when's", "who", "who's", "whose",
    "why", "how", "how's", "is", "are", "was", "were", "can", "could", "will",
    "would", "do", "does", "did", "should",
    # imperatives
    "set", "turn", "make", "change", "show", "tell", "give", "get", "check", "ping",
    "trace", "locate", "find", "run", "read", "list", "report", "revert", "restore",
})


@dataclass(frozen=True)
class TriggerDecision:
    """Whether a transmission opened a call, and why (the per-segment shadow record).

    ``caller_known`` is True only when the handle canonicalized against the roster —
    the certainty bar for addressing the caller by name in the ack (operator direction
    2026-08-19: "quill, go for obi" only when identified from the list).
    """

    fired: bool
    reason: str
    caller: str | None = None
    caller_known: bool = False
    # One-breath calls ("obi obi this is quill what's the uptime of the hub"): the words after
    # the handle ARE the request — the machine composes right after the ack instead of
    # waiting for a second transmission (field, 2026-08-19).
    request_tail: str | None = None
    # The name was REPEATED in hail position ("obi, obi, …") — the channel's formal
    # new-call opener, as opposed to a single-name re-fire mid-call ("obi, what's the
    # uptime…"), which is a continuation. The call machine uses the distinction to tell
    # new-call intent (fresh call, fresh budget — feature 129) from a budget-bound
    # follow-up (field 2026-09-01, radio.db call 187: "Obi, Obi, revert the tower to
    # the previous state" was swallowed silently because the doubling never reached it).
    doubled: bool = False


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9']+", " ", text.lower()).split()


def _squeeze(text: str) -> str:
    """The comparison form for callsigns: lowercase, punctuation/spacing removed —
    "Hi-Fi" / "hi fi" / "hifi" and "ZFX" / "z f x" unify."""

    return re.sub(r"[^a-z0-9]", "", text.lower())


# Fuzzy-match acceptance: a transcription near-miss ("quil" for Quill) clears this; a
# different word ("shabar", "radio check") does not. Ties within the margin are treated
# as ambiguous and rejected — a wrong canonical name is worse than none (fail-safe).
_MATCH_THRESHOLD = 0.75
_AMBIGUITY_MARGIN = 0.05


def _roster_match(
    raw: str | None,
    roster: Sequence[str],
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, float] | None:
    """Best roster callsign + score for a transcribed handle, or ``None``.

    Field lesson (2026-08-19): Whisper renders unknown proper nouns as common words
    ("Quill" → "quilt"). The vocabulary prompt fixes most of it; this cleans up the
    residual near-misses. Canonical names come back lowercased (the trigger's token
    domain). Near-ties between two roster names are refused — a wrong canonical name
    is worse than none.

    ``aliases`` are operator-curated KNOWN mis-hearings ("quilt" → Quill, field
    2026-08-20) that fuzzy scoring can't reach (0.67 < threshold): an exact
    (squeezed) alias hit is a certain identification and outranks fuzzy scoring.
    They apply only here — in callsign position — never inside request text.
    """

    if raw is None:
        return None
    squeezed = _squeeze(raw)
    if len(squeezed) < 2:
        return None
    for heard, canonical in (aliases or {}).items():
        if squeezed == _squeeze(heard):
            return canonical.lower(), 1.0
    if not roster:
        return None
    scored = sorted(
        (
            (SequenceMatcher(None, squeezed, _squeeze(name)).ratio(), name.lower())
            for name in roster
        ),
        reverse=True,
    )
    best_score, best_name = scored[0]
    if best_score < _MATCH_THRESHOLD:
        return None
    if (
        len(scored) > 1
        and scored[1][1] != best_name
        and best_score - scored[1][0] < _AMBIGUITY_MARGIN
    ):
        return None  # two roster names are equally plausible — refuse to guess
    return best_name, best_score


def match_caller(
    raw: str | None,
    roster: Sequence[str],
    aliases: Mapping[str, str] | None = None,
) -> str | None:
    """The canonical roster callsign for a transcribed handle, or ``None``."""

    matched = _roster_match(raw, roster, aliases)
    return matched[0] if matched is not None else None


def _match_variant_at(tokens: list[str], index: int, variants: list[list[str]]) -> int:
    """Length in tokens of the longest variant matching at ``index``; 0 when none does."""

    best = 0
    for variant in variants:
        span = len(variant)
        if span and tokens[index : index + span] == variant:
            best = max(best, span)
    return best


def _call_start(tokens: list[str], variant_tokens: list[list[str]]) -> int | None:
    """Index where obi's name opens the transmission (one filler tolerated), or None."""

    if _match_variant_at(tokens, 0, variant_tokens):
        return 0
    if (
        len(tokens) > 1
        and tokens[0] in _FILLER_TOKENS
        and _match_variant_at(tokens, 1, variant_tokens)
    ):
        return 1
    return None


def _weak_name_at(
    tokens: list[str],
    index: int,
    weak_tokens: list[list[str]],
    strong_squeezed: list[str],
) -> int:
    """Length of a WEAK station-name match at ``index`` (feature 109), 0 when none.

    Weak = the operator's ``name_variants_weak`` list (mined mis-hearings: ovi, obini,
    okie) plus a fuzzy fallback against the STRONG variants at the caller-matcher's own
    threshold. Weak matches never open a call on their own — see ``_weak_call_start``.
    """

    span = _match_variant_at(tokens, index, weak_tokens)
    if span:
        return span
    token = tokens[index]
    if len(token) >= 3 and any(
        SequenceMatcher(None, token, squeezed).ratio() >= _MATCH_THRESHOLD
        for squeezed in strong_squeezed
    ):
        return 1
    return 0


def _weak_call_start(
    tokens: list[str],
    variant_tokens: list[list[str]],
    weak_tokens: list[list[str]],
    strong_squeezed: list[str],
) -> int | None:
    """Feature 109: a weak variant opens a hail ONLY in the doubled shape.

    Every mangled hail in the mined history was doubled ("Ovi, Ovi, Vertigo"; "Obini,
    Obini, Corvid"; "Okie, Okie, Wren") and the doubling is what makes a common-word
    variant safe — "Okie dokie, heading over…" does not repeat the name, so it can
    never fire. One leading filler token is tolerated, as for strong hails.
    """

    for start in (0, 1):
        if start == 1 and (len(tokens) < 2 or tokens[0] not in _FILLER_TOKENS):
            continue
        first = _weak_name_at(tokens, start, weak_tokens, strong_squeezed)
        if not first:
            continue
        after = start + first
        if after < len(tokens) and (
            _match_variant_at(tokens, after, variant_tokens)
            or _weak_name_at(tokens, after, weak_tokens, strong_squeezed)
        ):
            return start
    return None


def heard_handle(text: str, *, variants: list[str]) -> str | None:
    """The RAW handle heard in callsign position, roster-matched or not.

    Mirrors ``detect``'s parse (name at start, then "this is <handle>" or a bare short
    remainder) but returns the heard words verbatim in token form — the input for
    ``[radio.caller_aliases]`` curation ("sir", "rook", "hever" → who was that really?).
    A long bare remainder — or a short one opening with a request word (feature 116) —
    is a one-breath request, not a handle."""

    tokens = _tokens(text)
    variant_tokens = [_tokens(v) for v in variants if _tokens(v)]
    if not tokens or not variant_tokens:
        return None
    start = _call_start(tokens, variant_tokens)
    if start is None:
        return None
    index = start
    while index < len(tokens):
        span = _match_variant_at(tokens, index, variant_tokens)
        if not span:
            break
        index += span
    if tokens[index : index + 2] == ["this", "is"]:
        rest = tokens[index + 2 :]
        return rest[0] if rest else None
    remainder = tokens[index:]
    if (
        remainder
        and len(remainder) <= _MAX_HANDLE_TOKENS
        and remainder[0] not in _REQUEST_OPENERS
    ):
        return " ".join(remainder)
    return None


def detect(
    text: str,
    *,
    variants: list[str],
    variants_weak: Sequence[str] = (),
    intelligible: bool = True,
    known_callers: Sequence[str] = (),
    caller_aliases: Mapping[str, str] | None = None,
) -> TriggerDecision:
    """Classify one transcript: did it open a directed call to obi?

    ``known_callers`` (the channel roster) canonicalizes near-miss handles after
    "this is", and additionally recognizes the bare-handle call form ("obi obi
    sawdust") — only when the entire remainder matches a roster name, so a request
    ("obi radio check") is never mistaken for a callsign.

    Feature 109 tolerance, both fail-closed by shape: ``variants_weak`` (mined
    mis-hearings + fuzzy near-misses of the strong variants) fire only in the
    DOUBLED-hail shape, and one arbitrary leading token ("Beep, Obi, Bloom") is
    tolerated only when the remainder identifies a known caller or uses "this is" —
    otherwise "tell obi later" (name at index 1 too) would open calls.
    """

    if not intelligible:
        return TriggerDecision(fired=False, reason="unintelligible")
    tokens = _tokens(text)
    variant_tokens = [_tokens(v) for v in variants if _tokens(v)]
    if not tokens or not variant_tokens:
        return TriggerDecision(fired=False, reason="no_name")
    weak_tokens = [_tokens(v) for v in variants_weak if _tokens(v)]
    strong_squeezed = [s for s in (_squeeze(v) for v in variants) if s]

    junk_guarded = False
    start = _call_start(tokens, variant_tokens)
    if start is None:
        start = _weak_call_start(tokens, variant_tokens, weak_tokens, strong_squeezed)
    if start is None and len(tokens) > 1 and _match_variant_at(tokens, 1, variant_tokens):
        # Tentative junk-start — accepted below only if the remainder identifies.
        start, junk_guarded = 1, True
    if start is None:
        mentioned = any(
            _match_variant_at(tokens, i, variant_tokens) for i in range(len(tokens))
        )
        reason = "mid_sentence_mention" if mentioned else "no_name"
        return TriggerDecision(fired=False, reason=reason)

    # Consume the (possibly repeated) name — "obi obi this is nono". Weak spans count
    # here too: the doubled weak hail's second name must not be read as the handle.
    index = start
    names_heard = 0
    while index < len(tokens):
        span = _match_variant_at(tokens, index, variant_tokens) or _weak_name_at(
            tokens, index, weak_tokens, strong_squeezed
        )
        if not span:
            break
        index += span
        names_heard += 1
    this_is_form = tokens[index : index + 2] == ["this", "is"]

    caller: str | None = None
    caller_known = False
    request_tail: str | None = None
    if this_is_form:
        rest = tokens[index + 2 :]
        # The handle is the BEST-SCORING roster prefix of what follows (multi-word
        # callsigns: "ash actual" — exact beats a longer fuzzy grab like "ash actual
        # radio"); with no roster match it is the FIRST token only — never a greedy grab
        # of the request ("this is quill what's the uptime…" ⇒ caller "quill"; field
        # lesson 2026-08-19). What follows the handle is the request tail — trusted as
        # a one-breath request at ANY length for a roster-identified caller, but only
        # when clearly longer than a handle for an unknown one ("this is net control"
        # must not compose an answer to "control").
        matched_len = 0
        candidates = []
        for span in range(1, min(_MAX_HANDLE_TOKENS, len(rest)) + 1):
            matched = _roster_match(" ".join(rest[:span]), known_callers, caller_aliases)
            if matched is not None:
                candidates.append((matched[1], span, matched[0]))
        if candidates:
            _score, matched_len, caller = max(candidates)
            caller_known = True
        elif rest:
            caller, matched_len = rest[0], 1
        tail = rest[matched_len:]
        if tail and (
            caller_known or len(tail) > _MAX_HANDLE_TOKENS or tail[0] in _REQUEST_OPENERS
        ):
            request_tail = " ".join(tail)
    elif len(tokens) - index > 0:
        remainder = tokens[index:]
        if len(remainder) <= _MAX_HANDLE_TOKENS:
            # Bare-handle call form: the whole remainder must BE a roster callsign. A
            # short remainder that matches nothing is most likely a MANGLED callsign
            # ("obi obi herrar") — ack plainly and wait; never compose an answer to it.
            # Exception (feature 116, field 2026-08-30 call 146): a remainder opening
            # with a question word or imperative verb is a short request ("obi obi
            # where's tumble") — compose to it; a mangled callsign never opens that way.
            caller = match_caller(" ".join(remainder), known_callers, caller_aliases)
            caller_known = caller is not None
            if not caller_known and remainder[0] in _REQUEST_OPENERS:
                request_tail = " ".join(remainder)
        else:
            # "obi, what's the uptime of the NOC gateway" — long remainder, no handle:
            # the remainder is the request itself (one-breath call).
            request_tail = " ".join(remainder)
    if junk_guarded and not caller_known and not this_is_form:
        # The leading token was a real word, not noise ("tell obi later") — nobody
        # identified, so this is a sentence about obi, not a hail (feature 109).
        return TriggerDecision(fired=False, reason="mid_sentence_mention")
    return TriggerDecision(
        fired=True,
        reason="name_at_start",
        caller=caller,
        caller_known=caller_known,
        request_tail=request_tail,
        doubled=names_heard >= 2,
    )
