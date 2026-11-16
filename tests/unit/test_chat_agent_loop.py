"""T004: the agent loop runs a tool, feeds the result back, then returns the final answer."""

from __future__ import annotations

from typing import Any

from obi.chat.agent import MAX_TOOL_ITERATIONS, run_conversation
from obi.core.llm import Usage
from tests.fakes import StubLLMClient, text_response, tool_response


def test_loop_runs_tool_then_answers() -> None:
    client = StubLLMClient(
        [
            tool_response("inventory_list", {"discovery_state": "any"}),
            text_response("3 devices are down."),
        ]
    )
    messages: list[dict[str, Any]] = []
    used: list[str] = []
    result = run_conversation(
        client=client,
        system="sys",
        tools=[{"name": "inventory_list"}],
        messages=messages,
        model="m",
        max_tokens=100,
        dispatch={"inventory_list": lambda args: "ROSTER-DATA"},
        on_tool=lambda name, inp: used.append(name),
    )

    assert result.answer == "3 devices are down."
    assert result.tools_used == ["inventory_list"]
    assert used == ["inventory_list"]
    # The tool result was fed back to the model on the second turn.
    assert client.calls[1]["messages"][-1]["content"][0]["content"] == "ROSTER-DATA"
    # The same tool schemas were passed each turn.
    assert client.calls[0]["tools"] == [{"name": "inventory_list"}]


def test_loop_answers_without_tools() -> None:
    client = StubLLMClient([text_response("hello")])
    result = run_conversation(
        client=client, system="s", tools=[], messages=[], model="m", max_tokens=10, dispatch={}
    )

    assert result.answer == "hello"
    assert result.tools_used == []
    assert result.tool_calls == []
    assert result.usage.model_calls == 1


def test_loop_captures_tool_calls_and_sums_usage() -> None:
    """089: tool calls are captured verbatim and usage sums over EVERY model call."""

    client = StubLLMClient(
        [
            tool_response(
                "routeros_query",
                {"device": "core-gw-1", "path": "/interface"},
                usage=Usage(input_tokens=100, output_tokens=20, cache_read_input_tokens=9000),
            ),
            text_response(
                "all up",
                usage=Usage(
                    input_tokens=150, output_tokens=30, cache_creation_input_tokens=1000
                ),
            ),
        ]
    )
    result = run_conversation(
        client=client,
        system="s",
        tools=[{"name": "routeros_query"}],
        messages=[],
        model="m",
        max_tokens=10,
        dispatch={"routeros_query": lambda args: "IFACE-TABLE"},
    )

    (call,) = result.tool_calls
    assert call.name == "routeros_query"
    assert call.arguments == {"device": "core-gw-1", "path": "/interface"}
    assert call.error is False
    # FR-009 transfer: the captured result is byte-identical to the tool_result content the
    # model received on the second call.
    assert call.result == client.calls[1]["messages"][-1]["content"][0]["content"]
    assert call.result == "IFACE-TABLE"
    assert result.usage.input_tokens == 250
    assert result.usage.output_tokens == 50
    assert result.usage.cache_creation_input_tokens == 1000
    assert result.usage.cache_read_input_tokens == 9000
    assert result.usage.model_calls == 2
    assert result.usage.total_input() == 250 + 1000 + 9000


def test_loop_marks_error_results() -> None:
    """089: the toolset's "error…" convention and the unknown-tool result set the error flag."""

    client = StubLLMClient(
        [
            tool_response("inventory_list", {}, use_id="a"),
            tool_response("no_such_tool", {}, use_id="b"),
            text_response("done"),
        ]
    )
    result = run_conversation(
        client=client,
        system="s",
        tools=[],
        messages=[],
        model="m",
        max_tokens=10,
        dispatch={"inventory_list": lambda args: "error: 'target' is required."},
    )

    first, second = result.tool_calls
    assert first.error is True
    assert second.name == "no_such_tool"
    assert second.result == "error: unknown tool 'no_such_tool'"
    assert second.error is True


def test_tool_limit_still_reports_capture() -> None:
    """089 edge case: the "stopped" result carries the full capture — abnormal turns report too."""

    client = StubLLMClient([tool_response("inventory_list", {"n": 1})])  # repeats forever
    result = run_conversation(
        client=client,
        system="s",
        tools=[],
        messages=[],
        model="m",
        max_tokens=10,
        dispatch={"inventory_list": lambda args: "DATA"},
    )

    assert result.answer.startswith("(stopped:")
    assert len(result.tool_calls) == MAX_TOOL_ITERATIONS
    assert result.usage.model_calls == MAX_TOOL_ITERATIONS
    assert all(call.result == "DATA" for call in result.tool_calls)
