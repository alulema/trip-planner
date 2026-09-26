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
    "Type a destination, days, budget and interests, and watch a chain of specialised agents hand work to each other in real time: intake → research → itinerary → budget → conflict resolution → synthesis. Fully self-hosted: a small local LLM (Qwen 2.5 via Ollama) writes only what must be written, and a non-generative decision engine takes the typed decisions — with their probabilities visible in the trace.",
  overviewEs:
    "Escribe un destino, días, presupuesto e intereses, y mira en tiempo real cómo una cadena de agentes especializados se pasa el trabajo: intake → investigación → itinerario → presupuesto → resolución de conflictos → síntesis. Totalmente autohospedado: un LLM local pequeño (Qwen 2.5 vía Ollama) escribe solo lo que hay que escribir, y un motor de decisiones no generativo toma las decisiones tipadas — con sus probabilidades visibles en el trace.",
  architecture: {
    description:
      "A deterministic orchestrator owns a single shared-context object (the chain's memory); each agent returns only its own section and the orchestrator merges it, emitting trace events streamed over SSE. Reasoning is split: generative steps (research, the itinerary — once — and the final narrative) run on Qwen 2.5; typed decisions (interest categories, how much each budget cut hurts the traveller, whether the revised plan still fits) go to a System-One-style decision engine; arithmetic is plain Python. When over budget, code computes each preset action's savings, the engine scores its harm, and code applies the best ones — no regeneration, loop capped at 2.",
    descriptionEs:
      "Un orquestador determinístico es dueño de un único objeto de contexto compartido (la memoria de la cadena); cada agente devuelve solo su sección y el orquestador la integra, emitiendo eventos de trace por SSE. El razonamiento se divide: los pasos generativos (investigación, el itinerario — una sola vez — y la narrativa final) corren en Qwen 2.5; las decisiones tipadas (categoría de cada interés, cuánto daña cada recorte al viajero, si el plan revisado aún encaja) van a un motor de decisiones estilo System One; la aritmética es Python. Si excede el presupuesto, el código calcula el ahorro de cada acción preestablecida, el motor puntúa su daño y el código aplica las mejores — sin regenerar, con tope de 2 iteraciones.",
    diagram: `flowchart LR
  U["Browser"] -->|"SSE"| O["Orchestrator<br/>(deterministic)"]
  O --> K["0 Intake<br/>decisions"]
  K --> R["1 Research<br/>Qwen 2.5"]
  R --> I["2 Itinerary<br/>Qwen 2.5 (once)"]
  I --> B["3 Budget<br/>Python"]
  B -->|over budget| C["4 Conflicts<br/>decisions + Python"]
  C -->|"revise by code (max 2x)"| B
  B -->|fits / cap reached| S["5 Synthesis<br/>Qwen 2.5 (streamed)"]
  O -.->|"trace + decisions"| U
  subgraph POD["Ephemeral pod"]
    O
    L[("Ollama<br/>Qwen 2.5 1.5B")]
  end
  R --- L
  I --- L
  S --- L`,
  },
  infra: [
    { name: "FastAPI app", role: "Stateless: serves the UI, runs the orchestrator and the rule-based decision engine, streams everything over SSE.", roleEs: "Stateless: sirve la UI, corre el orquestador y el motor de decisiones por reglas, y transmite todo por SSE." },
    { name: "Ollama — Qwen 2.5 1.5B", role: "Local LLM baked into its image; structured JSON generation and the streamed narrative. No external API.", roleEs: "LLM local horneado en su imagen; generación de JSON estructurado y la narrativa en streaming. Sin API externa." },
    { name: "Decision engine (rules)", role: "Typed decisions with probabilities (choice / score), Jev-style interface; keyword taxonomy + heuristics today.", roleEs: "Decisiones tipadas con probabilidades (choice / score), interfaz estilo Jev; hoy taxonomía de palabras clave + heurísticas." },
    { name: "Session gateway", role: "Reverse proxy in front of the demo: TLS, session auth and routing. The app itself has no auth.", roleEs: "Reverse proxy delante del demo: TLS, auth de sesión y ruteo. La app no lleva auth." },
    { name: "GHCR", role: "Public images (app / ollama), pulled fresh on each ephemeral provision.", roleEs: "Imágenes públicas (app / ollama), traídas frescas en cada provisión efímera." },
  ],
  sizing:
    "One ephemeral pod, 2 vCPU / 4 GiB, CPU only, biased to inference (Ollama ~1.75 vCPU / 3 GiB). Generation is kept to ~3 calls per trip; the conflict loop generates nothing. Guardrails: 6,000 tokens per trip, one chain at a time, 10 trips/hour per visitor, 180 s hard timeout.",
  sizingEs:
    "Un pod efímero, 2 vCPU / 4 GiB, solo CPU, sesgado a la inferencia (Ollama ~1.75 vCPU / 3 GiB). La generación se limita a ~3 llamadas por viaje; el ciclo de conflicto no genera nada. Guardrails: 6.000 tokens por viaje, una cadena a la vez, 10 viajes/hora por visitante, timeout duro de 180 s.",
  design: [
    "Generate once, decide many times: a CPU model writes only what must be written; everything else is a typed decision or arithmetic.",
    "Decision engine behind a Jev-style interface (state + typed questions → answers with probabilities); rule-based today, swappable.",
    "Arithmetic is Python, not the model: budget sums and savings can't be hallucinated.",
    "Deterministic orchestrator and append-only shared context: every step is visible, reproducible and cheap.",
    "Bounded, honest negotiation: at most 2 conflict iterations; if it still doesn't fit, the result says so.",
  ],
  designEs: [
    "Generar una vez, decidir muchas: un modelo en CPU escribe solo lo necesario; todo lo demás es una decisión tipada o aritmética.",
    "Motor de decisiones detrás de una interfaz estilo Jev (estado + preguntas tipadas → respuestas con probabilidades); hoy por reglas, intercambiable.",
    "La aritmética es Python, no el modelo: sumas y ahorros del presupuesto no pueden alucinarse.",
    "Orquestador determinístico y contexto compartido append-only: cada paso es visible, reproducible y barato.",
    "Negociación acotada y honesta: máximo 2 iteraciones de conflicto; si aun así no alcanza, el resultado lo dice.",
  ],
  limitations: [
    "CPU inference: roughly a minute or more per trip, mostly the itinerary generation.",
    "Prices are estimates from a 1.5B model's general knowledge (clamped to sane ranges) — no live flight/hotel data.",
    "Small model: wording or chosen areas are occasionally rough.",
    "Rule-based decisions only understand the keywords of their taxonomy (es/en); anything else maps to 'other'.",
    "Ephemeral: nothing is stored; a session can end at any time.",
  ],
  limitationsEs: [
    "Inferencia en CPU: aproximadamente un minuto o más por viaje, sobre todo la generación del itinerario.",
    "Los precios son estimaciones del conocimiento general de un modelo 1.5B (acotadas a rangos sensatos) — sin datos en vivo de vuelos/hoteles.",
    "Modelo pequeño: la redacción o las zonas elegidas a veces son toscas.",
    "Las decisiones por reglas solo entienden las palabras clave de su taxonomía (es/en); lo demás cae en 'other'.",
    "Efímero: no se guarda nada; la sesión puede terminar en cualquier momento.",
  ],
  links: { repo: "https://github.com/alulema/trip-planner" },
  lang: "auto",
};
