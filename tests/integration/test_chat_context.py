"""Integration (008): the operator network context reaches the model, and grounding survives it.

Uses the offline stub LLM client (which records the ``system`` it is sent) and the fake vendor
doubles — no network, no cloud API.
"""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeBehavior,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    tool_response,
)


def test_context_reaches_the_model(tmp_path: Path) -> None:
    """US1/SC-001: with a context_file set, the model actually receives the operator context."""

    ctx_file = tmp_path / "net.md"
    ctx_file.write_text("WALNUT is site 2; the walnut gateway is walnut-gw.")
    config = chat_config(tmp_path)
    config.chat.context_file = ctx_file
    runtime = chat_runtime(config, responses=[text_response("ok")])

    ask(runtime, [], "is the walnut gateway reachable?")

    system = runtime.client.calls[0]["system"]
    assert "WALNUT is site 2" in system
    assert "walnut-gw" in system


def test_context_is_static_across_turns(tmp_path: Path) -> None:
    """FR-012: the system is composed once and identical on every turn (stays cache-friendly)."""

    ctx_file = tmp_path / "net.md"
    ctx_file.write_text("WALNUT is site 2")
    config = chat_config(tmp_path)
    config.chat.context_file = ctx_file
    runtime = chat_runtime(config, responses=[text_response("a"), text_response("b")])

    ask(runtime, [], "q1")
    ask(runtime, [], "q2")

    systems = [call["system"] for call in runtime.client.calls]
    assert len(systems) == 2
    assert systems[0] == systems[1]  # built once, static
    assert "WALNUT is site 2" in systems[0]


def test_grounding_preserved_with_context(tmp_path: Path) -> None:
    """US2/SC-002: even with context loaded, a status question is answered via a tool read.

    The context names chestnut-gw, but liveness comes from routeros_status (a tool), not the
    document.
    """

    ctx_file = tmp_path / "net.md"
    ctx_file.write_text("chestnut-gw is the primary router at chestnut.")
    config = chat_config(tmp_path)
    config.chat.context_file = ctx_file
    save_inventory(
        config.inventory_path,
        [InventoryEntry("172.25.110.1", "chestnut-gw", DiscoveryState.IDENTIFIED)],
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("routeros_status", {"target": "chestnut-gw"}),
            text_response("chestnut-gw is reachable."),
        ],
        behaviors={"172.25.110.1": FakeBehavior()},
    )

    result = ask(runtime, [], "is chestnut-gw up?")

    assert result.tools_used == ["routeros_status"]  # grounded via the tool, not the doc
    # The live read was audited (Principle V) — the doc is not an audit/data source.
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"routeros_status"' in audit
