# Hand-off manifest (para el mantenedor de la infra de demos)

Según `docs/DEMO_INTEGRATION.md` → "Lo que entregas a la infra".

| Campo | Valor |
|---|---|
| `projectId` (slug) | `trip-planner` |
| Nombre legible (ES / EN) | "Planificador de Viajes (Cadena de Agentes)" / "Trip Planner (Chain of Agents)" |
| Repo GitHub (público) | `https://github.com/alulema/trip-planner` |
| Imágenes GHCR + puertos | `ghcr.io/alulema/trip-planner:latest` → **8080** (único con ingress interno)<br>`ghcr.io/alulema/trip-planner-ollama:latest` → 11434 (solo intra-pod, Qwen 2.5 1.5B horneado) |
| `shareable` | `false` recomendado: es stateless, pero la inferencia en CPU se serializa (1 cadena a la vez); compartir el entorno haría esperar/rebotar ("busy") a otros visitantes |
| Secretos a inyectar | **Ninguno** (LLM local; las fuentes de datos en vivo son públicas y sin API key) |
| Salida a la red (egress) | El contenedor `app` necesita **HTTPS saliente** a `geocoding-api.open-meteo.com`, `api.open-meteo.com`, `archive-api.open-meteo.com`, `overpass-api.de`, `overpass.private.coffee`, `api.frankfurter.dev` y `open.er-api.com`. Es opcional: sin egress la cadena termina igual (catálogo + modelo, con el motivo en el trace). `LIVE_DATA=off` omite las llamadas. `ollama` no necesita egress |
| Recursos extra | Sidecar `ollama` en el mismo pod (patrón rag-blogposts). La app usa el default `OLLAMA_HOST=http://localhost:11434` |
| Sizing sugerido (tope 2 vCPU / 4 GiB) | `ollama` 1.75 vCPU / 3.0 GiB · `app` 0.25 vCPU / 1.0 GiB |
| Health / startup probe | `GET /api/health` (app lista en ~1–2 s; `llm_ready` pasa a `true` cuando Qwen termina de cargar) |

Notas:

- Guardrails de la app: 6000 tokens/viaje, 1 cadena concurrente, 10 viajes/h por IP, 30/h global,
  timeout 180 s. Tunables por env sin rebuild (tabla en `README.md`).
- Rate limit por IP: la app usa `CF-Connecting-IP` (lo fija Cloudflare) y, si falta, el primer valor de `X-Forwarded-For`.
- Variable opcional `OLLAMA_MODEL` si se cambia el modelo horneado (p.ej. `qwen2.5:0.5b-instruct`
  para más velocidad; requiere rebuild de la imagen ollama con `--build-arg OLLAMA_MODEL=...`).
- Datos en vivo: clima (Open-Meteo, CC BY 4.0, uso no comercial), lugares (OpenStreetMap, ODbL) y tipo de
  cambio (BCE vía Frankfurter; ExchangeRate-API de respaldo). La UI muestra la atribución de cada fuente.
  Timeouts de 8–12 s por llamada y caché en memoria; los rate limits de la app mantienen el uso muy bajo.
- Cold start: la imagen ollama pesa ~2–3 GB; presupuestar el pull en la provisión.

## Glosario sugerido para el post técnico

| Término, tal como aparece en el post | Definición corta | Wikipedia |
|---|---|---|
| SSE (Server-Sent Events) | Estándar web para que el servidor envíe un flujo continuo de eventos al navegador sobre una única conexión HTTP, en un solo sentido. | https://en.wikipedia.org/wiki/Server-sent_events |
| Sistema multiagente | Sistema compuesto por varios agentes autónomos que interactúan o cooperan para resolver un problema que ninguno resolvería solo. | https://es.wikipedia.org/wiki/Sistema_multiagente |
| Salidas estructuradas (structured outputs) | Modo de un LLM en que la respuesta se restringe a un esquema JSON dado, de modo que siempre sea parseable. | https://en.wikipedia.org/wiki/JSON#Schema |
| Rate limiting | Técnica que limita cuántas solicitudes puede hacer un cliente en un intervalo de tiempo, para proteger un servicio de abuso o sobrecosto. | https://en.wikipedia.org/wiki/Rate_limiting |
| Motor de decisiones / System One | Modelo que, en lugar de generar texto, evalúa un estado y devuelve respuestas tipadas (una opción, un sí/no) con su probabilidad. | https://en.wikipedia.org/wiki/Thinking,_Fast_and_Slow |
| Ollama | Servidor open source para ejecutar modelos de lenguaje localmente, sin depender de una API externa. | https://en.wikipedia.org/wiki/Ollama |
| Geocodificación | Conversión de un nombre de lugar ("Valparaíso") en coordenadas geográficas. | https://en.wikipedia.org/wiki/Address_geocoding |
| OpenStreetMap | Mapa colaborativo y abierto del mundo; sus datos se consultan con la API Overpass. | https://en.wikipedia.org/wiki/OpenStreetMap |
| Wikidata | Base de conocimiento libre y estructurada de la Fundación Wikimedia; aquí sirve para quedarse solo con lugares notables. | https://en.wikipedia.org/wiki/Wikidata |
| LangGraph | Biblioteca open source para orquestar agentes de LLM como un grafo de estados: nodos, aristas condicionales y un estado compartido. | https://en.wikipedia.org/wiki/LangChain (sin página propia; el artículo de LangChain la menciona) |
