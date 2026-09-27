"""Generative half of the reasoning: a small local LLM (Qwen 2.5) served by Ollama.

Two call shapes:
  * `complete_json` — output constrained to a Pydantic model's JSON schema via Ollama's
    structured outputs (`format`), validated with Pydantic, one retry if still invalid.
  * `complete_text` — free text streamed token by token (the synthesis narrative).

Both stream from Ollama so the UI can show progress while the CPU generates, and both are
capped by — and charged to — the session's token budget (prompt + generated tokens: on a
CPU both cost wall-clock time).

`MockLLM` implements the same interface offline and deterministically (each agent supplies
its canned output); it powers mock mode and the test suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .guardrails import TokenBudget
from .models import TokenUsage

log = logging.getLogger("trip_planner.llm")

T = TypeVar("T", bound=BaseModel)
OnProgress = Callable[[int], Awaitable[None]]  # tokens generated so far
OnToken = Callable[[str], Awaitable[None]]  # text delta

# Below this many tokens left there's no point starting a call: the answer would be cut off.
MIN_TOKENS_FOR_CALL = 300
PROGRESS_EVERY = 16


class LLMError(Exception):
    """The generative step could not produce a valid answer."""


class LLMClient(Protocol):
    model: str
    ready: bool

    async def warmup(self) -> None: ...

    async def complete_json(
        self, *, agent: str, system: str, user: str, output_model: type[T], max_tokens: int,
        budget: TokenBudget, mock: Callable[[], T], on_progress: OnProgress | None = None,
    ) -> tuple[T, TokenUsage]: ...

    async def complete_text(
        self, *, agent: str, system: str, user: str, max_tokens: int, budget: TokenBudget,
        mock: Callable[[], str], on_token: OnToken | None = None,
    ) -> tuple[str, TokenUsage]: ...


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve `$ref`/`$defs` so the schema is self-contained for Ollama's grammar builder,
    and drop cosmetic `title` keys."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: walk(v) for k, v in node.items() if k not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _error_detail(raw: bytes) -> str:
    text = raw.decode(errors="replace")
    try:
        return str(json.loads(text).get("error", text))[:200]
    except (ValueError, AttributeError):
        return text[:200]


class OllamaLLM:
    def __init__(self, host: str, model: str, num_ctx: int = 4096, keep_alive: str = "30m",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.model = model
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.ready = False
        # Generous read timeout: a CPU can pause between tokens while evaluating the prompt.
        self._http = httpx.AsyncClient(
            base_url=host.rstrip("/"),
            timeout=httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=5.0),
            transport=transport,
        )

    async def warmup(self) -> None:
        """Load the model into memory ahead of the first request (cold load takes seconds)."""
        for attempt in range(30):
            try:
                r = await self._http.post("/api/generate", json={
                    "model": self.model, "prompt": "", "keep_alive": self.keep_alive})
                if r.status_code == 200:
                    self.ready = True
                    log.info("model %s loaded", self.model)
                    return
                log.warning("warmup: %s → HTTP %s %s", self.model, r.status_code, r.text[:200])
                if r.status_code == 404:
                    return  # model not present — no point retrying
            except httpx.HTTPError as exc:
                log.info("warmup: waiting for Ollama (%s)", type(exc).__name__)
            await asyncio.sleep(min(2 + attempt, 10))

    async def _stream_chat(self, *, system: str, user: str, num_predict: int, fmt: dict | None,
                           on_chunk: Callable[[str, int], Awaitable[None]]) -> tuple[str, TokenUsage, str]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2, "num_predict": num_predict, "num_ctx": self.num_ctx},
        }
        if fmt is not None:
            body["format"] = fmt
        text, n, final = [], 0, {}
        try:
            async with self._http.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    raise LLMError(f"Local model error (HTTP {resp.status_code}): {_error_detail(await resp.aread())}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise LLMError(f"Local model error: {chunk['error']}")
                    delta = chunk.get("message", {}).get("content", "")
                    if delta:
                        text.append(delta)
                        n += 1
                        await on_chunk(delta, n)
                    if chunk.get("done"):
                        final = chunk
                        break
        except httpx.HTTPError as exc:
            raise LLMError(f"Local model server unreachable ({type(exc).__name__}).") from exc
        self.ready = True
        usage = TokenUsage(input_tokens=final.get("prompt_eval_count", 0), output_tokens=final.get("eval_count", n))
        return "".join(text), usage, final.get("done_reason", "")

    async def complete_json(self, *, agent, system, user, output_model, max_tokens, budget, mock, on_progress=None):
        schema = inline_refs(output_model.model_json_schema())
        usage = TokenUsage()
        last_error: Exception | None = None

        async def on_chunk(_delta: str, n: int) -> None:
            if on_progress and n % PROGRESS_EVERY == 0:
                await on_progress(n)

        for attempt in (1, 2):
            budget.ensure_available(MIN_TOKENS_FOR_CALL)
            text, call_usage, reason = await self._stream_chat(
                system=system, user=user, num_predict=min(max_tokens, budget.remaining), fmt=schema,
                on_chunk=on_chunk)
            usage.input_tokens += call_usage.input_tokens
            usage.output_tokens += call_usage.output_tokens
            budget.record(call_usage.total)
            log.info("agent=%s attempt=%d tokens_in=%d tokens_out=%d done=%s",
                     agent, attempt, call_usage.input_tokens, call_usage.output_tokens, reason)
            try:
                return output_model.model_validate_json(text), usage
            except ValidationError as exc:
                last_error = exc
                log.warning("agent=%s attempt=%d invalid JSON (done=%s)", agent, attempt, reason)
        raise LLMError(f"{agent}: model returned invalid JSON twice") from last_error

    async def complete_text(self, *, agent, system, user, max_tokens, budget, mock, on_token=None):
        budget.ensure_available(MIN_TOKENS_FOR_CALL)

        async def on_chunk(delta: str, _n: int) -> None:
            if on_token:
                await on_token(delta)

        text, usage, reason = await self._stream_chat(
            system=system, user=user, num_predict=min(max_tokens, budget.remaining), fmt=None, on_chunk=on_chunk)
        budget.record(usage.total)
        log.info("agent=%s tokens_in=%d tokens_out=%d done=%s", agent, usage.input_tokens, usage.output_tokens, reason)
        if not text.strip():
            raise LLMError(f"{agent}: empty answer")
        return text.strip(), usage


class MockLLM:
    """Offline stand-in: no network, deterministic output, simulated latency/usage."""

    model = "mock"

    def __init__(self, latency_ms: int = 600):
        self.latency = latency_ms / 1000
        self.ready = True

    async def warmup(self) -> None:
        return None

    @staticmethod
    def _estimate(system: str, user: str, output: str) -> TokenUsage:
        # ≈4 chars/token, so the token meter behaves realistically.
        return TokenUsage(input_tokens=(len(system) + len(user)) // 4, output_tokens=max(1, len(output) // 4))

    async def complete_json(self, *, agent, system, user, output_model, max_tokens, budget, mock, on_progress=None):
        budget.ensure_available(MIN_TOKENS_FOR_CALL)
        result = mock()
        usage = self._estimate(system, user, result.model_dump_json())
        usage.output_tokens = min(usage.output_tokens, max_tokens)
        steps = max(1, usage.output_tokens // PROGRESS_EVERY)
        for i in range(1, steps + 1):
            if self.latency:
                await asyncio.sleep(self.latency / steps)
            if on_progress:
                await on_progress(i * PROGRESS_EVERY)
        budget.record(usage.total)
        return output_model.model_validate(result.model_dump()), usage

    async def complete_text(self, *, agent, system, user, max_tokens, budget, mock, on_token=None):
        budget.ensure_available(MIN_TOKENS_FOR_CALL)
        text = mock()
        words = text.split(" ")
        for i, w in enumerate(words):
            if self.latency:
                await asyncio.sleep(self.latency / len(words))
            if on_token:
                await on_token(w if i == 0 else " " + w)
        usage = self._estimate(system, user, text)
        budget.record(usage.total)
        return text, usage


def build_llm(mode: str, host: str, model: str, mock_latency_ms: int) -> LLMClient:
    if mode == "mock":
        return MockLLM(mock_latency_ms)
    return OllamaLLM(host, model)
