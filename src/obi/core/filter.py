"""Vendor-neutral device-name filter (feature 003, FR-016).

A small, case-insensitive substring matcher used to focus a large fleet on the devices the
operator cares about (e.g. ``"ptp"``). Lives in the core so it is interface-agnostic and
reusable by any vendor source.
"""

from __future__ import annotations

from collections.abc import Sequence


def name_matches(name: str | None, name_filter: str | None) -> bool:
    """Return whether ``name`` passes ``name_filter``.

    Unset/empty filter ⇒ everything matches. A device with no name is excluded when a filter is
    set (it cannot match a non-empty substring). Matching is case-insensitive.
    """

    if not name_filter:
        return True
    if not name:
        return False
    return name_filter.casefold() in name.casefold()


def name_excludes(name: str | None, patterns: Sequence[str]) -> bool:
    """Return whether ``name`` matches any exclude ``patterns`` (feature 035).

    The exclude companion to :func:`name_matches`, for ignore lists (e.g. ``[uisp] ignore_names``).
    No patterns, or a ``None``/empty ``name`` ⇒ ``False`` (nothing is excluded). Otherwise ``True``
    iff some non-empty pattern is a case-insensitive substring of ``name`` — the same convention as
    the watch ``ignore_links``/``ignore_topics`` fields. Compose with :func:`name_matches` by
    including first and excluding from the remainder.
    """

    if not patterns or not name:
        return False
    folded = name.casefold()
    return any(pattern and pattern.casefold() in folded for pattern in patterns)
