"""Go-ahead detection for outbound calls (feature 124) — pure protocol vocabulary.

When obi has hailed someone ("Dana, Dana, obi") the channel's etiquette answers with a
go-ahead in loose shapes: bare "go", "go ahead", "go for Dana", "go for obi",
"Dana, go for obi", "obi, go for Dana". This module classifies one received
transmission against the ONE outstanding hail — the runner calls it only while the
call machine reports an outbound acknowledgment window open, so the loosened shapes
exist nowhere else (contract O8; with no hail pending a bare "go" keeps matching
nothing anywhere, exactly as before this feature).

Design rules, all inherited from the neighbors:

- pure and clock-free (the calls.py idiom): text in, decision out, no I/O;
- the machine keeps no roster (calls.py:311) — the CALLEE, obi's name variants, and
  the operator's caller aliases ride in as arguments, resolved by the trigger's own
  matcher so "go for Dena" reaches Dana at the same 0.75 bar as everything else;
- shapes must OPEN the transmission (token index 0, or 1 after one filler token) —
  the sign-off precedent: "we'll go for the generator swap at six" mid-window is
  conversation, not permission;
- a go-ahead naming a station that is neither the callee nor obi NEVER matches
  (spec FR-007): "go for Mike" while we wait on Dana is other traffic.

Also home to :func:`with_over_sign_off`, the deterministic idempotent framing helper
the originators use to fix the delivery text at submission (contract O3) — protocol
correctness never rides on the operator remembering to say "over" (the 093 rule).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from obi.radio.trigger import (
    _FILLER_TOKENS,
    _MAX_HANDLE_TOKENS,
    _squeeze,
    _tokens,
    match_caller,
)

#: Shape labels, stored per classification for the audit trail (contract O9).
SHAPE_BARE_GO = "bare_go"
SHAPE_GO_AHEAD = "go_ahead"
SHAPE_GO_FOR_CALLEE = "go_for_callee"
SHAPE_GO_FOR_OBI = "go_for_obi"
SHAPE_CALLEE_GO_FOR_OBI = "callee_go_for_obi"
SHAPE_OBI_GO_FOR_CALLEE = "obi_go_for_callee"
SHAPE_HAIL_BACK = "hail_back"


@dataclass(frozen=True)
class GoAheadDecision:
    """Whether one transmission is the awaited go-ahead, and which shape said so."""

    matched: bool
    shape: str | None = None


def _is_obi(raw: str | None, name_variants: Sequence[str]) -> bool:
    if raw is None:
        return False
    squeezed = _squeeze(raw)
    return bool(squeezed) and any(_squeeze(v) == squeezed for v in name_variants)


def _name_at(
    tokens: list[str],
    index: int,
    *,
    callee: str,
    name_variants: Sequence[str],
    aliases: Mapping[str, str] | None,
) -> tuple[str | None, int]:
    """('callee' | 'obi' | None, tokens consumed) for the name starting at ``index``.

    Longest span first (multi-word handles — the exchange tracker's rule); the callee
    is matched through the trigger's fuzzy matcher against a one-name roster plus the
    operator's curated aliases, obi through the exact squeeze of its name variants.
    """

    rest = tokens[index:]
    # A name span never crosses "go" — otherwise "dana go" fuzzy-matches Dana (0.8)
    # and greedily swallows the very token the shape needs ("Dana, go for obi").
    limit = min(_MAX_HANDLE_TOKENS, len(rest))
    if "go" in rest[:limit]:
        limit = rest[:limit].index("go")
    for span in range(limit, 0, -1):
        heard = " ".join(rest[:span])
        if match_caller(heard, [callee], aliases) is not None:
            return ("callee", span)
        if _is_obi(heard, name_variants):
            return ("obi", span)
    return (None, 0)


def _go_shape_at(
    tokens: list[str],
    index: int,
    *,
    callee: str,
    name_variants: Sequence[str],
    aliases: Mapping[str, str] | None,
) -> str | None:
    """The go-shape opening at ``index``, or None. ``tokens`` already over-stripped."""

    rest = tokens[index:]
    if not rest or rest[0] != "go":
        return None
    if len(rest) == 1:
        return SHAPE_BARE_GO
    if rest[1] == "ahead":
        return SHAPE_GO_AHEAD
    if rest[1] == "for":
        named, span = _name_at(
            tokens, index + 2, callee=callee, name_variants=name_variants, aliases=aliases
        )
        if named == "callee":
            return SHAPE_GO_FOR_CALLEE
        if named == "obi":
            return SHAPE_GO_FOR_OBI
        # "go for <someone else>" is never ours (FR-007) — and "go for it"/"go for
        # coffee" opening a transmission is conversation either way.
        return None
    return None


#: A name prefix before the go is at most this many name spans — the longest real
#: opener is the hail-back "obi, obi, dana(, go)".
_MAX_NAME_PREFIX_SPANS = 4


def detect_go_ahead(
    text: str,
    *,
    callee: str,
    name_variants: Sequence[str],
    aliases: Mapping[str, str] | None = None,
) -> GoAheadDecision:
    """Classify one transmission heard inside obi's own outbound ack window.

    The opener may carry a prefix of NAMES — obi's ("obi, go for Dana"), the callee's
    ("Dana, go for obi"), or the full hail-back ("obi, obi, Dana") — but any other
    word there breaks the shape: "tell obi go" is a sentence, not permission.
    """

    tokens = _tokens(text)
    # A trailing "over" is transmission punctuation, not content ("go for Dana, over").
    while tokens and tokens[-1] == "over":
        tokens.pop()
    if not tokens or not callee.strip():
        return GoAheadDecision(matched=False)
    for start in (0, 1):
        if start == 1 and (len(tokens) < 2 or tokens[0] not in _FILLER_TOKENS):
            continue
        index = start
        saw_obi = False
        saw_callee = False
        for _ in range(_MAX_NAME_PREFIX_SPANS + 1):
            shape = _go_shape_at(
                tokens, index, callee=callee, name_variants=name_variants, aliases=aliases
            )
            if shape is not None:
                if saw_callee and shape in (SHAPE_BARE_GO, SHAPE_GO_AHEAD, SHAPE_GO_FOR_OBI):
                    # "Dana, go for obi" — and "Dana, go" / "Dana, go ahead": the
                    # callee self-identifying before the go, unambiguous either way.
                    return GoAheadDecision(matched=True, shape=SHAPE_CALLEE_GO_FOR_OBI)
                if saw_obi and not saw_callee and shape == SHAPE_GO_FOR_CALLEE:
                    return GoAheadDecision(matched=True, shape=SHAPE_OBI_GO_FOR_CALLEE)
                return GoAheadDecision(matched=True, shape=shape)
            named, span = _name_at(
                tokens, index, callee=callee, name_variants=name_variants, aliases=aliases
            )
            if named is None or not span:
                break
            if named == "obi":
                saw_obi = True
            else:
                saw_callee = True
            index += span
            if index >= len(tokens):
                # The whole transmission was names. An obi-led hail-back naming the
                # callee ("obi, obi, Dana") is the callee responding to OUR hail —
                # permission by protocol. The callee's bare name alone is not enough:
                # third parties say names too.
                if saw_obi and saw_callee:
                    return GoAheadDecision(matched=True, shape=SHAPE_HAIL_BACK)
                break
    return GoAheadDecision(matched=False)


#: Wait phrases (spec FR-012): the callee heard us and asked for time — stop re-hailing,
#: keep listening. Must OPEN the transmission, same rule as the go-ahead shapes.
_STANDBY_OPENERS: tuple[tuple[str, ...], ...] = (
    ("standby",),
    ("stand", "by"),
    ("wait", "one"),
)

#: Decline phrases: the callee is refusing the exchange — close as 'declined', never
#: re-hail at someone who said no.
_DECLINE_OPENERS: tuple[tuple[str, ...], ...] = (
    ("negative",),
    ("negatory",),
    ("no", "copy"),
)


def _opens_with(text: str, shapes: tuple[tuple[str, ...], ...]) -> bool:
    tokens = _tokens(text)
    for start in (0, 1):
        if start == 1 and (len(tokens) < 2 or tokens[0] not in _FILLER_TOKENS):
            continue
        for shape in shapes:
            if tuple(tokens[start : start + len(shape)]) == shape:
                return True
    return False


def is_standby(text: str) -> bool:
    """Whether an in-window transmission is the callee asking obi to wait."""

    return _opens_with(text, _STANDBY_OPENERS)


def is_decline(text: str) -> bool:
    """Whether an in-window transmission is the callee refusing the call."""

    return _opens_with(text, _DECLINE_OPENERS)


_TRAILING_SIGN_OFF = re.compile(r"\b(?:over and out|over|out)\b[.!?]*\s*$", re.IGNORECASE)


def with_over_sign_off(text: str) -> str:
    """Frame a delivery text with a terminal "over" — deterministic and idempotent.

    "Over" invites the reply the delivered message expects ("copy"); an operator who
    already closed with "over"/"out"/"over and out" passes through untouched. The
    sibling of ``calls._with_terminal_sign_off``, applied at SUBMISSION time so the
    requester sees the exact on-air text (contract O3).
    """

    if _TRAILING_SIGN_OFF.search(text):
        return text
    stripped = text.rstrip()
    return f"{stripped}, over" if stripped else text
