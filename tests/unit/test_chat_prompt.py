"""The system prompt encodes the read-only role, grounding, and vendor-aware tool guidance."""

from __future__ import annotations

from obi.core.prompt import SYSTEM_PROMPT, system_prompt


def test_prompt_states_read_only_and_lists_tools() -> None:
    prompt = system_prompt()
    lowered = prompt.lower()

    assert "read-only" in lowered
    assert "decline" in lowered
    assert "uisp" in lowered  # UISP is in scope now (feature 006)
    for tool in (
        "inventory_list",
        "inventory_summary",
        "routeros_status",
        "routeros_neighbors",
        "uisp_status",
    ):
        assert tool in prompt
    # Discovery is admin/CLI-only: the agent must have no rescan tool (`obi routeros discover`
    # is the operator's path instead).
    assert "rescan" not in prompt.lower()


def test_prompt_guides_whole_fleet_to_all_vendors() -> None:
    """Whole-fleet questions must steer the model to consult every vendor and label results."""

    lowered = system_prompt().lower()

    assert "all vendors" in lowered  # consult every vendor
    assert "label" in lowered  # label each device's vendor
    # UISP is live-only: the prompt must say there is no cached UISP roster / no UISP topology.
    assert "no cached" in lowered or "no uisp" in lowered or "not available" in lowered


def test_no_context_equals_baseline() -> None:
    """No context_file ⇒ the prompt is byte-for-byte today's prompt (no regression, FR-011)."""

    assert system_prompt(None) == system_prompt() == SYSTEM_PROMPT


def test_context_is_appended_with_text() -> None:
    """A provided context appears in the prompt, on top of the unchanged base (FR-001)."""

    prompt = system_prompt("WALNUT is site 2; the walnut gateway is walnut-gw.")

    assert "WALNUT is site 2" in prompt
    assert "walnut-gw" in prompt
    assert prompt.startswith(SYSTEM_PROMPT)  # base prompt unchanged, context appended after


def test_context_is_framed_interpretation_only() -> None:
    """The appended section must frame context as reference for interpretation, not facts.

    Encodes FR-003/FR-004: interpret terminology, ground in tools, never a fact, tool wins on
    disagreement.
    """

    # Collapse line-wrapping whitespace so phrase checks don't depend on where lines break.
    text = " ".join(system_prompt("chestnut-gw is the primary router.").lower().split())

    assert "reference" in text  # it is reference material …
    assert "interpret" in text  # … for interpreting the operator's terms
    assert "ground every factual claim in tool output" in text  # grounding rule restated
    assert "not live device state" in text and "not a source of facts" in text
    assert "the tool reading wins" in text  # tool is authoritative on disagreement
    # The base read-only role still stands when context is present.
    assert "strictly read-only" in text
