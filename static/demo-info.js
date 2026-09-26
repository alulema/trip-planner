// Content for the shared "About this demo" panel (engine: demo-panel.js, loaded after this).
// Rendered as text; only `diagram` is handed to Mermaid.

// Resolve the UI language before the deferred scripts run, so the panel (which reads
// <html lang> once at init) and app.js agree on it.
(function () {
  var lang = null;
  try { lang = localStorage.getItem("tp-lang"); } catch (_) {}
  if (lang !== "es" && lang !== "en") {
    lang = (navigator.language || "es").toLowerCase().indexOf("es") === 0 ? "es" : "en";
  }
  document.documentElement.lang = lang;
})();

window.DEMO_INFO = {
  trigger: "custom", // opened from the topbar button via window.DemoPanel.open()
  title: "Trip Planner — Chain-of-Agents Orchestrator",
  titleEs: "Trip Planner — Orquestador de Cadena de Agentes",
  overview:
    "Type a destination, number of days, budget and interests, and watch a chain of specialised AI agents hand work to each other in real time: research → itinerary → budget → conflict resolution → synthesis. The point isn't a pretty itinerary — it's making the orchestration visible: who is working, what was handed off, where a conflict (over budget) appeared and how the chain resolved it without human intervention.",
  overviewEs:
    "Escribe un destino, días, presupuesto e intereses, y mira en tiempo real cómo una cadena de agentes de IA especializados se pasan el trabajo: investigación → itinerario → presupuesto → resolución de conflictos → síntesis. El valor no es un itinerario bonito — es hacer visible la orquestación: quién trabaja, qué se entregó al siguiente, dónde apareció un conflicto (presupuesto excedido) y cómo la cadena lo resolvió sin intervención humana.",
  architecture: {
    description:
      "A deterministic orchestrator owns a single shared-context object (the chain's memory). Each agent reads the full context and returns only its own section; the orchestrator merges it and emits a trace event before and after every step, streamed to the browser over Server-Sent Events. The budget agent is plain Python arithmetic (no LLM). If the plan is over budget, the conflict-resolution agent proposes cuts that are fed back to the itinerary agent — a loop hard-capped at 2 iterations.",
    descriptionEs:
      "Un orquestador determinístico es dueño de un único objeto de contexto compartido (la memoria de la cadena). Cada agente lee el contexto completo y devuelve solo su sección; el orquestador la integra y emite un evento de trace antes y después de cada paso, transmitido al navegador por Server-Sent Events. El agente de presupuesto es aritmética en Python (sin LLM). Si el plan excede el presupuesto, el agente de resolución de conflictos propone recortes que se reinyectan al agente de itinerario — un ciclo con tope duro de 2 iteraciones.",
    diagram: `flowchart LR
  U["Browser"] -->|"SSE /api/plan-trip/stream"| O["Orchestrator<br/>(deterministic)"]
  O --> R["1 Destination research<br/>Claude Haiku"]
  R --> I["2 Itinerary planning<br/>Claude Haiku"]
  I --> B["3 Budget<br/>Python, no LLM"]
  B -->|over budget| C["4 Conflict resolution<br/>Claude Haiku"]
  C -->|"revise (max 2x)"| I
  B -->|within budget / cap reached| S["5 Synthesis<br/>Claude Haiku"]
  S --> O
  O -.->|trace events| U
  M[("shared_context<br/>append-only memory")] --- O`,
  },
  infra: [
    { name: "FastAPI app", role: "Single stateless container: serves the UI and streams the agent chain over SSE.", roleEs: "Un único contenedor stateless: sirve la UI y transmite la cadena de agentes por SSE." },
    { name: "Claude Haiku 4.5 (Anthropic API)", role: "LLM for the four language agents, with structured JSON outputs validated by Pydantic.", roleEs: "LLM de los cuatro agentes de lenguaje, con salidas JSON estructuradas validadas con Pydantic." },
    { name: "Session gateway", role: "Reverse proxy in front of the demo: TLS, session auth and routing. The app itself has no auth.", roleEs: "Reverse proxy delante del demo: TLS, auth de sesión y ruteo. La app no lleva auth." },
    { name: "GHCR", role: "Public container image, pulled fresh on each ephemeral provision.", roleEs: "Imagen pública, traída fresca en cada provisión efímera." },
  ],
  sizing:
    "Tiny footprint: no local model, so a single 0.5 vCPU / 1 GiB container is plenty. Cost is per token: ~4–6 Haiku calls per trip, capped at 8,000 tokens per session, 10 trips/hour per visitor, 3 concurrent runs and a 45 s hard timeout.",
  sizingEs:
    "Huella mínima: sin modelo local, un contenedor de 0.5 vCPU / 1 GiB basta. El costo es por token: ~4–6 llamadas a Haiku por viaje, con tope de 8.000 tokens por sesión, 10 viajes/hora por visitante, 3 ejecuciones concurrentes y timeout duro de 45 s.",
  design: [
    "Deterministic orchestrator: control flow is code, not an LLM — cheaper, predictable and easy to debug.",
    "Shared, append-only context: every agent reads everything and writes only its own section.",
    "Arithmetic is Python, not the model: the budget check can't hallucinate a sum.",
    "Bounded negotiation: at most 2 conflict iterations; if it still doesn't fit, the result says so honestly.",
    "Cost guardrails from day one: per-session token budget, rate limits, concurrency cap and hard timeout.",
  ],
  designEs: [
    "Orquestador determinístico: el flujo de control es código, no un LLM — más barato, predecible y fácil de depurar.",
    "Contexto compartido append-only: cada agente lee todo y solo escribe su propia sección.",
    "La aritmética es Python, no el modelo: el chequeo de presupuesto no puede alucinar una suma.",
    "Negociación acotada: máximo 2 iteraciones de conflicto; si aun así no alcanza, el resultado lo dice con honestidad.",
    "Guardrails de costo desde el día 1: presupuesto de tokens por sesión, rate limit, tope de concurrencia y timeout duro.",
  ],
  limitations: [
    "Prices are LLM estimates from general knowledge — no live flight/hotel APIs.",
    "Flights to the destination are not included; only on-the-ground costs.",
    "Trips are capped at 7 days to keep each run within the token budget.",
    "Ephemeral: nothing is stored; a session can end at any time.",
  ],
  limitationsEs: [
    "Los precios son estimaciones del LLM con conocimiento general — sin APIs de vuelos/hoteles en vivo.",
    "No incluye vuelos al destino; solo costos en el lugar.",
    "Viajes de hasta 7 días para mantener cada ejecución dentro del presupuesto de tokens.",
    "Efímero: no se guarda nada; la sesión puede terminar en cualquier momento.",
  ],
  links: { repo: "https://github.com/alulema/trip-planner" },
  lang: "auto",
};
