"""The garble judgement (feature 133): is this transcript a sick-capture hallucination?

The radio surface pins Whisper's decode language to English (``voice/stt.py``, field
lesson 2026-08-19), so no language verdict exists to lean on — and the pinned decoder
still emits foreign tokens when the capture chain has degraded: ``"Ó, píða, og fór í
hana."`` (rx #6095, 2026-09-07 21:00:51 PT, accepted as intelligible; the operator
restarted the station by hand fifteen seconds later), ``"Within Earth, Within
Earth,струструст…"`` (rx #2033), ``"остbenig er остbenig er …"`` (rx #2040, both
2026-08-27). The transcript TEXT is the only signal the runner has, and three shapes
cover every recorded incident:

- ``repetition`` — the key-beep / dead-stream loop ("asc asc asc", "окок…",
  "Bitcoin Bitcoin Bitcoin"), the rule the station has carried since 088;
- ``non_latin`` — letters from another script (Cyrillic, Greek, CJK …);
- ``foreign_orthography`` — Latin letters English never uses (ð þ æ, accented vowels)
  beyond the tolerance one accented word or name deserves ("café", "Zoë").

Pure and stateless (contract G1): no I/O, no config. A non-``None`` verdict is stored
as ``stt_gate="unintelligible"`` by the runner, so every downstream rule — the
once-per-call say-again budget, the on-air garble close, low-confidence attribution,
the trigger's ``intelligible=False`` — applies unchanged. The verdict is deliberately
NOT behind the ``garble_reopen`` switch (operator decision 2026-09-07): nonsense must
never reach the answer composer, whatever the device-recovery policy is. The replay
corpus (``tests/fixtures/radio_replay``) is the false-positive guard.
"""

from __future__ import annotations

import re
import unicodedata

REPETITION = "repetition"
NON_LATIN = "non_latin"
FOREIGN_ORTHOGRAPHY = "foreign_orthography"

# A squeezed periodic blob is a loop when one unit of up to this many characters
# repeats at least _MIN_LOOP_REPEATS times with nothing else. 1–6 caught "adoado…" and
# "окок…"; the 10-char unit of "остbenig er остbenig er …" (rx #2040) needed 12.
_MAX_LOOP_UNIT = 12
_MIN_LOOP_REPEATS = 4
# Real English traffic carries zero letters from another script; one or two are a
# transcriber slip, three are the hallucination.
_MIN_NON_LATIN_LETTERS = 3
# Latin-with-foreign-orthography: garbled at three such letters, OR when two word
# occurrences carry one ("Er það er það?") — one accented word stays intelligible.
_MIN_FOREIGN_LETTERS = 3
_MIN_FOREIGN_WORDS = 2
# Latin letters that carry no combining mark yet never occur in English words.
_FOREIGN_PLAIN_LATIN = frozenset("ðþæœøłß")

_WORD_RE = re.compile(r"[^\W\d_]+")


def is_repetition_hallucination(text: str) -> bool:
    """Whisper's key-beep signatures: a token loop ("asc asc asc …") or a spaceless
    periodic blob ("adoadoadoado…" — same decode loop, no token boundaries).

    Radios chirp on key-up; whisper transcribes the tone as looping nonsense often
    enough that periodicity is a reliable hallucination marker (field, 2026-08-19,
    rounds two and four). Real traffic never repeats one short unit 4+ times with
    nothing else.
    """

    tokens = text.lower().split()
    if len(tokens) >= 3 and len(set(tokens)) == 1:
        return True
    # Unicode-aware squeeze (field 2026-08-20 15:27: a 446-char "окок…" blob passed —
    # the ASCII-only class stripped every Cyrillic letter, leaving nothing to test).
    squeezed = re.sub(r"[\W_]", "", text.lower())
    if not squeezed and text.strip():
        return True  # punctuation/symbol soup is never real traffic
    if len(squeezed) >= 12:
        for unit_len in range(1, _MAX_LOOP_UNIT + 1):
            unit = squeezed[:unit_len]
            repeats = len(squeezed) // unit_len
            if repeats >= _MIN_LOOP_REPEATS and (unit * (repeats + 1))[: len(squeezed)] == squeezed:
                return True
    return False


def _is_latin(ch: str) -> bool:
    try:
        return unicodedata.name(ch).startswith("LATIN")
    except ValueError:  # unnamed code point — not a letter English uses either way
        return False


def _is_foreign_latin(ch: str) -> bool:
    """A Latin letter English orthography never uses: a plain foreign letter, or any
    letter whose canonical decomposition carries a combining mark (á í ó ð-less ü …)."""

    if ch.lower() in _FOREIGN_PLAIN_LATIN:
        return True
    decomposed = unicodedata.normalize("NFKD", ch)
    return len(decomposed) > 1 and any(unicodedata.combining(c) for c in decomposed)


def garble_signature(text: str) -> str | None:
    """The hallucination signature this transcript carries, or ``None`` when it reads as
    English traffic. Precedence: repetition → non_latin → foreign_orthography."""

    text = unicodedata.normalize("NFC", text)
    if not text.strip():
        return None
    if is_repetition_hallucination(text):
        return REPETITION
    letters = [ch for ch in text if ch.isalpha()]
    non_latin = sum(1 for ch in letters if not _is_latin(ch))
    if non_latin >= _MIN_NON_LATIN_LETTERS:
        return NON_LATIN
    foreign_letters = 0
    foreign_words = 0
    for word in _WORD_RE.findall(text):
        count = sum(1 for ch in word if _is_latin(ch) and _is_foreign_latin(ch))
        if count:
            foreign_letters += count
            foreign_words += 1
    if foreign_letters >= _MIN_FOREIGN_LETTERS or foreign_words >= _MIN_FOREIGN_WORDS:
        return FOREIGN_ORTHOGRAPHY
    return None
