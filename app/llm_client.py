"""Thin wrapper over the Anthropic SDK: one structured-JSON call per agent step.

Every call:
  * is constrained to a Pydantic schema via structured outputs (`messages.parse`),
  * gets one retry if the model still returns something unparseable (design §11),
  * is capped by the session's remaining token budget and records its usage.

`MockLLM` implements the same interface without network access: each agent supplies a
deterministic `mock` factory. It powers the offline mode and the test suite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .guardrails import TokenBudget
from .models import TokenUsage

log = logging.getLogger("trip_planner.llm")

T = TypeVar("T", bound=BaseModel)

# Below this many tokens left there's no point starting a call: the JSON would be cut off.
MIN_TOKENS_FOR_CALL = 400


class LLMError(Exception):
    """The LLM step could not produce a valid answer (API error or invalid output)."""


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        agent: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int,
        budget: TokenBudget,
        mock: Callable[[], T],
    ) -> tuple[T, TokenUsage]: ...


class AnthropicLLM:
    def __init__(self, model: str, timeout_seconds: float = 25.0):
        # Credentials come from the environment (ANTHROPIC_API_KEY) — never hardcoded.
        self._client = anthropic.AsyncAnthropic(timeout=timeout_seconds, max_retries=1)
        self.model = model

    async def complete(self, *, agent, system, user, output_model, max_tokens, budget, mock):
        output_config = {"format": {"type": "json_schema", "schema": _schema_for(output_model)}}
        usage = TokenUsage()
        last_error: Exception | None = None
        for attempt in (1, 2):
            budget.ensure_available(MIN_TOKENS_FOR_CALL)
            try:
                response = await self._client.messages.create(
                    model=self.model,
                    max_tokens=min(max_tokens, budget.remaining),
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config=output_config,
                )
            except anthropic.RateLimitError as exc:
                raise LLMError("The model provider is rate limiting this demo — try again shortly.") from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(f"Model API error ({exc.status_code}).") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError("Could not reach the model API.") from exc

            # Usage is recorded before validation so failed attempts still count.
            usage.input_tokens += response.usage.input_tokens
            usage.output_tokens += response.usage.output_tokens
            budget.record(response.usage.input_tokens + response.usage.output_tokens)
            log.info(
                "agent=%s attempt=%d tokens_in=%d tokens_out=%d stop=%s",
                agent, attempt, response.usage.input_tokens, response.usage.output_tokens, response.stop_reason,
            )
            if response.stop_reason == "refusal":
                raise LLMError("The model declined this request.")

            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                return output_model.model_validate_json(text), usage
            except ValidationError as exc:
                last_error = exc
                log.warning("agent=%s attempt=%d invalid JSON (stop=%s)", agent, attempt, response.stop_reason)
        raise LLMError(f"{agent}: model returned invalid JSON twice") from last_error


def _schema_for(model: type[BaseModel]) -> dict:
    # The SDK helper rewrites a Pydantic JSON schema into the subset structured outputs
    # accepts (additionalProperties: false, required fields, no unsupported keywords).
    return anthropic.transform_schema(model.model_json_schema())


class MockLLM:
    """Offline stand-in: no network, deterministic output, simulated latency/usage."""

    def __init__(self, latency_ms: int = 600):
        self.latency = latency_ms / 1000

    async def complete(self, *, agent, system, user, output_model, max_tokens, budget, mock):
        budget.ensure_available(MIN_TOKENS_FOR_CALL)
        if self.latency:
            await asyncio.sleep(self.latency)
        result = mock()
        # Rough token estimate (≈4 chars/token) so the usage panel behaves realistically.
        usage = TokenUsage(
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=min(max_tokens, len(result.model_dump_json()) // 4),
        )
        budget.record(usage.total)
        return output_model.model_validate(result.model_dump()), usage


def build_llm(mode: str, model: str, mock_latency_ms: int) -> LLMClient:
    if mode == "mock":
        return MockLLM(mock_latency_ms)
    return AnthropicLLM(model)
