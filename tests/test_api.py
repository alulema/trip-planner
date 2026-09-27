import json

import pytest
from fastapi.testclient import TestClient

from app.guardrails import Admission, SlidingWindowLimiter
from app.main import app

PARAMS = dict(destination="Kyoto", days=3, budget_usd=1200, travelers=1, interests="food,temples", lang="en")


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        if lines[0].startswith(":"):
            continue
        event = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event, data))
    return events


@pytest.fixture()
def client():
    app.state.admission = Admission(per_ip_per_hour=100, global_per_hour=100, max_concurrent=3)
    return TestClient(app)


def test_index_and_health(client):
    assert "Trip Planner" in client.get("/").text
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/static/app.js").status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["llm_mode"] == "mock" and cfg["decision_engine"] == "rules"


def test_stream_happy_path(client):
    r = client.get("/api/plan-trip/stream", params=PARAMS)
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[0][0] == "session"
    assert events[-1][0] == "done"
    final = events[-1][1]["final_itinerary"]
    assert final["within_budget"] is True and len(final["days"]) == 3
    assert app.state.admission.active == 0  # released after the stream ends


def test_invalid_input_is_an_sse_error(client):
    events = parse_sse(client.get("/api/plan-trip/stream", params={**PARAMS, "days": 30}).text)
    assert events == [("error", {"code": "invalid_request", "message": "Invalid input: days"})]


def test_rate_limit_per_ip(client):
    app.state.admission = Admission(per_ip_per_hour=2, global_per_hour=100, max_concurrent=3)
    headers = {"x-forwarded-for": "203.0.113.9"}
    for _ in range(2):
        assert parse_sse(client.get("/api/plan-trip/stream", params=PARAMS, headers=headers).text)[-1][0] == "done"
    events = parse_sse(client.get("/api/plan-trip/stream", params=PARAMS, headers=headers).text)
    assert events[0][0] == "error" and events[0][1]["code"] == "rate_limited"
    # Another address is unaffected.
    other = client.get("/api/plan-trip/stream", params=PARAMS, headers={"x-forwarded-for": "198.51.100.1"})
    assert parse_sse(other.text)[-1][0] == "done"


def test_rate_limit_prefers_cf_connecting_ip(client):
    app.state.admission = Admission(per_ip_per_hour=1, global_per_hour=100, max_concurrent=3)
    first = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "198.51.100.1"}
    assert parse_sse(client.get("/api/plan-trip/stream", params=PARAMS, headers=first).text)[-1][0] == "done"
    # A forged X-Forwarded-For doesn't dodge the limit while CF-Connecting-IP is the same.
    forged = {"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "192.0.2.77"}
    events = parse_sse(client.get("/api/plan-trip/stream", params=PARAMS, headers=forged).text)
    assert events[0][0] == "error" and events[0][1]["code"] == "rate_limited"


def test_sliding_window_expires():
    lim = SlidingWindowLimiter(1, window_seconds=10)
    assert lim.check_and_record("k", now=0)
    assert not lim.check_and_record("k", now=5)
    assert lim.check_and_record("k", now=10.1)


def test_hard_timeout_returns_friendly_error(client, monkeypatch):
    from dataclasses import replace

    import app.main as main
    from app.llm_client import MockLLM

    monkeypatch.setattr(main, "settings", replace(main.settings, chain_timeout_seconds=1))
    monkeypatch.setattr(app.state, "llm", MockLLM(latency_ms=2000))
    events = parse_sse(client.get("/api/plan-trip/stream", params=PARAMS).text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "timeout"
    assert app.state.admission.active == 0


def test_graph_endpoint_returns_mermaid(client):
    r = client.get("/api/graph")
    assert r.status_code == 200 and "budget -.-> conflict_resolution" in r.text
