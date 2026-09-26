# Hand-off manifest (para el mantenedor de la infra de demos)

Según `docs/DEMO_INTEGRATION.md` → "Lo que entregas a la infra".

| Campo | Valor |
|---|---|
| `projectId` (slug) | `trip-planner` |
| Nombre legible (ES / EN) | "Planificador de Viajes (Cadena de Agentes)" / "Trip Planner (Chain of Agents)" |
| Repo GitHub (público) | `https://github.com/alulema/trip-planner` |
| Imagen GHCR + puerto | `ghcr.io/alulema/trip-planner:latest`, `8080` |
| `shareable` | `true` — stateless: cada request es independiente, sin estado entre sesiones |
| Secretos a inyectar | `ANTHROPIC_API_KEY` |
| Recursos extra | Ninguno (sin sidecars, sin DB). Un contenedor de **0.5 vCPU / 1 GiB** es suficiente |
| Health / startup probe | `GET /api/health` (arranque ~1–2 s) |

Notas:

- **Tope de gasto duro** en la consola de Anthropic recomendado (además de los guardrails de
  la app: 8000 tokens/sesión, 10 viajes/h por IP, 30/h global, 3 concurrentes, timeout 45 s).
- Si la imagen corre sin `ANTHROPIC_API_KEY`, arranca en **modo simulado** (sin costo) y lo
  indica en la UI — útil para validar la provisión antes de inyectar el secreto.
- La app lee `X-Forwarded-For` (primer valor) para el rate limit por IP; si el gateway no lo
  propaga, el límite aplica por IP del gateway (efectivamente global).
- Env opcionales para tunear sin rebuild: ver tabla en `README.md`.

## Glosario sugerido para el post técnico

| Término, tal como aparece en el post | Definición corta | Wikipedia |
|---|---|---|
| SSE (Server-Sent Events) | Estándar web para que el servidor envíe un flujo continuo de eventos al navegador sobre una única conexión HTTP, en un solo sentido. | https://en.wikipedia.org/wiki/Server-sent_events |
| Sistema multiagente | Sistema compuesto por varios agentes autónomos que interactúan o cooperan para resolver un problema que ninguno resolvería solo. | https://es.wikipedia.org/wiki/Sistema_multiagente |
| Salidas estructuradas (structured outputs) | Modo de un LLM en que la respuesta se restringe a un esquema JSON dado, de modo que siempre sea parseable. | https://en.wikipedia.org/wiki/JSON#Schema |
| Rate limiting | Técnica que limita cuántas solicitudes puede hacer un cliente en un intervalo de tiempo, para proteger un servicio de abuso o sobrecosto. | https://en.wikipedia.org/wiki/Rate_limiting |
