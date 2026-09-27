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
    "Type a destination, days, budget and interests, and watch a chain of specialised agents hand work to each other in real time: intake → live data → research → itinerary → budget → conflict resolution → synthesis. The reasoning is self-hosted: a small local LLM (Qwen 2.5 via Ollama) writes only what must be written, and a non-generative decision engine takes the typed decisions — with their probabilities visible in the trace. Changing facts come from free public sources: the real weather for your dates (Open-Meteo), districts and places from Wikidata/OpenStreetMap, and the exchange rate (ECB).",
  overviewEs:
    "Escribe un destino, días, presupuesto e intereses, y mira en tiempo real cómo una cadena de agentes especializados se pasa el trabajo: intake → datos en vivo → investigación → itinerario → presupuesto → resolución de conflictos → síntesis. El razonamiento es autohospedado: un LLM local pequeño (Qwen 2.5 vía Ollama) escribe solo lo que hay que escribir, y un motor de decisiones no generativo toma las decisiones tipadas — con sus probabilidades visibles en el trace. Los hechos que cambian vienen de fuentes públicas gratuitas: el clima real de tus fechas (Open-Meteo), barrios y lugares de Wikidata/OpenStreetMap y el tipo de cambio (BCE).",
  architecture: {
    description:
      "A LangGraph state graph orchestrates the chain: its state is a single shared-context object (the chain's memory), each node runs one agent and returns only its own section, and plain-code routing decides the next node, including the bounded budget loop. Nodes stream trace events live over SSE. A live-data node (no LLM) fetches weather, places and the exchange rate in parallel, each with a timeout and a fallback to the catalog or the model. Reasoning is split: generative steps (research, the itinerary — once — and the final narrative) run on Qwen 2.5; typed decisions (interest categories, how much each budget cut hurts the traveller, whether the revised plan still fits) go to a System-One-style decision engine; arithmetic is plain Python. When over budget, code computes each preset action's savings, the engine scores its harm, and code applies the best ones — no regeneration, loop capped at 2.",
    descriptionEs:
      "Un grafo de estado de LangGraph orquesta la cadena: su estado es un único objeto de contexto compartido (la memoria de la cadena), cada nodo ejecuta un agente y devuelve solo su sección, y un ruteo en código decide el siguiente nodo, incluido el ciclo acotado de presupuesto. Los nodos emiten eventos de trace en vivo por SSE. Un nodo de datos en vivo (sin LLM) consulta clima, lugares y tipo de cambio en paralelo, cada uno con timeout y respaldo al catálogo o al modelo. El razonamiento se divide: los pasos generativos (investigación, el itinerario — una sola vez — y la narrativa final) corren en Qwen 2.5; las decisiones tipadas (categoría de cada interés, cuánto daña cada recorte al viajero, si el plan revisado aún encaja) van a un motor de decisiones estilo System One; la aritmética es Python. Si excede el presupuesto, el código calcula el ahorro de cada acción preestablecida, el motor puntúa su daño y el código aplica las mejores — sin regenerar, con tope de 2 iteraciones.",
    diagram: `flowchart LR
  U["Browser"] -->|"SSE"| O["Orchestrator<br/>(deterministic)"]
  O --> K["0 Intake<br/>decisions"]
  K --> D["Live data<br/>weather · places · fx"]
  D --> R["1 Research<br/>catalog / OSM + Qwen"]
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
  S --- L
  D -.-> X[("Open-Meteo · Wikidata/OSM<br/>ECB rates")]`,
  },
  infra: [
    { name: "FastAPI app", role: "Stateless: serves the UI, runs the chain and the rule-based decision engine, streams everything over SSE.", roleEs: "Stateless: sirve la UI, ejecuta la cadena y el motor de decisiones por reglas, y transmite todo por SSE." },
    { name: "LangGraph", role: "Orchestration only: state graph, reducers for the append-only memory, conditional edges for the budget loop, custom stream for live events.", roleEs: "Solo orquestación: grafo de estado, reducers para la memoria append-only, aristas condicionales para el ciclo de presupuesto y stream propio para eventos en vivo." },
    { name: "Ollama — Qwen 2.5 1.5B", role: "Local LLM baked into its image; structured JSON generation and the streamed narrative. No external API.", roleEs: "LLM local horneado en su imagen; generación de JSON estructurado y la narrativa en streaming. Sin API externa." },
    { name: "Decision engine (rules)", role: "Typed decisions with probabilities (choice / score), Jev-style interface; keyword taxonomy + heuristics today.", roleEs: "Decisiones tipadas con probabilidades (choice / score), interfaz estilo Jev; hoy taxonomía de palabras clave + heurísticas." },
    { name: "Live data sources", role: "Open-Meteo (forecast / historical weather), Wikidata with OpenStreetMap as fallback (districts and notable places), Frankfurter/ECB exchange rates. Free, no key; optional with fallbacks.", roleEs: "Open-Meteo (pronóstico / clima histórico), Wikidata con OpenStreetMap de respaldo (barrios y lugares notables), tipos de cambio del BCE vía Frankfurter. Gratis, sin key; opcionales, con respaldo." },
    { name: "Session gateway", role: "Reverse proxy in front of the demo: TLS, session auth and routing. The app itself has no auth.", roleEs: "Reverse proxy delante del demo: TLS, auth de sesión y ruteo. La app no lleva auth." },
    { name: "GHCR", role: "Public images (app / ollama), pulled fresh on each ephemeral provision.", roleEs: "Imágenes públicas (app / ollama), traídas frescas en cada provisión efímera." },
  ],
  sizing:
    "One ephemeral pod, 2 vCPU / 4 GiB, CPU only, biased to inference (Ollama ~1.75 vCPU / 3 GiB). Generation is kept to ~3 calls per trip; the conflict loop generates nothing. Guardrails: 6,000 tokens per trip, one chain at a time, 10 trips/hour per visitor, 180 s hard timeout.",
  sizingEs:
    "Un pod efímero, 2 vCPU / 4 GiB, solo CPU, sesgado a la inferencia (Ollama ~1.75 vCPU / 3 GiB). La generación se limita a ~3 llamadas por viaje; el ciclo de conflicto no genera nada. Guardrails: 6.000 tokens por viaje, una cadena a la vez, 10 viajes/hora por visitante, timeout duro de 180 s.",
  design: [
    "Generate once, decide many times: a CPU model writes only what must be written; everything else is a typed decision or arithmetic.",
    "Live facts first: real weather for the travel dates, Wikidata/OpenStreetMap places outside the catalog, and the exchange rate — each shown with its source and fetch time, and written into the text by code, never by the model.",
    "Facts as data: for ~45 popular cities, real districts, highlights and cost levels come from a curated catalog; the small model only writes prose around them.",
    "Decision engine behind a Jev-style interface (state + typed questions → answers with probabilities); rule-based today, swappable.",
    "Arithmetic is Python, not the model: budget sums and savings can't be hallucinated.",
    "The model's prose is checked by code: sentences about money or naming places that aren't in the plan are dropped.",
    "Deterministic orchestrator and append-only shared context: every step is visible, reproducible and cheap.",
    "Bounded, honest negotiation: at most 2 conflict iterations; if it still doesn't fit, the result says so.",
  ],
  designEs: [
    "Generar una vez, decidir muchas: un modelo en CPU escribe solo lo necesario; todo lo demás es una decisión tipada o aritmética.",
    "Primero los hechos en vivo: clima real de las fechas del viaje, lugares de Wikidata/OpenStreetMap fuera del catálogo y tipo de cambio — cada uno con su fuente y hora de consulta, y escrito en el texto por código, nunca por el modelo.",
    "Hechos como datos: para ~45 ciudades populares, barrios reales, imperdibles y nivel de costos vienen de un catálogo curado; el modelo pequeño solo redacta alrededor.",
    "Motor de decisiones detrás de una interfaz estilo Jev (estado + preguntas tipadas → respuestas con probabilidades); hoy por reglas, intercambiable.",
    "La aritmética es Python, no el modelo: sumas y ahorros del presupuesto no pueden alucinarse.",
    "La prosa del modelo la revisa el código: se descartan frases sobre dinero o que nombran lugares que no están en el plan.",
    "Orquestador determinístico y contexto compartido append-only: cada paso es visible, reproducible y barato.",
    "Negociación acotada y honesta: máximo 2 iteraciones de conflicto; si aun así no alcanza, el resultado lo dice.",
  ],
  limitations: [
    "CPU inference: ~35–40 s per 3-day trip, mostly the itinerary and the narrative.",
    "Weather and exchange rates are live; lodging, food and activity prices are estimates (catalog or model) — no live flight/hotel data.",
    "Beyond ~16 days there is no forecast: the same dates of an earlier year are shown as a reference.",
    "Outside the catalog, districts come from Wikidata/OpenStreetMap; if it has too little data, the small model may pick wrong districts or invent places.",
    "Rule-based decisions only understand the keywords of their taxonomy (es/en); anything else maps to 'other'.",
    "Ephemeral: nothing is stored; a session can end at any time.",
  ],
  limitationsEs: [
    "Inferencia en CPU: ~35–40 s por viaje de 3 días, sobre todo el itinerario y la narrativa.",
    "Clima y tipo de cambio son en vivo; alojamiento, comida y actividades son estimaciones (catálogo o modelo) — sin datos en vivo de vuelos/hoteles.",
    "Más allá de ~16 días no hay pronóstico: se muestran las mismas fechas de un año anterior como referencia.",
    "Fuera del catálogo, los barrios vienen de Wikidata/OpenStreetMap; si tiene pocos datos, el modelo pequeño puede elegir barrios equivocados o inventar lugares.",
    "Las decisiones por reglas solo entienden las palabras clave de su taxonomía (es/en); lo demás cae en 'other'.",
    "Efímero: no se guarda nada; la sesión puede terminar en cualquier momento.",
  ],
  links: { repo: "https://github.com/alulema/trip-planner" },
  lang: "auto",
};
