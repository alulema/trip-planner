# Hand-off manifest (para el mantenedor de la infra de demos)

Según `docs/DEMO_INTEGRATION.md` → "Lo que entregas a la infra".

| Campo | Valor |
|---|---|
| `projectId` (slug) | `trip-planner` |
| Nombre legible (ES / EN) | "Planificador de Viajes (Cadena de Agentes)" / "Trip Planner (Chain of Agents)" |
| Repo GitHub (público) | `https://github.com/alulema/trip-planner` |
| Imágenes GHCR + puertos | `ghcr.io/alulema/trip-planner:latest` → **8080** (único con ingress interno)<br>`ghcr.io/alulema/trip-planner-ollama:latest` → 11434 (solo intra-pod, Qwen 2.5 1.5B horneado) |
| `shareable` | `false` recomendado: es stateless, pero la inferencia en CPU se serializa (1 cadena a la vez); compartir el entorno haría esperar/rebotar ("busy") a otros visitantes |
| Secretos a inyectar | **Ninguno** (LLM local, sin APIs externas) |
| Recursos extra | Sidecar `ollama` en el mismo pod (patrón rag-blogposts). La app usa el default `OLLAMA_HOST=http://localhost:11434` |
| Sizing sugerido (tope 2 vCPU / 4 GiB) | `ollama` 1.75 vCPU / 3.0 GiB · `app` 0.25 vCPU / 1.0 GiB |
| Health / startup probe | `GET /api/health` (app lista en ~1–2 s; `llm_ready` pasa a `true` cuando Qwen termina de cargar) |

Notas:

- Guardrails de la app: 6000 tokens/viaje, 1 cadena concurrente, 10 viajes/h por IP, 30/h global,
  timeout 180 s. Tunables por env sin rebuild (tabla en `README.md`).
- La app lee `X-Forwarded-For` (primer valor) para el rate limit por IP.
- Variable opcional `OLLAMA_MODEL` si se cambia el modelo horneado (p.ej. `qwen2.5:0.5b-instruct`
  para más velocidad; requiere rebuild de la imagen ollama con `--build-arg OLLAMA_MODEL=...`).
- Cold start: la imagen ollama pesa ~2–3 GB; presupuestar el pull en la provisión.

## Glosario sugerido para el post técnico

| Término, tal como aparece en el post | Definición corta | Wikipedia |
|---|---|---|
| SSE (Server-Sent Events) | Estándar web para que el servidor envíe un flujo continuo de eventos al navegador sobre una única conexión HTTP, en un solo sentido. | https://en.wikipedia.org/wiki/Server-sent_events |
| Sistema multiagente | Sistema compuesto por varios agentes autónomos que interactúan o cooperan para resolver un problema que ninguno resolvería solo. | https://es.wikipedia.org/wiki/Sistema_multiagente |
| Salidas estructuradas (structured outputs) | Modo de un LLM en que la respuesta se restringe a un esquema JSON dado, de modo que siempre sea parseable. | https://en.wikipedia.org/wiki/JSON#Schema |
| Rate limiting | Técnica que limita cuántas solicitudes puede hacer un cliente en un intervalo de tiempo, para proteger un servicio de abuso o sobrecosto. | https://en.wikipedia.org/wiki/Rate_limiting |
| Motor de decisiones / System One | Modelo que, en lugar de generar texto, evalúa un estado y devuelve respuestas tipadas (una opción, un sí/no) con su probabilidad. | https://en.wikipedia.org/wiki/Thinking,_Fast_and_Slow |
| Ollama | Servidor open source para ejecutar modelos de lenguaje localmente, sin depender de una API externa. | https://es.wikipedia.org/wiki/Modelo_de_lenguaje_grande (sin página propia; enlace al concepto — verificar) |
