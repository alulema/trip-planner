"""Runtime settings, read once from environment variables.

Every guardrail has a conservative default so a fresh container is safe to expose
publicly without extra configuration. Inference is local and CPU-bound, so the limits
protect compute time rather than an API bill.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    project_id: str
    demo_slot: str

    # "ollama" calls the local model server; "mock" returns canned, deterministic agent
    # output (no model needed) — useful for UI work and for the test suite.
    llm_mode: str
    ollama_host: str
    ollama_model: str
    # Live data (weather, places, exchange rates): "on" calls the public APIs, "mock" returns
    # canned data (offline), "off" skips the step. Defaults to "mock" when the LLM is mocked.
    live_data: str
    overpass_urls: tuple[str, ...]
    # Engine for the non-generative decisions (see app/decisions). Only "rules" today.
    decision_engine: str

    max_sessions_per_hour: int
    max_requests_per_ip_per_hour: int
    max_concurrent_sessions: int
    max_tokens_per_session: int
    chain_timeout_seconds: int
    max_conflict_iterations: int
    mock_latency_ms: int


def load_settings() -> Settings:
    return Settings(
        project_id=os.environ.get("PROJECT_ID", "trip-planner"),
        demo_slot=os.environ.get("DEMO_SLOT", ""),
        llm_mode=os.environ.get("LLM_MODE", "ollama").strip().lower(),
        ollama_host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b-instruct"),
        live_data=(os.environ.get("LIVE_DATA", "").strip().lower()
                   or ("mock" if os.environ.get("LLM_MODE", "ollama").strip().lower() == "mock" else "on")),
        # Comma-separated Overpass endpoints, tried in order (empty = the public defaults).
        overpass_urls=tuple(u.strip() for u in os.environ.get("OVERPASS_URL", "").split(",") if u.strip()),
        decision_engine=os.environ.get("DECISION_ENGINE", "rules").strip().lower(),
        max_sessions_per_hour=_int("MAX_SESSIONS_PER_HOUR", 30),
        max_requests_per_ip_per_hour=_int("MAX_REQUESTS_PER_IP_PER_HOUR", 10),
        # One CPU-bound generation at a time: concurrent runs would just slow each other down.
        max_concurrent_sessions=_int("MAX_CONCURRENT_SESSIONS", 1),
        max_tokens_per_session=_int("MAX_TOKENS_PER_SESSION", 6000),
        chain_timeout_seconds=_int("CHAIN_TIMEOUT_SECONDS", 180),
        # Hard cap from the design: the Itinerary↔Budget↔Conflict loop never runs more
        # than twice. Clamped so it can't be raised by configuration.
        max_conflict_iterations=min(_int("MAX_CONFLICT_ITERATIONS", 2), 2),
        mock_latency_ms=_int("MOCK_LATENCY_MS", 600),
    )
