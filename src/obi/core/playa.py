"""Playa place lookup (feature 127): where is any camp or art installation.

One engine, :class:`PlayaLocator`, owns every decision between "where is Camp
Soft Landing?" and the answer string — the toolset handler and the ``obi playa``
CLI are thin callers (Principle III). Answers come from the locally downloaded
Burning Man public API datasets (art + camps, verbatim arrays under
:func:`obi.core.config.default_playa_state_path`): camps speak their published
address string ("G & 9:15") with an optional look-for landmark line; art speaks
obi's own Black Rock City address for its GPS (the 126 forms, Esplanade distance
included). Name resolution takes the trigger.py mishearing policy — squeeze,
accept at ≥ 0.75, refuse a near-tie — with exact and substring matches first.

A lookup NEVER touches the network (Principle VI). The one egress in this module
is :func:`fetch_dataset`, the explicit operator download (`obi playa fetch`),
X-API-Key on /api/art | /api/camp per the official OpenAPI spec — never the old
Postman docs' /api/v1 paths, whose gateway 401s good keys (field 2026-09-02).
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from obi.core.brcaddr import ModelLoader, no_model, street_display, to_address

logger = logging.getLogger("obi")

#: The all-surface answer contract (the mesh_locate constant): ≤600 chars, line 1 a
#: complete spoken-ready sentence, detail lines trimmed first.
MAX_ANSWER_CHARS = 600

_SUGGESTION_CAP = 3
#: The trigger.py / mesh.py mishearing constants — one policy everywhere a
#: transcribed name meets a roster.
_FUZZY_THRESHOLD = 0.75
_FUZZY_MARGIN = 0.05
_CLOSE_ENOUGH = 0.6  # unknown-name suggestions: worth listing, not worth answering

_KIND_ENDPOINTS = {"art": "/api/art", "camp": "/api/camp"}

#: Beyond this distance from the Man the city's clock arithmetic stops helping and the
#: published string carries the context obi's forms lose ("4:45 8000', Airport" — the
#: 2026 airport/walk-in pieces all sit ≥ 8,000 ft). Within it, obi's own address forms
#: (126, Esplanade distance included) are the more speakable ones.
_FAR_FROM_MAN_FT = 7_000.0
#: Coordinates farther than this are dataset placeholders (0,0 renders thousands of
#: miles out) — treat as no coordinates at all.
_ABSURD_FT = 50_000.0


#: A standalone ring letter in a published placement string ("G & 9:15", "9:00 G Plaza").
_RING_LETTER = re.compile(r"\b([A-K])\b")

#: The simple corner form with the ring street FIRST ("G & 9:15") — 668 of 1,184 2026
#: camps publish this order; the other simple corners are already clock-first. Plaza and
#: portal forms don't match and pass through untouched.
_STREET_FIRST_CORNER = re.compile(
    r"^\s*(?P<street>[A-K]|Esplanade|Rod's Road|Route 66)"
    r"\s*&\s*(?P<clock>\d{1,2}:\d{2})\s*$"
)


def _officialize(address: str) -> str:
    """Published placement strings → how the city speaks them (operator, 2026-09-03).

    Two display rules: the clock leads a simple corner ("G & 9:15" → "9:15 & G", the
    order brcaddr's own corner form already uses), and bare ring letters become the
    NATO phonetic words (G is Golf — operator decision after the on-air test). Everything else in
    the string stays verbatim."""

    corner = _STREET_FIRST_CORNER.match(address)
    if corner is not None:
        address = f"{corner.group('clock')} & {corner.group('street')}"
    return _RING_LETTER.sub(lambda match: street_display(match.group(1)), address)


def _squeeze(text: str) -> str:
    """Lowercase alphanumerics only (the trigger.py squeeze): "Soft Landing!" ≡ "softlanding"."""

    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class Place:
    """One published camp or art installation — everything an answer may speak."""

    kind: str  # "camp" | "art"
    name: str
    address: str | None  # the published location_string, e.g. "G & 9:15"
    latitude: float | None
    longitude: float | None
    landmark: str | None  # a camp's published look-for text


_CACHE: dict[str, tuple[float, tuple[Place, ...]]] = {}


def load_places(art_path: Path, camp_path: Path) -> tuple[Place, ...]:
    """Every named place from both datasets; missing/corrupt files contribute ()."""

    return _load_kind(art_path, "art") + _load_kind(camp_path, "camp")


def _load_kind(path: Path, kind: str) -> tuple[Place, ...]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ()
    key = f"{kind}:{path}"
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("playa: unreadable %s dataset at %s", kind, path, exc_info=True)
        raw = []
    places = _places_from(raw, kind)
    _CACHE[key] = (mtime, places)
    return places


def _places_from(raw: object, kind: str) -> tuple[Place, ...]:
    if not isinstance(raw, list):
        logger.debug("playa: %s dataset is not a list — ignoring it", kind)
        return ()
    places: list[Place] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        location = entry.get("location")
        source = location if isinstance(location, dict) else {}
        lat, lon = source.get("gps_latitude"), source.get("gps_longitude")
        has_gps = (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and not isinstance(lat, bool)
            and not isinstance(lon, bool)
        )
        address = str(entry.get("location_string") or "").strip() or None
        landmark = str(entry.get("landmark") or "").strip() or None
        places.append(
            Place(
                kind=kind,
                name=name,
                address=address,
                latitude=float(lat) if has_gps else None,  # type: ignore[arg-type]
                longitude=float(lon) if has_gps else None,  # type: ignore[arg-type]
                landmark=landmark,
            )
        )
    return tuple(places)


def _labeled(place: Place) -> str:
    return f"{place.name} ({place.kind})"


def _resolve(needle: str, places: tuple[Place, ...]) -> Place | list[Place] | tuple[str, ...]:
    """One winning place, a few tied candidates, or labeled name suggestions.

    Exact squeezed match → unique substring → fuzzy at the shared constants. A
    near-tie between DIFFERENT names returns the tied places (a ``list``) so the
    answer can carry each one's location outright — the operator's 2026-09-03
    directive: list both locations, never ask the caller to disambiguate. Only a
    below-threshold miss returns bare name suggestions (a ``tuple``): those are
    guesses, and speaking their locations would dress a guess up as an answer.
    """

    squeezed = _squeeze(needle)
    if len(squeezed) < 2:
        return ()
    # "The temple" means THE Temple — the year's honored build, named "Temple of …" by
    # convention every year (2026: Temple of the Moon). Camps and satellites with
    # "temple" in their names are never what that ask means; when the convention breaks
    # (zero or several "Temple of" pieces) this falls through to ordinary matching.
    if squeezed in ("temple", "thetemple"):
        temples = [
            place
            for place in places
            if place.kind == "art" and _squeeze(place.name).startswith("templeof")
        ]
        if len(temples) == 1:
            return temples[0]
    exact = [place for place in places if _squeeze(place.name) == squeezed]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return exact[:_SUGGESTION_CAP]
    subs = [place for place in places if squeezed in _squeeze(place.name)]
    if len(subs) == 1:
        return subs[0]

    pool = subs if subs else list(places)
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, squeezed, _squeeze(place.name)).ratio(), place)
            for place in pool
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best = scored[0]
    rivals = [
        place
        for score, place in scored[1:]
        if place.name != best.name and best_score - score < _FUZZY_MARGIN
    ]
    if subs:
        # Every substring hit is plausible; a clearly-best one answers alone, a tie
        # answers with each candidate's location.
        if not rivals:
            return best
        return [place for _score, place in scored][:_SUGGESTION_CAP]
    if best_score >= _FUZZY_THRESHOLD:
        if not rivals:
            return best
        return [best, *rivals][:_SUGGESTION_CAP]
    close = [place for score, place in scored[:10] if score >= _CLOSE_ENOUGH]
    return tuple(dict.fromkeys(_labeled(p) for p in close))[:_SUGGESTION_CAP]


class PlayaLocator:
    """Resolve → render, total: every path returns a speakable answer string."""

    def __init__(
        self,
        *,
        art_path: Path,
        camp_path: Path,
        model_loader: ModelLoader | None = None,
    ) -> None:
        self._art_path = art_path
        self._camp_path = camp_path
        self._model_loader = model_loader or no_model

    def locate(self, name: str) -> str:
        needle = name.strip()
        if not needle:
            return "Which camp or art piece? Give me a name."
        places = load_places(self._art_path, self._camp_path)
        if not places:
            return (
                "No playa data downloaded yet — run `obi playa fetch` with a "
                "Burning Man API key to load this year's camps and art."
            )
        resolution = _resolve(needle, places)
        if isinstance(resolution, Place):
            return self._render(resolution)
        if isinstance(resolution, list):
            return self._render_candidates(resolution)
        if resolution:
            listed = "; ".join(resolution)
            return (f"No confident match for '{needle}' — closest: {listed}.")[:MAX_ANSWER_CHARS]
        return (f"Nothing named '{needle}' among this year's published camps and art.")[
            :MAX_ANSWER_CHARS
        ]

    def _location_phrase(self, place: Place) -> str | None:
        """The speakable location, or ``None`` when nothing honest is published."""

        if place.kind == "art" and place.latitude is not None and place.longitude is not None:
            address = to_address(place.latitude, place.longitude, self._model_loader())
            distance_ft = address.distance_ft
            if distance_ft is None or distance_ft <= _FAR_FROM_MAN_FT:
                return address.text
            if place.address:
                return _officialize(place.address)
            if distance_ft <= _ABSURD_FT:
                return address.text
            return None
        return _officialize(place.address) if place.address else None

    def _head(self, place: Place) -> str:
        phrase = self._location_phrase(place)
        if phrase is not None:
            return f"{place.name} is at {phrase}."
        noun = "placement" if place.kind == "camp" else "location"
        return f"{place.name} has no published {noun} this year."

    def _render_candidates(self, candidates: list[Place]) -> str:
        """A tie answers with EVERY candidate's location in one breath (operator
        directive 2026-09-03) — no landmark lines, no 'which did you mean'."""

        parts = []
        for place in candidates:
            phrase = self._location_phrase(place)
            if phrase is None:
                parts.append(f"{_labeled(place)} has no published location")
            else:
                parts.append(f"{_labeled(place)} is at {phrase}")
        answer = "; ".join(parts) + "."
        while len(answer) > MAX_ANSWER_CHARS and len(parts) > 1:
            parts.pop()
            answer = "; ".join(parts) + "."
        return answer[:MAX_ANSWER_CHARS]

    def _render(self, place: Place) -> str:
        head = self._head(place)
        lines = [head]
        if place.kind == "camp" and place.landmark:
            landmark = place.landmark.rstrip(".")
            lines.append(f"- Look for: {landmark}.")
        answer = "\n".join(lines)
        while len(answer) > MAX_ANSWER_CHARS and len(lines) > 1:
            lines.pop()
            answer = "\n".join(lines)
        return answer[:MAX_ANSWER_CHARS]


def fetch_dataset(
    kind: str,
    *,
    year: int,
    key: str,
    out_path: Path,
    base_url: str,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Download one year's dataset (``kind``: "art" | "camp"); returns the entry count.

    The operator's explicit egress (plan D5): ``X-API-Key`` header on the official
    endpoints. The body must decode to a JSON list; the file is written atomically —
    every failure raises BEFORE the rename, so an existing dataset survives.
    """

    endpoint = _KIND_ENDPOINTS[kind]
    with httpx.Client(
        base_url=base_url,
        timeout=timeout,
        headers={"X-API-Key": key},
        transport=transport,
    ) as client:
        response = client.get(endpoint, params={"year": year})
        response.raise_for_status()
        raw = response.json()
    if not isinstance(raw, list):
        raise ValueError(f"{kind} API returned {type(raw).__name__}, expected a list")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / (out_path.name + ".tmp")
    tmp.write_text(json.dumps(raw), encoding="utf-8")
    os.replace(tmp, out_path)
    return len(raw)
