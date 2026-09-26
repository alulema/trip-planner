# Devlog — Trip Planner (Chain-of-Agents)

Bitácora interna y cronológica: decisiones, desafíos y cómo se resolvieron. El manual
público de réplica es el `README.md`; este archivo es la memoria para reconstruir el demo
y la base del futuro post del blog.

---

## 2026-09-26 — Sesión 1: base funcional de punta a punta

**Fuentes de verdad:** propuesta de diseño "Trip Planner — Demo de Chain-of-Agents
Orchestrator" + contrato `docs/DEMO_INTEGRATION.md` (copiado del repo del sitio, rama
`staging`).

### Decisiones

1. **Stack según la propuesta (Anthropic), no según el README inicial.** El README del
   commit inicial describía Phi 3.8B vía Ollama + JEV. La propuesta de diseño (más reciente
   y explícita) pide Claude Haiku 4.5 vía Anthropic API; se siguió la propuesta y se
   reescribió el README. Ventaja adicional: sin modelo local → imagen de ~225 MB y pod
   diminuto (0.5 vCPU / 1 GiB), en vez de un sidecar Ollama de varios GB.
2. **Orquestador determinístico (sin Sonnet en v1).** La propuesta lo permite para v1: más
   barato y fácil de depurar. `claude-sonnet-4-6` para el control queda como v2.
3. **Budget Agent sin LLM.** Aritmética en Python, nunca en el modelo.
4. **Extensión del contrato: `itinerary_draft.cost_assumptions`.** El contrato original solo
   tenía `estimated_cost_usd` por día, y el budget sumaba días + alojamiento de referencia.
   Problema: las acciones típicas de conflicto ("alojamiento más barato", "comer en
   mercados") no tenían dónde aplicarse → el loop solo podía recortar actividades. Solución:
   el planner se compromete con supuestos diarios (alojamiento/noche, comida/día,
   transporte/día, para todo el grupo) que puede bajar en cada revisión; el costo por día
   queda solo para actividades. Budget = lodging·días + food·días + transport·días + Σ
   actividades. Todo sigue siendo append-only.
5. **Unidades explícitas en `reference_costs`:** alojamiento = grupo completo por noche;
   comida = 1 comida, 1 persona; transporte = 1 persona por día. Evita que el LLM mezcle
   unidades.
6. **Structured outputs + validación Pydantic + 1 reintento.** Se usa `messages.create` con
   `output_config.format` (schema generado desde Pydantic con `anthropic.transform_schema`)
   y validación manual, en vez de `messages.parse`: `parse` lanza `ValidationError` antes de
   devolver el mensaje, y se perdía el `usage` del intento fallido (el presupuesto de tokens
   dejaba de ser honesto). Los modelos `*Output` no llevan restricciones numéricas para que
   el schema sea compatible; el saneamiento (no negativos, exactamente N días) se hace en
   Python (`itinerary_planning._normalize`).
7. **Modo mock.** Sin `ANTHROPIC_API_KEY` la app arranca con agentes simulados
   determinísticos (latencia configurable). Sirve para UI, para los tests y para que el
   README funcione "en cualquier parte" sin gastar.
8. **Todo resultado viaja como evento SSE**, incluidos errores de validación y rate limit
   (HTTP 200 + `event: error`): `EventSource` no expone el cuerpo de respuestas no-200. El
   cliente cierra el `EventSource` en `done`/`error` para evitar el auto-reconnect (cada
   reconexión sería otra ejecución pagada).
9. **Degradación honesta en vez de fallo:** si se agota el presupuesto de tokens o falla un
   LLM dentro del loop de conflicto, se conserva el último borrador válido, se registra en
   el trace (`skipped`/`failed`) y la síntesis cae a una plantilla determinística. Solo
   research/itinerario inicial son obligatorios (sin ellos → `chain_failed`).
10. **UI bilingüe (es/en)** con el idioma resuelto antes de cargar `demo-panel.js` (el panel
    lee `<html lang>` una sola vez al iniciar); el toggle recarga la página para que el panel
    cambie también. El idioma se pasa a los agentes (`lang`) para que el texto generado
    coincida.
11. **Panel "Acerca de"** con `trigger: "custom"` (botón en la topbar), contenido en
    `static/demo-info.js`. El botón se oculta si el script compartido no carga.

### Guardrails implementados (día 1, no TODO)

- Presupuesto de tokens por sesión (`MAX_TOKENS_PER_SESSION`, def. 8000): se verifica antes
  de cada llamada y `max_tokens` de cada llamada se recorta al remanente.
- Rate limit por IP (sliding window 1 h, `X-Forwarded-For`), límite global por hora y tope
  de concurrencia (3).
- Timeout duro de 45 s por cadena (`asyncio.timeout`) → error amigable.
- Loop de conflicto con tope duro de 2 iteraciones (clamp en config).
- Cancelación de la cadena si el cliente se desconecta.
- Log de tokens por agente por sesión (`trip_planner.orchestrator` INFO).

### Apagado por inactividad

La propuesta pedía reusar el mecanismo de `agentic-racing`. En la infra efímera el teardown
por inactividad/tope de vida lo hace la plataforma (gateway + reaper), no la app — la app
solo debe tolerar un corte abrupto (stateless, arranque en ~1 s). Por eso
`SESSION_IDLE_TIMEOUT_SECONDS` de la propuesta **no** se implementó en la app.

### Verificación

- 16 tests offline (pytest, modo mock): aritmética del budget, happy path, loop acotado
  ($50 → 2 iteraciones, `within_budget:false`), convergencia en 1 iteración, agotamiento de
  tokens con fallback, error obligatorio → `chain_failed`, fallo del LLM en el loop,
  reintento de JSON inválido con conteo de usage, guard de tokens antes de llamar, SSE
  end-to-end, input inválido, rate limit por IP, timeout duro.
- UI probada con Chromium headless (desktop + móvil) en modo mock; sin errores JS (solo los
  recursos externos del tema, bloqueados por el sandbox → se verificó el fallback de tokens).
- `docker build` + `docker run` OK (imagen 225 MB, usuario no-root, healthcheck OK, SSE OK
  dentro del contenedor).

### Pendiente / siguiente sesión

- [ ] **Probar con la API real** (en esta sesión no había `ANTHROPIC_API_KEY`): medir tokens
      reales por agente (objetivo < 8000 con 2 iteraciones de conflicto), latencia total vs.
      timeout de 45 s, calidad del JSON de Haiku.
- [ ] Ajustar `max_tokens` por agente según lo medido (itinerario: `250 + 170·días`).
- [ ] Pulir textos del trace (algunos mensajes del orquestador están en inglés fijo).
- [ ] Publicar la imagen (push a `main` dispara `.github/workflows/image.yml`) y marcar el
      package de GHCR como **Public**.
- [ ] Entregar el hand-off manifest (`docs/HANDOFF.md`) al mantenedor de la infra.
- [ ] Capturas del panel de trace para el post.
