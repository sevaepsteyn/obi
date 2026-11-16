"""Unit tests: the spoken-style system-prompt addendum (feature 023, T007/FR-006)."""

from __future__ import annotations

from obi.core.prompt import SYSTEM_PROMPT, VOICE_SYSTEM_ADDENDUM, system_prompt


def test_addendum_is_nonempty_and_spoken_style() -> None:
    lowered = VOICE_SYSTEM_ADDENDUM.lower()
    assert VOICE_SYSTEM_ADDENDUM.strip()
    # The three spoken-style constraints the spec demands (FR-006): brevity, no visual
    # formatting, listening-friendly numbers.
    assert "sentence" in lowered
    assert "markdown" in lowered or "formatting" in lowered
    assert "round" in lowered


def test_addendum_appends_cleanly_after_the_chat_prompt() -> None:
    composed = system_prompt(None) + VOICE_SYSTEM_ADDENDUM
    assert composed.startswith(SYSTEM_PROMPT)
    assert composed.endswith(VOICE_SYSTEM_ADDENDUM)
    # Appending must not disturb the cached chat prefix (research R5): the base prompt is a
    # byte-identical prefix of the composed one.
    assert composed[: len(SYSTEM_PROMPT)] == SYSTEM_PROMPT


def test_chat_and_watch_prompts_are_untouched() -> None:
    # FR-006 applies only to voice sessions; the chat prompt must not mention the addendum.
    assert VOICE_SYSTEM_ADDENDUM not in SYSTEM_PROMPT
    assert system_prompt(None) == SYSTEM_PROMPT
