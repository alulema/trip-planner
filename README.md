# Trip Planner: Chain-of-Agents Orchestrator Demo

A containerized demonstration of the **Chain-of-Agents Orchestrator** pattern (Chapter 7, "30 Agents Every AI Engineer Must Build" by Imran Ahmad, Packt) applied to travel planning.

## Overview

Given a travel request (`destination, days, budget, travelers, interests`), the system orchestrates a chain of specialized agents that pass work between them, each adding their expertise, until a validated itinerary is produced.

**Why this matters:** You see in real-time how agents reason, hand off to the next, resolve conflicts (over-budget scenarios), and converge on a solution — without human intervention.

## Architecture

```
User Input: "Kyoto, 5 days, $1200 budget, solo, food & temples"
  ↓
[1] Orchestrator (JEV state machine) → Deterministic control flow
  ↓
[2] Destination Research (Phi 3.8B) → Climate, areas, reference costs
  ↓
[3] Itinerary Planning (Phi 3.8B) → Day-by-day activities
  ↓
[4] Budget Agent (JEV + Python) → Validate cost vs budget
  ↓
[5] Conflict Resolution (JEV strategy + optional Phi) → Negotiate if over budget (max 2 iterations)
  ↓
[6] Synthesis (Phi 3.8B) → Final narrative itinerary
  ↓
Response + Real-time trace (SSE stream)
```

## Key Patterns Demonstrated

- **Chain-of-Agents Orchestrator:** One agent orchestrates the flow, maintains shared context, coordinates handoffs
- **Memory-Augmented Multi-Agent System:** Single JSON context object shared across all agents (each appends, never overwrites)
- **Conflict Resolution Mechanism:** Explicit resolution logic for constraint violations (budget), with iteration limits to prevent infinite loops
- **JEV (JSON Execution Vectors):** Deterministic reasoning for control flow and budget calculations (shows when NOT to use an LLM)
- **Real-Time Streaming:** SSE-based agent trace visible to the user, making the orchestration transparent

## Stack

| Component | Choice | Reason |
|---|---|---|
| **Backend** | Python 3.12 + FastAPI + Uvicorn | Async, lightweight, SSE-native |
| **LLM** | Phi 3.8B via Ollama | Local, CPU-friendly, OSS |
| **Reasoning Engine** | JEV (typesafe.ai) | Deterministic for orchestration + budget |
| **Data Contracts** | Pydantic v2 | Strict validation between agents |
| **Streaming** | Server-Sent Events (SSE) | Unidirectional agent→UI, simpler than WebSocket |
| **Frontend** | Vanilla HTML + JS | No framework, no build step |
| **Container** | Docker Compose | App + Ollama sidecar |

## Running Locally

### Prerequisites
- Docker + Docker Compose
- (Optional) Python 3.12 + pip (if running without Docker)

### With Docker Compose (Recommended)

```bash
docker-compose up
```

This starts:
1. **Ollama** service (pulls Phi 3.8B on first run, ~2GB download)
2. **FastAPI app** on `http://localhost:8080`

Navigate to `http://localhost:8080` and submit a trip request.

### Without Docker (Development)

```bash
# Install dependencies
pip install -r requirements.txt

# Start Ollama (separately, in another terminal or as a service)
# Ollama pulls Phi on first request if not already cached

# Run FastAPI
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Example Usage

**Form Input:**
- Destination: `Kyoto`
- Days: `5`
- Budget: `$1200`
- Travelers: `1`
- Interests: `food, temples, gardens`

**Real-Time Output (via SSE):**
```
⏳ Orchestrator: started
⏳ Destination Research: started
✅ Destination Research: completed (5.2s)
  → Season: Spring/Fall recommended; costs: lodging $80/night, meals $15 avg, transport $20/day
⏳ Itinerary Planning: started
✅ Itinerary Planning: completed (4.8s)
  → 5 days, Higashiyama/Arashiyama/Downtown, activities matched to interests
⏳ Budget Agent: started
✅ Budget Agent: completed (0.3s)
  → Estimated total: $850 USD (within budget!)
⏳ Synthesis: started
✅ Synthesis: completed (2.1s)
  → Final narrative ready

**Final Itinerary:**
Day 1 (Higashiyama): Temple exploration, traditional lunch → $45 USD
Day 2 (Arashiyama): Bamboo grove, kimono rental, street food → $55 USD
...
Total Cost: $850 USD | Status: ✅ Within Budget
```

If over budget, you'd see:
```
⏳ Conflict Resolution: started
  → Strategy: Replace 2 paid activities with free alternatives; suggest budget lodging option
⏳ Itinerary Planning (revision): started
✅ Itinerary Planning (revision): completed (4.1s)
⏳ Budget Agent (revalidation): started
✅ Budget Agent (revalidation): completed (0.2s)
  → Estimated total: $1180 USD (adjusted, still within budget!)
```

## Folder Structure

```
trip-planner/
├── Dockerfile                      # App image (python:3.12-slim)
├── docker-compose.yml              # App + Ollama services
├── requirements.txt                # Python dependencies
├── README.md                       # This file
├── app/
│   ├── main.py                     # FastAPI app, SSE /api/plan-trip/stream endpoint
│   ├── orchestrator.py             # Orchestration logic + JEV state machine
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── destination_research.py # Phi agent: research destination
│   │   ├── itinerary_planning.py   # Phi agent: plan day-by-day itinerary
│   │   ├── budget.py               # JEV + Python: calculate & validate budget
│   │   ├── conflict_resolution.py  # JEV strategy + optional Phi execution
│   │   └── synthesis.py            # Phi agent: redact final narrative
│   ├── models.py                   # Pydantic v2: SharedContext + sub-schemas
│   ├── llm_client.py               # Ollama HTTP client for Phi calls
│   ├── jev_engine.py               # JEV state machine implementation
│   └── guardrails.py               # Rate limiting, token budget, timeouts
├── static/
│   ├── index.html                  # Frontend form + agent trace panel
│   └── app.js                      # SSE connection, DOM updates
└── docs/
    └── Devlog.md                   # Challenge log + solutions (for blog post)
```

## Challenges & Solutions (Devlog)

See `docs/Devlog.md` for a chronicle of:
1. JEV integration challenges
2. Phi structured output reliability
3. SSE + shared context synchronization
4. Conflict loop convergence edge cases
5. Token counting accuracy
6. Latency perception in the UI
7. Error handling & recovery

This log is the foundation for the accompanying blog post.

## Guardrails

- **Rate Limiting:** 10 requests/hour per IP (in-memory)
- **Token Budget:** 8000 tokens max per session (summed across all Phi calls)
- **Hard Timeout:** 45 seconds per request (abort + return error)
- **Conflict Loop:** Max 2 iterations (prevents infinite loops)

## Cost Control

- **Ollama local:** No API costs, model runs on your hardware (CPU)
- **JEV:** No inference cost, pure computation
- **Observability:** Token usage logged per agent for transparency

## Future Enhancements (v2+)

- Integration with real flight/hotel APIs (Skyscanner, Booking.com)
- Persistent vector store of destination knowledge (pgvector)
- Multi-user concurrency with session management
- Frontend theming integration with `alexisalulema.com/demo-theme.css`
- Optional "About This Demo" panel (DEMO_INFO widget)

## References

- **Book Chapter:** "30 Agents Every AI Engineer Must Build", Chapter 7 (Imran Ahmad, Packt)
- **JEV Docs:** https://typesafe.ai/blog/introducing-system-one-models-and-jev
- **Pattern Name:** Chain-of-Agents Orchestrator + Memory-Augmented Multi-Agent System + Conflict Resolution Mechanism

## License

MIT (pending, align with personal-website)

## Contact

Built as a public demo for [alexisalulema.com](https://alexisalulema.com). Questions or feedback? Open an issue or reach out.

---

**Status:** 🚧 In Development  
**Target:** Ready for ephemeral deployment on `demoNN.alexisalulema.com` by end of sprint
