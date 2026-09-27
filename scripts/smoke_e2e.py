"""End-to-end smoke test against a running stack (app + Ollama with the real model).

Waits for the model to load, plans a few trips over the real SSE endpoint and reports
latency per agent, tokens, and output-quality signals. Exits non-zero if a run fails.

    python scripts/smoke_e2e.py --base-url http://localhost:8080

When $GITHUB_STEP_SUMMARY is set (GitHub Actions), a Markdown report is appended to it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import httpx

SCENARIOS = [
    # name, params, expect_conflict
    ("Presupuesto holgado (es)", {"destination": "Kioto", "days": 3, "budget_usd": 2500, "travelers": 1,
                                  "interests": "comida,templos", "lang": "es"}, False),
    ("Unrealistic budget (en)", {"destination": "Lisbon", "days": 3, "budget_usd": 50, "travelers": 2,
                                 "interests": "wine,museums", "lang": "en"}, True),
    # A cheap destination: its reference costs should differ from the others.
    ("Destino económico (es)", {"destination": "Hanói", "days": 2, "budget_usd": 400, "travelers": 1,
                                "interests": "comida callejera,historia", "lang": "es"}, False),
    # Not in the curated catalog: everything comes from the model.
    ("Fuera del catálogo (es)", {"destination": "Valparaíso", "days": 2, "budget_usd": 600, "travelers": 1,
                                 "interests": "arte,miradores", "lang": "es"}, False),
]

GENERIC = {"market", "mercado", "temple", "templo", "street", "museum", "museo", "cathedral", "catedral",
           "church", "iglesia", "palace", "palacio", "garden", "gardens", "jardin", "plaza", "square", "tower",
           "bridge", "puente", "park", "parque", "food", "shrine", "santuario", "basilica", "mosque", "night",
           "house", "casa", "lake", "lago", "visit", "visita", "walk", "paseo", "free", "tour", "view", "mirador"}


def _fold(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _stems(text: str) -> set[str]:
    """Distinctive word stems (first 5 letters), so "Vietnamese"/"Vietnamienses" or a short
    name like "Ngọc" still match; generic words ("museum", "temple") prove nothing."""
    words = re.findall(r"[a-z0-9']+", _fold(text))
    return {w[:5] for w in words if len(w) >= 4 and w not in GENERIC}


def highlight_placement(ctx: dict) -> tuple[int, int, list[str]]:
    """(highlights mentioned in their own day's activities, highlights checkable, misplaced).

    Only the activities count (not the free alternative), and words shared with the city or an
    area name are ignored — they appear everywhere and prove nothing."""
    by_area = ctx["destination_research"].get("area_highlights") or {}
    city = _stems(ctx["user_request"]["destination"])
    days = ctx["itinerary_draft"]["days"]
    used = planned = 0
    misplaced = []
    seen_areas = set()
    for d in days:
        words = _stems(" ".join(d["activities"]))
        own = _stems(d["area"]) | city
        # Words of this day's own highlights ("Santa" Luzia) can't prove another area's
        # highlight ("Elevador de Santa Justa") is here.
        own_highlights = set().union(set(), *(_stems(h) for h in by_area.get(d["area"], [])))
        if d["area"] in by_area and d["area"] not in seen_areas:
            seen_areas.add(d["area"])
            for h in by_area[d["area"]]:
                key = _stems(h) - own
                if key:  # e.g. "Hoàn Kiếm Lake" has nothing distinctive beyond the area name
                    planned += 1
                    used += bool(key & words)
        for area, hs in by_area.items():
            if area == d["area"]:
                continue
            misplaced += [f"day {d['day']} ({d['area']}): {h}" for h in hs
                          if (_stems(h) - _stems(area) - own - own_highlights) & words]
    return used, planned, misplaced


CLAIMS_WITHIN = re.compile(r"within (your|the) budget|fits the budget|dentro del presupuesto", re.I)
CLAIMS_OVER = re.compile(r"over budget|excede el presupuesto", re.I)


def wait_ready(client: httpx.Client, timeout_s: int) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        try:
            if client.get("/api/health").json().get("llm_ready"):
                return time.monotonic() - t0
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit(f"model not ready after {timeout_s}s")


def run_trip(client: httpx.Client, params: dict) -> dict:
    events: list[tuple[str, dict]] = []
    t0 = time.monotonic()
    first_token_at = None
    with client.stream("GET", "/api/plan-trip/stream", params=params, timeout=None) as resp:
        event = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:])
                events.append((event, data))
                if event == "token" and first_token_at is None:
                    first_token_at = time.monotonic() - t0
                if event in ("done", "error"):
                    break
    return {"events": events, "elapsed_s": time.monotonic() - t0, "first_token_s": first_token_at}


def analyze(name: str, params: dict, expect_conflict: bool, run: dict) -> tuple[list[str], list[str], dict]:
    """Returns (failures, warnings, metrics)."""
    failures, warnings = [], []
    events = run["events"]
    last_event, last = events[-1] if events else ("none", {})
    if last_event != "done":
        return [f"{name}: ended with {last_event}: {last}"], [], {}
    ctx = last
    final = ctx["final_itinerary"]
    traces = [d for e, d in events if e == "trace"]
    durations = {}
    for t in traces:
        if t["event"] == "completed" and t.get("duration_ms") is not None:
            durations[t["agent"]] = durations.get(t["agent"], 0) + t["duration_ms"]
    tokens = {k: v["input_tokens"] + v["output_tokens"] for k, v in ctx["token_usage"].items()}
    fallback = any(t.get("message") == "template fallback (no LLM)" for t in traces)
    days = final["days"]
    empty_days = [d["day"] for d in days if not d["activities"]]
    no_free_alt = [d["day"] for d in ctx["itinerary_draft"]["days"] if not d["free_alternative"]]

    for agent in ("destination_research", "itinerary_planning"):
        if tokens.get(agent, 0) <= 0:
            failures.append(f"{name}: {agent} reported no tokens (did the real model run?)")
    if len(days) != params["days"]:
        failures.append(f"{name}: expected {params['days']} days, got {len(days)}")
    if fallback:
        warnings.append(f"{name}: synthesis fell back to the template")
    if empty_days:
        warnings.append(f"{name}: days without activities from the model: {empty_days}")
    if no_free_alt:
        warnings.append(f"{name}: days without free_alternative: {no_free_alt}")
    if expect_conflict and not ctx["conflict_resolution"]["triggered"]:
        warnings.append(f"{name}: expected the conflict loop to trigger")
    if len(final["summary"]) < 40:
        warnings.append(f"{name}: very short summary")
    # Honesty: the summary must never contradict the computed budget verdict.
    if final["within_budget"] and CLAIMS_OVER.search(final["summary"]):
        failures.append(f"{name}: summary says over budget but the plan fits")
    if not final["within_budget"] and CLAIMS_WITHIN.search(final["summary"]):
        failures.append(f"{name}: summary claims it fits the budget but it is over")
    draft_days = ctx["itinerary_draft"]["days"]
    if all(d["estimated_cost_usd"] == 0 for d in draft_days) and not ctx["conflict_resolution"]["triggered"]:
        warnings.append(f"{name}: the model priced every day's activities at $0")
    used, planned, misplaced = highlight_placement(ctx)
    swapped = "free_alternatives" in ctx["conflict_resolution"].get("applied_action_ids", [])
    if planned and used < planned / 2 and not swapped:  # the free swap drops paid highlights on purpose
        warnings.append(f"{name}: itinerary mentions only {used}/{planned} planned highlights")
    if misplaced:
        warnings.append(f"{name}: highlights placed in the wrong area: {misplaced}")
    odd_areas = [a for a in ctx["destination_research"]["recommended_areas"] if len(a.split()) > 4]
    if odd_areas:
        warnings.append(f"{name}: area names look like descriptions: {odd_areas}")

    metrics = {
        "elapsed_s": round(run["elapsed_s"], 1),
        "first_token_s": round(run["first_token_s"], 1) if run["first_token_s"] else None,
        "durations_s": {k: round(v / 1000, 1) for k, v in durations.items()},
        "tokens": tokens,
        "tokens_total": sum(tokens.values()),
        "within_budget": final["within_budget"],
        "total_cost_usd": final["total_cost_usd"],
        "conflict": ctx["conflict_resolution"],
        "areas": ctx["destination_research"]["recommended_areas"],
        "source": ctx["destination_research"].get("source", "model"),
        "highlights_used": f"{used}/{planned}" if planned else "–",
        "misplaced": len(misplaced),
        "draft_costs": [d["estimated_cost_usd"] for d in ctx["itinerary_draft"]["days"]],
        "reference_costs": ctx["destination_research"]["reference_costs"],
        "days": days,
        "summary": final["summary"],
    }
    return failures, warnings, metrics


def report(model: str, ready_s: float, results: list) -> str:
    lines = [f"## Trip Planner E2E — `{model}`", "", f"Model ready after **{ready_s:.0f}s**.", ""]
    lines += ["| Scenario | Total | 1st summary token | Research | Itinerary | Synthesis | Tokens | Result |",
              "|---|---|---|---|---|---|---|---|"]
    for name, _, m, fails, _warns in results:
        if not m:
            lines.append(f"| {name} | – | – | – | – | – | – | ❌ {fails[0]} |")
            continue
        d = m["durations_s"]
        verdict = "✅ within budget" if m["within_budget"] else f"⚠️ over budget (${m['total_cost_usd']:,.0f})"
        lines.append(f"| {name} | {m['elapsed_s']}s | {m['first_token_s']}s | {d.get('destination_research', '–')}s "
                     f"| {d.get('itinerary_planning', '–')}s | {d.get('synthesis', '–')}s | {m['tokens_total']} | {verdict} |")
    for name, _, m, fails, warns in results:
        lines += ["", f"### {name}"]
        lines += [f"- ❌ {f}" for f in fails] + [f"- ⚠️ {w}" for w in warns]
        if not m:
            continue
        lines += [f"- Data source: **{m['source']}** · highlights used on their day: {m['highlights_used']}"
                  f" · misplaced: {m['misplaced']}",
                  f"- Areas: {', '.join(m['areas'])}",
                  f"- Reference costs: `{json.dumps(m['reference_costs'])}`",
                  f"- Activity cost per day (final draft): `{m['draft_costs']}`",
                  f"- Tokens per agent: `{json.dumps(m['tokens'])}`"]
        if m["conflict"]["triggered"]:
            lines.append(f"- Conflict: {m['conflict']['iterations']} iteration(s), "
                         f"actions `{m['conflict']['applied_action_ids']}`, resolved={m['conflict']['resolved']}")
        for d in m["days"]:
            lines.append(f"  - Day {d['day']} · {d['area']} · ${d['estimated_cost_usd']}: {'; '.join(d['activities'])}")
        lines += ["", "> " + m["summary"].replace("\n", "\n> ")]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--ready-timeout", type=int, default=600)
    args = ap.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        ready_s = wait_ready(client, args.ready_timeout)
        model = client.get("/api/config").json()["model"]
        results = []
        for name, params, expect_conflict in SCENARIOS:
            print(f"→ {name} …", flush=True)
            run = run_trip(client, params)
            fails, warns, metrics = analyze(name, params, expect_conflict, run)
            results.append((name, params, metrics, fails, warns))
            print(f"  {'FAIL' if fails else 'ok'} in {run['elapsed_s']:.1f}s", flush=True)

    costs = [json.dumps(r[2]["reference_costs"], sort_keys=True) for r in results
             if r[2] and r[2]["source"] == "model"]
    if len(costs) > 1 and len(set(costs)) == 1:
        results[-1][4].append("reference costs are identical for every destination (model not estimating)")

    md = report(model, ready_s, results)
    print(md)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md)
    return 1 if any(r[3] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
