"""The chat agent's tool-use loop (feature 005, contracts/llm-client.md).

Drives a Messages-API conversation: ask the model, run any tool it calls through the read-only
dispatch, feed the results back, and repeat until the model returns a final answer. The loop
holds no business logic — the tools do the work, the loop only routes (Constitution III).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from obi.core.llm import LLMClient, LLMResponse, TextBlock, ToolUseBlock

# A safety bound so a misbehaving model cannot loop forever calling tools.
MAX_TOOL_ITERATIONS = 12

OnTool = Callable[[str, dict[str, Any]], None]
Dispatch = dict[str, Callable[[dict[str, Any]], str]]


@dataclass
class ToolCall:
    """One tool invocation of a turn, exactly as exchanged with the model (feature 089).

    ``result`` is the byte-identical string fed back as ``tool_result`` content — already
    redacted by the tool layer, so surfaces may display it without a second redaction pass.
    """

    name: str
    arguments: dict[str, Any]
    result: str
    error: bool


@dataclass
class TurnUsage:
    """Service-reported token usage summed over every model call of one turn (feature 089)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    model_calls: int = 0

    def total_input(self) -> int:
        """The headline "in" figure: everything the model processed as context."""

        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def add(self, response: LLMResponse) -> None:
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.cache_creation_input_tokens += response.usage.cache_creation_input_tokens
        self.cache_read_input_tokens += response.usage.cache_read_input_tokens
        self.model_calls += 1


@dataclass
class ConversationResult:
    """The outcome of one operator question: the final answer and which tools were run.

    Feature 089 enriches the capture: ``tool_calls`` carries each call's arguments and result
    (call order preserved) and ``usage`` the turn's summed token accounting — surfaces that
    don't report (terminal REPL, voice speech, radio) simply ignore both.
    """

    answer: str
    tools_used: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TurnUsage = field(default_factory=TurnUsage)


def _assistant_content(blocks: list[Any]) -> list[dict[str, Any]]:
    """Rebuild the assistant message content (in wire form) from normalized blocks."""

    content: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return content


def run_conversation(
    *,
    client: LLMClient,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    dispatch: Dispatch,
    on_tool: OnTool | None = None,
) -> ConversationResult:
    """Run the loop to a final answer, appending each turn to ``messages`` as it goes."""

    tools_used: list[str] = []
    tool_calls: list[ToolCall] = []
    usage = TurnUsage()
    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.create_message(
            system=system, tools=tools, messages=messages, model=model, max_tokens=max_tokens
        )
        usage.add(response)
        messages.append({"role": "assistant", "content": _assistant_content(response.content)})

        tool_uses = response.tool_uses()
        if response.stop_reason != "tool_use" or not tool_uses:
            return ConversationResult(
                answer=response.text(), tools_used=tools_used, tool_calls=tool_calls, usage=usage
            )

        results: list[dict[str, Any]] = []
        for use in tool_uses:
            tools_used.append(use.name)
            if on_tool is not None:
                on_tool(use.name, use.input)
            handler = dispatch.get(use.name)
            unknown = f"error: unknown tool '{use.name}'"
            content = handler(use.input) if handler is not None else unknown
            # The toolset's uniform failure convention is an "error…"-prefixed string.
            tool_calls.append(
                ToolCall(
                    name=use.name,
                    arguments=use.input,
                    result=content,
                    error=content.startswith("error"),
                )
            )
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": content})
        messages.append({"role": "user", "content": results})

    return ConversationResult(
        answer="(stopped: reached the tool-call limit without a final answer)",
        tools_used=tools_used,
        tool_calls=tool_calls,
        usage=usage,
    )
