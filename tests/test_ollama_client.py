"""OllamaLLM against a fake Ollama server (httpx.MockTransport speaking /api/chat NDJSON)."""

import asyncio
import json

import httpx
import pytest

from app.guardrails import TokenBudget, TokenBudgetExceeded
from app.llm_client import LLMError, OllamaLLM, inline_refs
from app.models import DestinationResearchOutput, ItineraryPlanningOutput

VALID = DestinationResearchOutput(season_notes="Mild", recommended_areas=["Gion"], lodging_per_night_usd=90,
                                  meal_avg_usd=12, local_transport_day_usd=8).model_dump_json()


def fake_ollama(answers, status=200, requests=None):
    """Each request consumes the next answer and streams it in small chunks."""
    answers = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if requests is not None:
            requests.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": "model 'qwen' not found"})
        text = answers.pop(0)
        chunks = [text[i:i + 4] for i in range(0, len(text), 4)]
        lines = [json.dumps({"message": {"content": c}, "done": False}) for c in chunks]
        lines.append(json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop",
                                 "prompt_eval_count": 50, "eval_count": len(chunks)}))
        return httpx.Response(200, text="\n".join(lines) + "\n")

    return httpx.MockTransport(handler)


def llm_with(transport):
    return OllamaLLM("http://ollama:11434", "qwen2.5:1.5b-instruct", transport=transport)


def complete_json(llm, budget, on_progress=None):
    return asyncio.run(llm.complete_json(agent="t", system="s", user="u", output_model=DestinationResearchOutput,
                                         max_tokens=300, budget=budget, mock=None, on_progress=on_progress))


def test_structured_call_streams_and_counts_usage():
    requests, progress = [], []

    async def on_progress(n):
        progress.append(n)

    llm = llm_with(fake_ollama([VALID], requests=requests))
    budget = TokenBudget(6000)
    out, usage = complete_json(llm, budget, on_progress)
    assert out.recommended_areas == ["Gion"]
    assert usage.input_tokens == 50 and usage.output_tokens > 0 and budget.used == usage.total
    assert progress and progress == sorted(progress)
    body = requests[0]
    assert body["model"] == "qwen2.5:1.5b-instruct" and body["stream"] is True
    assert body["format"]["type"] == "object" and "$defs" not in json.dumps(body["format"])
    assert body["options"]["num_predict"] == 300


def test_invalid_json_is_retried_once():
    llm = llm_with(fake_ollama(["{not json", VALID]))
    budget = TokenBudget(6000)
    out, usage = complete_json(llm, budget)
    assert out.season_notes == "Mild"
    assert usage.input_tokens == 100  # both attempts are charged


def test_invalid_json_twice_raises():
    with pytest.raises(LLMError):
        complete_json(llm_with(fake_ollama(["{}", "nope"])), TokenBudget(6000))


def test_http_error_becomes_llm_error():
    with pytest.raises(LLMError, match=r"HTTP 404\): model .qwen. not found$"):
        complete_json(llm_with(fake_ollama([], status=404)), TokenBudget(6000))


def test_unreachable_server_becomes_llm_error():
    def boom(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="unreachable"):
        complete_json(llm_with(httpx.MockTransport(boom)), TokenBudget(6000))


def test_budget_guard_blocks_before_calling():
    requests = []
    with pytest.raises(TokenBudgetExceeded):
        complete_json(llm_with(fake_ollama([VALID], requests=requests)), TokenBudget(100))
    assert requests == []


def test_text_call_streams_tokens():
    tokens = []

    async def on_token(t):
        tokens.append(t)

    llm = llm_with(fake_ollama(["Un viaje precioso a Kioto."]))
    text, usage = asyncio.run(llm.complete_text(agent="s", system="s", user="u", max_tokens=200,
                                                budget=TokenBudget(6000), mock=None, on_token=on_token))
    assert text == "Un viaje precioso a Kioto." and "".join(tokens) == text and len(tokens) > 1


def test_inline_refs_makes_schema_self_contained():
    schema = inline_refs(ItineraryPlanningOutput.model_json_schema())
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped and '"title"' not in dumped
    assert schema["properties"]["days"]["items"]["properties"]["free_alternative"]["type"] == "string"
