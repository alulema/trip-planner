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

---

## 2026-09-26 — Sesión 2: pivote a Qwen 2.5 (Ollama) + motor de decisiones estilo JEV

### Decisiones

1. **Adiós Anthropic, hola Qwen 2.5 local.** Pedido explícito. Se descartó **Phi**: es un
   modelo de Microsoft y el contrato prohíbe tecnología Microsoft en el stack interno. Qwen 2.5
   ya está validado en la infra (rag-blogposts). Default `qwen2.5:1.5b-instruct` (0.5b es ~3×
   más rápido pero redacta peor; configurable con `OLLAMA_MODEL`).
2. **JEV detrás de una interfaz, implementado con reglas.** JEV (TypeSafe) es API alojada,
   pesos cerrados, early access con waitlist, sin self-host; y además su web/docs están
   bloqueados desde el sandbox. Se diseñó `app/decisions/` con su forma: estado + preguntas
   tipadas (`choice`/`score`) → respuestas con probabilidades. Cada pregunta trae `family`
   (para despachar reglas) y `text` en lenguaje natural (lo que evaluaría Jev). Hoy:
   `RulesDecisionEngine` (taxonomía de palabras clave es/en + heurísticas). Enchufar Jev =
   implementar `evaluate()`. Ojo con lo documentado de Jev: no hace aritmética ni genera texto →
   el presupuesto sigue en Python (el README original decía "Budget Agent (JEV + Python)", error).
3. **"Generar una vez, decidir muchas".** En CPU cada token cuesta segundos. Cambios:
   - El itinerario se genera **una sola vez** y trae `free_alternative` por día.
   - Resolución de conflictos ya **no usa LLM**: catálogo fijo de acciones
     (`free_alternatives`, `cheaper_lodging`, `street_food`, `transit_pass`); el código calcula
     el ahorro, el motor puntúa P(daña intereses), se ordena por `ahorro × (1 − P)` y se toman
     las mínimas que cierran la brecha (máx. 2 por iteración).
   - La revisión del itinerario la aplica **código** (`itinerary_planning.revise`), sin generar.
   - `cost_assumptions` ahora se derivan en código de los costos de referencia (antes los
     proponía el LLM).
   - Nuevo paso **intake**: clasifica intereses en la taxonomía (decisiones visibles en trace).
   - **Validación** tras cada revisión: "¿el plan sigue respetando los intereses?" (score).
     Útil y honesto: con el catálogo actual, cambiar actividades de pago por paseos gratuitos
     baja esa probabilidad (se ve en el trace).
   - Síntesis en **streaming token a token** (evento SSE `token`); progreso de generación JSON
     (evento `progress`) para que la espera en CPU no parezca colgada.
4. **Cliente Ollama propio sobre httpx** (sin SDK): `/api/chat` con `stream: true`, `format` =
   JSON schema de Pydantic con `$ref` inlineados (`inline_refs`), `num_predict` recortado al
   presupuesto de tokens, `keep_alive` 30m, 1 reintento si el JSON no valida. Warmup en
   segundo plano al arrancar (`llm_ready` en `/api/health` y `/api/config`).
5. **Guardrails re-calibrados para CPU:** 6000 tokens/viaje (prompt + generados: ambos cuestan
   tiempo), timeout 180 s, **1 cadena concurrente** (la inferencia se serializa).
6. **Dos imágenes:** app (`Dockerfile`) + `ollama/Dockerfile` con Qwen horneado (sin descarga
   al arrancar). `docker-compose.yml` para local; workflow publica ambas en GHCR.
   Números del modelo acotados a rangos sensatos en código (un 1.5B puede delirar precios).

### Verificación

- 34 tests offline: reglas (clasificación, daño, match), cadena (loop sin tokens, convergencia
  con la acción de mayor utilidad, acción dañina rankeada abajo, catálogo agotado, fallback por
  tokens y por fallo del LLM en síntesis), cliente Ollama contra un servidor falso NDJSON
  (streaming, usage, progreso, reintento, 404, conexión caída, guard de tokens), API/SSE.
- UI en Chromium (modo mock): decisiones con probabilidades, perfil de intereses, días
  "ajustado", resumen escribiéndose en vivo.
- **Integración con Ollama real** (contenedor `ollama/ollama:0.12.3` + app en contenedor): la app
  conecta, el warmup detecta el modelo faltante y la cadena falla limpia con `chain_failed`.
- **No se pudo descargar Qwen** en el sandbox: `registry.ollama.ai` y `huggingface.co` están
  bloqueados por la política de red. Tampoco se pudo construir `ollama/Dockerfile` aquí.

### Pendiente

- [ ] **Correr con Qwen real** (`docker compose up --build` en una máquina con red): medir
      latencia por paso en ~2 vCPU, tokens reales por viaje (objetivo < 6000), calidad del JSON
      del 1.5B (¿cumple exactamente N días?, ¿areas reales?) y ajustar prompts/`num_predict`.
- [ ] Validar que Ollama 0.12.x acepta el schema inlineado como `format` sin quejarse.
- [ ] Decidir 1.5b vs 0.5b según la latencia medida.
- [ ] Cuando haya acceso a Jev: `JevDecisionEngine.evaluate()` + `DECISION_ENGINE=jev`
      (necesita `TYPESAFE_API_KEY` como secreto y salida a la red).
- [ ] Ampliar catálogo de acciones (p.ej. reducir un día de actividades pagas en vez de todos).

---

## 2026-09-27 — Sesión 3: primer e2e con Qwen real (CI) y correcciones de calidad

### E2E en GitHub Actions (run #1)

Nuevo job `e2e`: construye la imagen ollama con Qwen 2.5 1.5B real, levanta el stack con
límites de pod (`docker-compose.ci.yml`: ollama 1.75 vCPU / 3 GiB, app 0.25 / 1 GiB) y corre
`scripts/smoke_e2e.py` (viajes reales por SSE; reporte en el job summary).

Mediciones (3 días): **~37 s por viaje** (research ~8 s, itinerario ~15 s, síntesis ~14 s;
primer token del resumen ~27–31 s), **~1.2k tokens/viaje** (límite 6000), JSON válido al primer
intento en todas las llamadas, carga del modelo < 1 s. Muy por debajo del timeout de 180 s.

Problemas de calidad encontrados:
1. **Síntesis inventó cifras** ("$366, which is within your budget of $50"). Grave: contradice
   el resultado honesto.
2. Costo de actividades **siempre $0** (sesgo por "Use 0 if everything is free" en el prompt).
3. Costos de referencia **idénticos** para Kioto y Lisboa (120/8/5).
4. "Zonas" que no son barrios (Shirakawa-go para Kioto; "Baixa - the historic heart…").

### Correcciones

1. Síntesis en dos partes: **narrativa** (Qwen, sin ver ningún número, prohibido hablar de
   dinero; red de seguridad que elimina frases con montos/presupuesto) + **párrafo de
   presupuesto escrito por código** (total, recortes aplicados, si encaja o no). Mismo principio
   que "la aritmética es código": la parte factual no puede alucinarse.
2. Prompt de itinerario: costo realista por día (rangos típicos), sin la frase del 0.
3. Prompt de research: rangos orientativos por costo de vida; barrios dentro de la ciudad,
   solo nombre. `clean_area()` recorta descripciones (" - ", paréntesis, comas).
4. E2E endurecido: **falla** si el resumen contradice el veredicto del presupuesto; warnings si
   todos los días cuestan $0, si las zonas parecen descripciones o si los costos de referencia
   son idénticos entre destinos. Tercer escenario (Hanói, destino barato) para ver variación.
5. Tests nuevos: narrativa "mentirosa" filtrada y párrafo de presupuesto veraz; `clean_area`.
