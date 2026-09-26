"""Runtime settings, read once from environment variables.

Secrets (ANTHROPIC_API_KEY) are only ever read from the environment — never baked into
the image. Every cost guardrail has a conservative default so a fresh container is safe
to expose publicly without extra configuration.
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

    # "anthropic" calls the real API; "mock" returns canned, deterministic agent output
    # (no network, no cost) — useful for local UI work and for the test suite.
    llm_mode: str
    domain_model: str

    max_sessions_per_hour: int
    max_requests_per_ip_per_hour: int
    max_concurrent_sessions: int
    max_tokens_per_session: int
    chain_timeout_seconds: int
    max_conflict_iterations: int
    mock_latency_ms: int


def load_settings() -> Settings:
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    default_mode = "anthropic" if has_key else "mock"
    return Settings(
        project_id=os.environ.get("PROJECT_ID", "trip-planner"),
        demo_slot=os.environ.get("DEMO_SLOT", ""),
        llm_mode=os.environ.get("LLM_MODE", default_mode).strip().lower(),
        domain_model=os.environ.get("DOMAIN_MODEL", "claude-haiku-4-5"),
        max_sessions_per_hour=_int("MAX_SESSIONS_PER_HOUR", 30),
        max_requests_per_ip_per_hour=_int("MAX_REQUESTS_PER_IP_PER_HOUR", 10),
        max_concurrent_sessions=_int("MAX_CONCURRENT_SESSIONS", 3),
        max_tokens_per_session=_int("MAX_TOKENS_PER_SESSION", 8000),
        chain_timeout_seconds=_int("CHAIN_TIMEOUT_SECONDS", 45),
        # Hard cap from the design: the Itinerary↔Budget↔Conflict loop never runs more
        # than twice. Clamped so it can't be raised by configuration.
        max_conflict_iterations=min(_int("MAX_CONFLICT_ITERATIONS", 2), 2),
        mock_latency_ms=_int("MOCK_LATENCY_MS", 600),
    )
