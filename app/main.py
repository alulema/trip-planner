"""FastAPI app: serves the single-page UI from `/` and streams the agent chain over SSE.

No auth and no TLS here by design: the app is meant to run behind a reverse proxy /
gateway that terminates TLS and authenticates users. It is stateless — every run lives
only for the duration of its request. The LLM is a local Ollama server (OLLAMA_HOST).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import load_settings
from .decisions import build_decision_engine
from .guardrails import Admission, GuardrailError
from .llm_client import build_llm
from .models import SharedContext, UserRequest
from .orchestrator import ChainError, Orchestrator, graph_mermaid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("trip_planner")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
KEEPALIVE_SECONDS = 10

settings = load_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the model in the background so the server answers health checks immediately.
    warmup = asyncio.create_task(app.state.llm.warmup())
    yield
    warmup.cancel()


app = FastAPI(title="Trip Planner — Chain-of-Agents demo", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.state.settings = settings
app.state.admission = Admission(
    per_ip_per_hour=settings.max_requests_per_ip_per_hour,
    global_per_hour=settings.max_sessions_per_hour,
    max_concurrent=settings.max_concurrent_sessions,
)
app.state.llm = build_llm(settings.llm_mode, settings.ollama_host, settings.ollama_model, settings.mock_latency_ms)
app.state.engine = build_decision_engine(settings.decision_engine)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "project": settings.project_id, "slot": settings.demo_slot,
            "llm_ready": app.state.llm.ready}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    """Public, non-secret settings the UI displays (mode badge, limits)."""
    return {
        "llm_mode": settings.llm_mode,
        "model": app.state.llm.model,
        "llm_ready": app.state.llm.ready,
        "decision_engine": app.state.engine.name,
        "max_days": 7,
        "max_tokens_per_session": settings.max_tokens_per_session,
        "max_conflict_iterations": settings.max_conflict_iterations,
        "chain_timeout_seconds": settings.chain_timeout_seconds,
    }


@app.get("/api/graph", response_class=PlainTextResponse)
async def graph() -> str:
    """The agent chain as a Mermaid diagram, generated from the LangGraph graph itself."""
    return graph_mermaid()


def _client_ip(request: Request) -> str:
    # CF-Connecting-IP is set by Cloudflare and can't be forged by the client; the first
    # X-Forwarded-For value can, so it is only a fallback for other reverse proxies.
    connecting = request.headers.get("cf-connecting-ip", "").strip()
    if connecting:
        return connecting
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/plan-trip/stream")
async def plan_trip_stream(
    request: Request,
    destination: str = Query(...),
    days: int = Query(...),
    budget_usd: float = Query(...),
    travelers: int = Query(1),
    interests: str = Query("", description="Comma-separated"),
    lang: str = Query("es"),
) -> StreamingResponse:
    # EventSource can't read error bodies, so every outcome (including validation and
    # guardrail refusals) is delivered as an SSE `error` event.
    try:
        user_request = UserRequest(
            destination=destination,
            days=days,
            budget_usd=budget_usd,
            travelers=travelers,
            interests=[i for i in interests.split(",") if i.strip()],
            lang=lang if lang in ("es", "en") else "es",
        )
    except ValidationError as exc:
        fields = ", ".join(str(e["loc"][0]) for e in exc.errors())
        return _single_event("error", {"code": "invalid_request", "message": f"Invalid input: {fields}"})

    return StreamingResponse(
        _run_chain(request, user_request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _single_event(event: str, data: dict[str, Any]) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        yield _sse(event, data)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def _run_chain(request: Request, user_request: UserRequest) -> AsyncIterator[str]:
    admission: Admission = request.app.state.admission
    try:
        await admission.acquire(_client_ip(request))
    except GuardrailError as exc:
        yield _sse("error", {"code": exc.code, "message": exc.message})
        return

    ctx = SharedContext(session_id=str(uuid.uuid4()), user_request=user_request)
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    orchestrator = Orchestrator(request.app.state.llm, request.app.state.engine,
                                settings.max_tokens_per_session, settings.max_conflict_iterations)

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    async def runner() -> None:
        try:
            async with asyncio.timeout(settings.chain_timeout_seconds):
                await orchestrator.run(ctx, emit)
            await emit("done", ctx.model_dump(mode="json"))
        except TimeoutError:
            await emit("error", {"code": "timeout",
                                 "message": "The demo is busy — the agent chain took too long. Please try again."})
        except ChainError as exc:
            await emit("error", {"code": "chain_failed", "message": str(exc)})
        except Exception:  # noqa: BLE001 — never leak internals to the client
            log.exception("session=%s crashed", ctx.session_id)
            await emit("error", {"code": "internal", "message": "Unexpected error — please try again."})
        finally:
            await queue.put(None)

    yield _sse("session", {"session_id": ctx.session_id, "llm_mode": settings.llm_mode})
    task = asyncio.create_task(runner())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield _sse(*item)
    finally:
        # Client disconnected (or the gateway cut the stream): stop spending tokens.
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await admission.release()
