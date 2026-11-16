"""The injectable LLM-client seam for the chat agent (feature 005, contracts/llm-client.md).

The agent loop depends on :class:`LLMClient`, not on the ``anthropic`` SDK directly, so the
loop and tools are testable fully offline (Constitution II) and a future local-model backend
can slot in without touching the loop (the Principle VI restoration path). ``anthropic`` is
imported lazily inside :class:`AnthropicLLMClient`, so importing this module never requires the
package or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ChatUnavailable(Exception):
    """The cloud reasoning model is unreachable or unauthenticated (maps to CLI exit code 3).

    Carries an operator-facing, actionable message. The offline core (``discover``/``status``)
    is unaffected — the constitution v1.3.0 degraded-mode contract (Principle VI).
    """


@dataclass
class TextBlock:
    """A model text-output block."""

    text: str


@dataclass
class ToolUseBlock:
    """A model request to run a named tool with the given input."""

    id: str
    name: str
    input: dict[str, Any]


ContentBlock = TextBlock | ToolUseBlock


@dataclass
class Usage:
    """Token usage for one model turn (feature 014: the watcher logs per-run token cost).

    Feature 089: the two prompt-cache classes are first-class — the client caches system+tools
    (`cache_control` below), so on a warm turn most of the context arrives as cache reads and
    ``input_tokens`` alone would badly under-report what the model processed (and what it cost).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class LLMResponse:
    """One model turn, normalized away from any specific SDK's wire types."""

    stop_reason: str
    content: list[ContentBlock] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def text(self) -> str:
        """The concatenated text blocks (the final answer when ``stop_reason`` is not tool_use)."""

        return "".join(b.text for b in self.content if isinstance(b, TextBlock)).strip()

    def tool_uses(self) -> list[ToolUseBlock]:
        """The tool-use blocks the model emitted this turn."""

        return [b for b in self.content if isinstance(b, ToolUseBlock)]


class LLMClient(Protocol):
    """The minimal client the agent loop needs — satisfied by the real client and the test stub."""

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> LLMResponse: ...


class AnthropicLLMClient:
    """Production :class:`LLMClient` over the Anthropic Messages API.

    Imports ``anthropic`` lazily and maps its errors to :class:`ChatUnavailable` so chat
    degrades cleanly (a clear message, a non-zero exit) when offline or misconfigured.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ModuleNotFoundError as exc:  # pragma: no cover - dependency is declared
                raise ChatUnavailable(
                    "the 'anthropic' package is not installed; run: uv add anthropic"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> LLMResponse:
        import anthropic

        client = self._ensure_client()
        cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=cached_system,
                tools=_with_cache_control(tools),
                messages=messages,
                # Sonnet 5 turns adaptive thinking ON when the parameter is omitted (Sonnet 4.6
                # ran thinking-off). Keep it off explicitly so thinking tokens don't eat into our
                # small max_tokens budgets and truncate the answer — preserves prior behavior.
                thinking={"type": "disabled"},
            )
        except anthropic.AuthenticationError as exc:
            raise ChatUnavailable(
                "the reasoning model rejected the API key — check ANTHROPIC_API_KEY or "
                "[chat].api_key in your config."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ChatUnavailable(
                "could not reach the reasoning model (offline?). Your offline commands still "
                "work: `obi routeros discover`, `obi status`."
            ) from exc
        except anthropic.APIStatusError as exc:  # rate limit / 5xx / other server-side error
            raise ChatUnavailable(f"the reasoning model returned an error: {exc}") from exc
        return _to_response(message)


class AuditedLLMClient:
    """An :class:`LLMClient` decorator that records every model call to the audit trail (042).

    Wraps any inner client (the production Anthropic one or a test stub) and records one
    ``llm_call`` event per ``create_message`` — the full request (system + messages), the
    normalized response blocks, token usage, and duration — through the ambient audit scope
    (:func:`obi.core.audit.record_event`), so it is a silent pass-through when capture is off.
    Failures are recorded with ``outcome=error`` and re-raised unchanged.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    def create_message(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
    ) -> LLMResponse:
        import time

        from obi.core.audit import record_event

        start = time.monotonic()
        try:
            response = self._inner.create_message(
                system=system, tools=tools, messages=messages, model=model, max_tokens=max_tokens
            )
        except Exception as exc:
            record_event(
                "llm_call",
                summary=f"model call failed: {model}",
                payload={
                    "model": model,
                    "system": system,
                    "messages": messages,
                    "tool_names": [str(t.get("name", "?")) for t in tools],
                },
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error",
                error=str(exc),
            )
            raise
        record_event(
            "llm_call",
            summary=(
                f"model call: {model} "
                f"({response.usage.input_tokens}→{response.usage.output_tokens} tokens, "
                f"{response.stop_reason})"
            ),
            payload={
                "model": model,
                "system": system,
                "messages": messages,
                "tool_names": [str(t.get("name", "?")) for t in tools],
                "response": _blocks_to_payload(response.content),
                "stop_reason": response.stop_reason,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                "cache_read_input_tokens": response.usage.cache_read_input_tokens,
            },
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return response


def _blocks_to_payload(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
    """Serialize normalized response blocks for the audit payload (mirrors the wire shape)."""

    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        else:
            out.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return out


def _with_cache_control(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the last (static) tool schema cacheable so the whole tools+system prefix caches."""

    if not tools:
        return tools
    cached = [dict(tool) for tool in tools]
    cached[-1] = {**cached[-1], "cache_control": {"type": "ephemeral"}}
    return cached


def _to_response(message: Any) -> LLMResponse:
    """Convert an Anthropic ``Message`` into our normalized :class:`LLMResponse` (defensive)."""

    blocks: list[ContentBlock] = []
    for block in getattr(message, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            blocks.append(TextBlock(text=str(getattr(block, "text", ""))))
        elif block_type == "tool_use":
            blocks.append(
                ToolUseBlock(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    input=dict(getattr(block, "input", {}) or {}),
                )
            )
    raw_usage = getattr(message, "usage", None)
    usage = Usage(
        input_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(raw_usage, "cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0),
    )
    return LLMResponse(
        stop_reason=str(getattr(message, "stop_reason", "end_turn")),
        content=blocks,
        usage=usage,
    )
