# Devlog: Trip Planner (Chain-of-Agents)

Bitácora interna del demo. Tiene tres partes:

1. **Estado final**: qué es y cómo está implementado hoy (`main` @ `223bd7e` + fase 1 de datos en vivo).
2. **Desafíos superados**: cada problema real que apareció, por qué ocurrió, cómo se resolvió, dónde vive la solución y con qué evidencia se validó.
3. **Cronología**: sesiones, commits y PRs, para reconstruir el camino.

El manual público de réplica es el `README.md`. Este archivo es la memoria del proyecto y la materia prima del post del blog.

---

## 1. Estado final

### Qué es

Una demo pública del patrón **Chain-of-Agents Orchestrator** (cap. 7 de *"30 Agents Every AI Engineer Must Build"*, Imran Ahmad), junto con *Memory-Augmented Multi-Agent Systems* y *Conflict Resolution Mechanisms*. El usuario pide un viaje ("Kioto, 3 días, $2500, comida y templos") y ve en vivo cómo una cadena de agentes se pasa el trabajo:

- qué agente trabaja en cada momento;
- qué decisiones toma, con su probabilidad;
- cuándo aparece un conflicto de presupuesto y cómo se resuelve sin intervención humana.

El razonamiento corre **local**: un LLM pequeño (Qwen 2.5 1.5B vía Ollama) más un motor de decisiones no generativo. Los hechos que cambian (clima de las fechas del viaje, barrios y lugares fuera del catálogo, tipo de cambio) vienen de **fuentes públicas gratuitas y sin API key**, todas opcionales. No hay secretos.

### Principio de diseño: generar una vez, decidir muchas

Un modelo de 1.5B en CPU produce pocos tokens por segundo y se equivoca en hechos. Por eso cada paso usa el mecanismo más barato que lo resuelve bien:

| Paso | Mecanismo | Qué hace |
|---|---|---|
| Intake | Decisión tipada | Clasifica cada interés en una taxonomía fija (comida, religión/patrimonio, museos…) |
| Datos en vivo | APIs públicas, sin LLM | Geocodifica el destino y consulta en paralelo clima (Open-Meteo), lugares (OpenStreetMap, solo fuera del catálogo) y tipo de cambio (BCE) |
| Investigación | En vivo + catálogo + generativo | La nota de clima la escribe el código con los datos reales. Ciudad en catálogo: barrios, atracciones y costos curados (con clima real, **cero generación**). Fuera del catálogo: barrios y lugares de OpenStreetMap y Qwen solo estima costos; sin OSM, Qwen estima todo. |
| Itinerario | Generativo, **una sola vez** | El código decide qué barrio visita cada día y con qué atracciones; Qwen redacta las actividades. |
| Presupuesto | Código | Alojamiento + comida + actividades + transporte, comparado con el presupuesto |
| Resolución de conflictos | Decisiones + código | El código calcula el ahorro de cada acción preestablecida; el motor puntúa P(daña los intereses); se elige por `ahorro × (1 − P)` |
| Revisión del itinerario | Código | Aplica los recortes (paseo gratuito del barrio, bajar costos diarios) **sin regenerar** |
| Validación | Decisión tipada | "¿El plan revisado sigue respetando los intereses?" (probabilidad visible en el trace) |
| Síntesis | Generativo + código | Qwen escribe la narrativa (sin ver cifras, filtrada por código); el código escribe el párrafo de presupuesto |

### Orquestación: grafo de LangGraph

`app/orchestrator.py` (`langgraph==1.2.12`) es un `StateGraph` cuyo estado es el propio `SharedContext`:

```
START → intake → live_data → destination_research → itinerary_planning → budget
budget ──(excede y quedan iteraciones)──▶ conflict_resolution
budget ──(cabe, o se alcanzó el tope)──▶ synthesis
conflict_resolution ──(hay recortes)──▶ revise_itinerary → budget
conflict_resolution ──(catálogo agotado)──▶ synthesis
synthesis → finish → END
```

- **Nodos:** un agente por nodo. Cada nodo devuelve **solo su sección** y el grafo la integra.
- **Reducers:** `trace` (`operator.add`) y `token_usage` (`add_usage`, suma por agente) acumulan. La memoria compartida es append-only.
- **Ruteo:** `after_budget` y `after_conflict` son funciones de código, no un LLM. El ciclo de conflicto tiene un tope duro de 2 iteraciones (clamp en la config).
- **Dependencias por ejecución:** el cliente LLM, el motor de decisiones, los proveedores de datos en vivo y el presupuesto de tokens viajan en el *runtime context* (`context_schema=Deps`), no en el estado.
- **Eventos en vivo:** los nodos emiten `trace`, `section`, `progress` y `token` con el stream `custom` de LangGraph. `Orchestrator.run` los reenvía por SSE a medida que ocurren.
- **Diagrama:** `GET /api/graph` devuelve el Mermaid que LangGraph genera del grafo compilado.
- **Alcance:** LangGraph se usa **solo para orquestar**. El modelo se llama con un cliente Ollama propio, sin wrappers de LangChain.

### Componentes

| Archivo | Responsabilidad |
|---|---|
| `app/orchestrator.py` | Grafo LangGraph, nodos, ruteo, reenvío de eventos |
| `app/models.py` | `SharedContext` (contrato y estado del grafo), secciones, esquemas de salida del LLM |
| `app/live/` | Interfaces `Geocoder`, `WeatherProvider`, `PlacesProvider`, `FxProvider`; implementaciones Open-Meteo, Overpass (OSM + Wikidata), Frankfurter/ExchangeRate-API; `Http` con timeout y caché TTL; `mock` |
| `app/agents/live_data.py` | Paso de datos en vivo: llamadas en paralelo con timeout, errores no fatales, resumen de clima escrito por código |
| `app/agents/destination_research.py` | Caminos catálogo, OSM (`source="live"`, el modelo solo estima costos) y modelo; nota de clima real o de temporada; límites a números del modelo; `clean_area` |
| `app/agents/itinerary_planning.py` | `day_plan()` (barrio + atracciones por día), generación única, normalización (impone el barrio), `revise()` por código, alternativa gratuita por código |
| `app/agents/budget.py` | Aritmética del presupuesto |
| `app/agents/conflict_resolution.py` | Ahorros por código, daño por decisiones, selección por utilidad |
| `app/agents/interests.py` | Intake (clasificación) y validación del plan |
| `app/agents/synthesis.py` | Narrativa en streaming, `clean_narrative` (markdown, dinero, lugares desconocidos, frase cortada), párrafo de presupuesto |
| `app/decisions/` | Interfaz tipo JEV (`TypedQuestion` → `Answer` con probabilidades), `RulesDecisionEngine`, taxonomía y catálogo de acciones |
| `app/catalog.py` + `app/data/cities.json` | 45 ciudades: 3 barrios cada una, 269 atracciones asociadas a su barrio (152 gratuitas), costos aproximados, alias es/en y tolerancia a erratas |
| `app/llm_client.py` | Cliente Ollama sobre httpx: streaming, `format` = JSON schema, 1 reintento, conteo de tokens, warmup; `MockLLM` |
| `app/guardrails.py` | Rate limit por IP y global, concurrencia, presupuesto de tokens |
| `app/main.py` | FastAPI: UI, `/api/plan-trip/stream` (SSE), `/api/health`, `/api/config`, `/api/graph` |
| `static/` | UI sin framework: trace en vivo, decisiones con probabilidad, itinerario progresivo, resumen en streaming, panel "Acerca de" |
| `scripts/smoke_e2e.py` | E2E con modelo real: 4 viajes, latencias, tokens, métricas de calidad y chequeo de honestidad |

### Protocolo SSE (`GET /api/plan-trip/stream`)

| Evento | Contenido |
|---|---|
| `session` | id de sesión y modo del LLM |
| `trace` | cada transición de paso y cada decisión |
| `section` | una sección del contexto cuando cambia |
| `progress` | tokens generados durante una salida JSON |
| `token` | texto de la narrativa en streaming |
| `done` / `error` | contexto final / `{code, message}` |

Todo resultado, incluidos los errores de validación y los rechazos por límites, viaja como evento: `EventSource` no expone el cuerpo de respuestas no-200. El cliente cierra la conexión tras `done` o `error` para evitar la reconexión automática.

### Guardrails

- 6000 tokens por viaje (prompt + generados, porque en CPU ambos cuestan tiempo).
- 1 cadena concurrente, porque la inferencia se serializa.
- 10 viajes por hora por IP y 30 por hora en total.
- Timeout duro de 180 s.
- Cancelación de la cadena si el cliente se desconecta.
- Tope de 2 iteraciones en el ciclo de conflicto.
- Datos en vivo: timeout por llamada (3 s de conexión, 6 s en total; 10 s por instancia de Overpass), un reintento (Overpass: segunda instancia pública), tope de 22 s por proveedor, caché en memoria (clima 1 h, tipo de cambio 6 h, lugares y geocodificación 24 h). Ningún fallo es fatal.

### Datos en vivo (fase 1)

| Hecho | Fuente (gratis, sin key) | Uso |
|---|---|---|
| Coordenadas y país | Open-Meteo Geocoding | Base de todo lo demás |
| Clima de las fechas | Open-Meteo: pronóstico si el viaje empieza dentro de ~16 días; si no, las mismas fechas de un año anterior (archivo histórico), marcadas como **referencia** | Nota de clima escrita por código, clima por día, marca de lluvia para el planificador |
| Barrios y lugares notables | OpenStreetMap vía Overpass: barrios con nombre + lugares enlazados a Wikidata, emparejados por distancia con su barrio | Solo ciudades fuera del catálogo (`source="live"`) |
| USD → moneda local | Frankfurter (BCE); ExchangeRate-API para monedas que el BCE no publica | Total en moneda local, con tasa, fuente y fecha |

- La fecha de inicio entra en el formulario y en la API (opcional, hasta un año adelante).
- Cada cifra en vivo muestra su fuente y hora de consulta; la UI lista las atribuciones (CC BY 4.0, ODbL).
- El modelo nunca reescribe un dato en vivo: temperaturas, lluvia y tasas las escribe el código.
- Los precios de alojamiento, comida y actividades siguen siendo estimaciones (catálogo o modelo).

### Infraestructura y CI

- **Imágenes (públicas en GHCR):**
  - `trip-planner` (app, `:8080`, 277 MB);
  - `trip-planner-ollama` (Ollama 0.12.3 con `qwen2.5:1.5b-instruct` incluido, `:11434`, sin descarga al arrancar).
- **Pod sugerido** (tope 2 vCPU / 4 GiB): Ollama 1.75 vCPU / 3 GiB y app 0.25 vCPU / 1 GiB. `shareable: false`. Datos completos en `docs/HANDOFF.md`.
- **Workflow `image.yml`:**
  - `test`: 91 tests sin red (LLM y datos en vivo simulados; los proveedores se prueban con `httpx.MockTransport`);
  - `e2e`: Qwen real con los límites del pod, APIs de datos en vivo reales y 4 viajes por SSE (fechas dentro y fuera de la ventana de pronóstico);
  - `image`: publica solo en `main` y solo si pasan `test` y `e2e`.
- **Workflow `model-benchmark.yml`** (manual): los mismos viajes con 1.5B y 3B en paralelo.

### Resultados finales (e2e con Qwen real y límites de pod)

| Escenario | Fuente | Tiempo | Tokens | Calidad |
|---|---|---|---|---|
| Kioto, $2500 | catálogo | 26–38 s | ~1150 | 6/6 atracciones en su día, narrativa fiel |
| Lisboa, $50, 2 personas | catálogo | 25–29 s | ~950–990 | 2 rondas de conflicto; paseos gratuitos en su barrio; "excede el presupuesto" correcto |
| Hanói, $400 | catálogo | 28–35 s | ~1020–1060 | 2/3 (métrica); costos creíbles ($45/noche) |
| Valparaíso, $600 | modelo | 32–36 s | ~1165–1180 | el modelo inventa barrios (limitación esperada) |

Con datos en vivo (segundo e2e con APIs reales, `431b239`):

| Escenario | Datos en vivo | Paso en vivo | Investigación | Total | Tokens |
|---|---|---|---|---|---|
| Kioto, en 5 días | pronóstico real 10–24 °C; 1 USD = 157,59 JPY (BCE) | 0,7 s | 0 tokens | 41,7 s | 1024 |
| Lisboa, en 45 días | referencia 2025: 14–20 °C, lluvia 3 de 3 días; 1 USD = 0,877 EUR (BCE) | 4,3 s | 0 tokens | 42,1 s | 966 |
| Hanói, en 10 días | pronóstico 25–30 °C, lluvia 1 de 2 días; 1 USD = 25 952 VND (ExchangeRate-API) | 7,2 s | 0 tokens | 42,3 s | 901 |
| Valparaíso, en 7 días | pronóstico 11–18 °C; 1 USD = 963 CLP; **OSM: 504 en overpass-api.de** → barrios del modelo | 15,4 s | 333 tokens | 55,6 s | 1090 |

Las tres ciudades del catálogo ya no generan nada en la investigación (antes ~90–140 tokens), y el total de tokens bajó en consecuencia.

La latencia varía entre runners de GitHub (se vio de 21 a 63 s con los mismos tokens). La inferencia domina el tiempo; la orquestación no lo afecta de forma medible.

### Limitaciones conocidas

- Fuera del catálogo, los barrios vienen de OpenStreetMap; si OSM tiene pocos datos, el 1.5B inventa barrios y lugares. El emparejamiento lugar↔barrio es geométrico y puede fallar cerca de un límite.
- Más allá de ~16 días no hay pronóstico real: se muestra una referencia histórica.
- Las APIs públicas a veces se atascan en una petición (visto en CI); hay reintento, pero un proveedor puede faltar y la cadena lo reemplaza por catálogo/modelo.
- El filtro de la narrativa es heurístico: un detalle falso junto a un nombre conocido ("estación Kyoto Central") puede pasar.
- Clima y tipo de cambio son en vivo; los precios (alojamiento, comida, actividades) siguen siendo aproximados y no hay vuelos.
- El catálogo es curado a mano: los tests garantizan la estructura, no la exactitud de cada ubicación.
- El motor de decisiones es por reglas: solo entiende las palabras clave de su taxonomía (es/en).

---

## 2. Desafíos superados

### D1. Especificaciones en conflicto y restricciones del contrato

- **Síntoma:** el README inicial describía Phi 3.8B + Ollama + JEV; la propuesta de diseño pedía Claude Haiku vía Anthropic.
- **Qué pasó:** se siguió primero la propuesta (v1 con Anthropic). Después el usuario pidió volver a un LLM local. **Phi se descartó** porque es de Microsoft y el contrato de la infra prohíbe tecnología Microsoft en el stack interno. Se eligió **Qwen 2.5**, ya validado en otro demo de la misma infra.
- **Lección:** leer el contrato de integración (`docs/DEMO_INTEGRATION.md`) antes de elegir componentes; una restricción dura vale más que una preferencia.

### D2. JEV no era accesible

- **Síntoma:** la idea era usar JEV (TypeSafe) para el razonamiento no generativo.
- **Causa:** JEV es una API alojada, con pesos cerrados, en acceso anticipado con lista de espera y sin opción de self-host. Además su documentación estaba bloqueada desde el entorno de desarrollo. Tampoco hace aritmética ni genera texto.
- **Solución:** `app/decisions/` define una **interfaz con la forma de JEV**: estado + preguntas tipadas (`choice` con opciones, o `score` → P(sí)) → respuestas con probabilidades. Cada pregunta lleva un `family` (para despachar reglas) y un `text` en lenguaje natural (lo que evaluaría JEV). Hoy la implementa `RulesDecisionEngine`, con una taxonomía de palabras clave en español e inglés y heurísticas explícitas. Enchufar JEV es implementar un método, `evaluate()`.
- **Corrección de paso:** el README original decía "Budget Agent (JEV + Python)", pero JEV no hace aritmética. El presupuesto quedó en Python puro.

### D3. Inferencia en CPU: latencia y cantidad de generación

- **Síntoma:** con la v1, el itinerario se regeneraba en cada ronda de conflicto, así que un viaje eran varias salidas JSON largas. En CPU eso no cabía en un tiempo razonable.
- **Solución: "generar una vez, decidir muchas".**
  - El itinerario se genera **una sola vez**.
  - El ciclo de conflicto **no genera nada**: elige acciones de un catálogo fijo (`free_alternatives`, `cheaper_lodging`, `street_food`, `transit_pass`), calcula los ahorros en código, puntúa el daño con el motor de decisiones, y `revise()` aplica los cambios en código.
  - Streaming token a token de la narrativa y eventos `progress` durante la generación, para que la espera no parezca colgada.
- **Evidencia:** ~1000–1200 tokens por viaje y 21–43 s con límites de pod, frente al límite de 6000 tokens y 180 s. En los e2e, el ciclo de conflicto consume 0 tokens.

### D4. Un presupuesto que no pueda alucinarse

- **Problema de diseño:** las acciones típicas de recorte ("alojamiento más barato", "comer en mercados") no tenían dónde aplicarse si el presupuesto solo sumaba actividades por día.
- **Solución:**
  - `itinerary_draft.cost_assumptions`: alojamiento por noche, comida por día y transporte por día para todo el grupo. Se derivan en código de los costos de referencia y las revisiones pueden bajarlos.
  - Unidades explícitas en `reference_costs`.
  - Toda la aritmética vive en `budget.py`.
- **Test:** `test_budget_is_deterministic_arithmetic`.

### D5. JSON fiable con un modelo pequeño

- **Solución:**
  - Salida restringida a un JSON schema (Ollama `format`) generado desde Pydantic, con los `$ref` inlineados (`inline_refs`).
  - Validación Pydantic y **1 reintento**.
  - Los números del modelo se acotan a rangos sensatos, y la normalización fuerza exactamente N días.
- **Detalle importante:** los tokens de un intento fallido **también se cuentan**. En la v1 con Anthropic, `messages.parse` lanzaba la excepción antes de devolver el `usage`, así que se cambió a `create` con validación manual. El cliente Ollama mantiene ese principio.
- **Evidencia:** en los e2e con Qwen real, las llamadas dieron JSON válido al primer intento (`attempt=1, done=stop`). La excepción fue un JSON cortado por un límite de tokens demasiado bajo (ver D11).

### D6. Sin red para el modelo en el entorno de desarrollo

- **Síntoma:** el entorno de desarrollo no podía descargar Qwen (`registry.ollama.ai` y `huggingface.co` estaban bloqueados), ni tenía credenciales de Anthropic en la v1.
- **Solución:**
  - **Modo mock** determinista para UI y tests.
  - Cliente Ollama probado contra un servidor NDJSON falso (`httpx.MockTransport`).
  - Integración contra un Ollama real sin modelo, para validar la conexión y el manejo del 404.
  - **Job `e2e` en GitHub Actions**, donde sí hay red: construye la imagen con Qwen real, aplica los límites del pod (`docker-compose.ci.yml`) y ejecuta viajes reales por SSE.
- **Resultado:** desde entonces, cada push se valida contra el modelo real.

### D7. El resumen inventaba cifras

- **Síntoma (primer e2e real):** *"You can expect to spend $366, which is within your budget of $50"*. Contradecía el resultado honesto.
- **Causa:** a un modelo de 1.5B se le pedía razonar sobre totales y presupuestos.
- **Solución:** la síntesis se partió en dos.
  - La **narrativa** la escribe Qwen **sin ver ningún número** y con prohibición de hablar de dinero. Un filtro en código elimina cualquier frase sobre montos o presupuesto.
  - El **párrafo de presupuesto** (total, recortes aplicados, si cabe o no) lo escribe el **código**.
- **Guardia permanente:** el e2e **falla** si el resumen contradice el veredicto del presupuesto.
- **Test:** `test_budget_facts_in_the_summary_come_from_code_not_the_model` reproduce la frase mentirosa y comprueba que se filtra.

### D8. Conocimiento geográfico y de precios del modelo pequeño

- **Síntomas:**
  - Shibuya (que está en Tokio) aparecía como barrio de Kioto;
  - barrios inventados en Hanói ("Quốc Hoà");
  - un "Museo del Ámbito" que no existe;
  - Hanói más caro que Lisboa;
  - costos idénticos para destinos distintos.
- **Intento 1, prompts** (rangos de costos, "solo barrios dentro de la ciudad", `clean_area`): mejoró los precios, pero no la geografía.
- **Intento 2, modelo más grande:** el benchmark con 3B fue **2 veces más lento** (56–75 s) sin mejorar la geografía (Kinkaku-ji en Gion, "Ribeira" de Oporto en Lisboa, "Paseo Alcorta" de Buenos Aires en Valparaíso).
- **Solución: hechos como datos.** Un catálogo curado (`cities.json`, 45 ciudades) aporta barrios reales, atracciones y nivel de costos. Para esas ciudades Qwen solo redacta. Fuera del catálogo se mantiene el camino del modelo, y el trace indica la fuente.
- **Decisión:** quedarse con 1.5B más catálogo.

### D9. Atracciones en el barrio equivocado

- **Síntoma:** con una lista suelta de atracciones, el modelo las repartía mal entre los días (Kinkaku-ji en Gion).
- **Solución:**
  - Cada barrio del catálogo lista **sus propias** atracciones. Donde una atracción famosa no caía dentro del barrio listado, se cambió el barrio (Kioto: Higashiyama, Gion, Arashiyama; Lisboa: Bairro Alto pasó a Belém).
  - `day_plan()` decide **en código** el barrio de cada día y pasa al modelo solo las atracciones de ese barrio.
  - La normalización **impone el barrio del plan** aunque el modelo devuelva otro.
- **Tests:** ninguna atracción aparece en dos barrios; un modelo que manda todos los días a "Shibuya" queda corregido.
- **Evidencia:** Kioto pasó a 6/6 atracciones en su día.

### D10. El recorte a gratuitos rompía el anclaje

- **Síntoma:** en Lisboa con $50, el recorte `free_alternatives` puso "Explore Alfama's streets" en los tres días, incluidos Baixa y Belém. La alternativa gratuita la escribía el modelo sin saber el barrio.
- **Solución:** en ciudades del catálogo, la alternativa gratuita la escribe el código y **nombra atracciones gratuitas de ese mismo barrio**. Por ejemplo: "Free walk around Alfama: Miradouro de Santa Luzia". Para eso el catálogo marca 152 atracciones como gratuitas, con criterio conservador.
- **Evidencia:** cada día quedó anclado a su barrio en todos los e2e posteriores.

### D11. Un error propio, encontrado por el benchmark

- **Síntoma:** en el benchmark, el viaje a Kioto con 1.5B falló con `chain_failed`. El e2e del CI del **mismo commit** había pasado, porque el modelo no es determinista.
- **Causa:** la llamada que solo pide la nota de temporada tenía `max_tokens=90`. El modelo se extendió, el JSON salió cortado dos veces y un paso obligatorio tumbó la cadena.
- **Solución:** 200 tokens, "máximo 25 palabras" en el prompt, y **si la nota de temporada falla se usa un texto genérico**: los hechos del catálogo no dependen del modelo.
- **Test de regresión:** `test_catalog_city_survives_a_failed_season_note`.
- **Lección:** un solo run verde con un modelo no determinista no prueba nada; conviene tener más de un escenario y más de una ejecución.

### D12. Narrativa con invenciones, markdown y frases cortadas

- **Síntomas:**
  - "Parque Nacional de Higashiyama", "estación Kyoto Central", "Parque Lagoa";
  - negritas y títulos pese a la prohibición en el prompt;
  - una frase final cortada (`done=length`).
- **Solución (`clean_narrative`):**
  - quita el markdown y los títulos (líneas sin puntuación final);
  - descarta la última frase si quedó cortada;
  - descarta frases sobre dinero;
  - descarta frases que nombran un lugar desconocido: un sustantivo de lugar ("Parque", "Museo", "Estación"…) seguido de un nombre propio cuya raíz no aparece en el destino, los barrios, las atracciones ni las actividades.
- **Límite conocido:** "estación Kyoto Central" pasa, porque "Kyoto" es un nombre conocido.
- **Evidencia:** en el e2e siguiente, la narrativa de Kioto salió limpia y fiel al plan.

### D13. Métricas de calidad que mentían

- **Síntomas:**
  - Hanói marcaba 1/4 atracciones cuando en realidad estaba bien, porque los nombres venían traducidos o eran muy cortos ("Ngọc");
  - Kioto daba falsos "mal ubicado" porque la métrica contaba la alternativa gratuita;
  - "Elevador de Santa Justa" se marcaba mal ubicado solo por compartir "Santa" con "Santa Luzia".
- **Solución (`scripts/smoke_e2e.py`):**
  - solo cuenta las actividades;
  - compara raíces de 5 letras (reconoce "Vietnamienses");
  - ignora palabras de la ciudad, del barrio y de las atracciones del propio día;
  - excluye del denominador las atracciones sin palabras distintivas;
  - no avisa de "pocas atracciones" cuando se aplicó el recorte a gratuitos.
- **Lección:** una métrica automática sobre texto libre es una heurística; hay que validarla contra casos reales antes de fiarse de ella.

### D14. SSE detrás de un gateway y con `EventSource`

- **Problemas:**
  - `EventSource` no expone el cuerpo de las respuestas no-200;
  - la reconexión automática lanzaría otra ejecución;
  - los proxies pueden acumular la respuesta en buffer;
  - si el usuario se va, se sigue gastando CPU.
- **Solución:**
  - todo resultado se envía como evento, incluido `error` con un `code` estable;
  - el cliente cierra la conexión tras `done` o `error`;
  - cabeceras `Cache-Control: no-cache` y `X-Accel-Buffering: no`;
  - keepalive cada 10 s;
  - la tarea de la cadena se cancela cuando el cliente se desconecta.

### D15. Migrar a LangGraph sin romper nada

- **Objetivo:** usar un framework reconocible para el post, con el diagrama generado automáticamente.
- **Riesgo:** reescribir el orquestador podía cambiar el comportamiento visible (eventos, orden, respaldos).
- **Solución:**
  - el estado es el mismo `SharedContext`, con reducers para mantener la memoria append-only;
  - los nodos no mutan el estado, devuelven actualizaciones;
  - `ConflictResolution.last_action_ids` pasa los recortes al nodo de revisión;
  - las dependencias van en el runtime context;
  - los eventos salen por el stream `custom`;
  - `Orchestrator.run(ctx, emit)` **conserva su interfaz**.
- **Evidencia:**
  - los 56 tests previos pasaron **sin cambios**, más 4 nuevos (topología, ruta de catálogo agotado, trace final igual al transmitido, endpoint del grafo);
  - comprobado que los eventos llegan en vivo durante cada nodo;
  - el e2e con Qwen real dio los mismos resultados;
  - la imagen pasa de 203 a 277 MB.

### D16. Fricciones del entorno de desarrollo

- **Proxy TLS en `docker build`:** `pip` fallaba al verificar certificados. Se construyó con un Dockerfile temporal que confía en la CA del proxy, sin tocar el `Dockerfile` del repo.
- **Latencia variable entre runners:** los mismos tokens tardaron entre 21 y 63 s según la máquina. Una subida de latencia se interpreta mirando también los tokens antes de atribuirla a un cambio.

### D17. Datos en vivo sin romper "generar una vez" ni la honestidad

- **Síntoma:** el clima era una frase genérica del modelo ("clima templado"), fuera del catálogo el modelo inventaba barrios (Valparaíso: "Casa Blanca", "Playa de Coquimbo"), y el total solo existía en USD.
- **Opciones evaluadas:** Amadeus Self-Service (precios de hoteles/vuelos) cierra en julio de 2026; las alternativas de precios piden key y acuerdo comercial. Se dejó para una fase 2 y se eligieron fuentes gratuitas y sin key para los hechos que cambian: clima, lugares y tipo de cambio.
- **Solución:**
  - un nodo `live_data` en LangGraph, sin LLM, entre `intake` y la investigación; consulta en paralelo y cada proveedor está detrás de una interfaz (igual que el motor de decisiones);
  - la nota de clima la escribe el código a partir de los números: en ciudades del catálogo con clima real, la investigación ya **no genera nada** (0 tokens);
  - fuera de la ventana de pronóstico se usa el mismo rango de fechas de un año anterior, rotulado como referencia para no presentarlo como pronóstico;
  - fuera del catálogo, OSM aporta barrios y lugares notables (filtro: enlazados a Wikidata) y el código empareja cada lugar con su barrio más cercano, reproduciendo el emparejamiento barrio↔atracción del catálogo (D9); el modelo solo estima costos;
  - los días con lluvia probable llegan marcados al planificador (`"rain": true`), que prefiere actividades bajo techo;
  - el total en moneda local lo calcula y escribe el código, con tasa, fuente y fecha.
- **Desafío del entorno:** el sandbox de desarrollo no tiene salida a esas APIs (igual que con Qwen, D6). Los proveedores se probaron con respuestas HTTP simuladas y la validación real quedó en el e2e de CI.
- **Hallazgo del primer e2e real:** Kioto salió completo (pronóstico real 10–24 °C, 1 USD = 157,59 JPY del BCE, investigación con 0 tokens), pero tres peticiones sueltas a Open-Meteo se atascaron hasta el timeout (geocodificación de Lisboa y Valparaíso, pronóstico de Hanói) mientras las siguientes respondían al instante. La cadena cayó correctamente a catálogo/modelo y lo dijo en el trace. Se añadió un reintento (solo para timeouts, errores de conexión y 5xx), timeout de conexión de 3 s y registro de cada fallo con su duración.
- **Segundo e2e real:** los logs mostraron un patrón claro: la *primera* petición a Open-Meteo de cada viaje se atasca 3 s (timeout de conexión) y el reintento responde al instante. Con el reintento, las cuatro ciudades obtuvieron clima y tipo de cambio reales. Lo único que faltó fue OSM: la instancia pública de Overpass respondió `504 Gateway Timeout` (sobrecarga habitual). Se añadió una segunda instancia pública (`overpass.private.coffee`) como respaldo; `OVERPASS_URL` acepta una lista.
- **Dónde:** `app/live/`, `app/agents/live_data.py`, `app/agents/destination_research.py`, `app/agents/itinerary_planning.py` (`forecast_days`, marca de lluvia), `app/agents/synthesis.py` (`local_fx`), `static/app.js` (panel de fuentes).
- **Evidencia:** 28 tests nuevos (26 en `tests/test_live.py`, 2 en `tests/test_api.py`); e2e con APIs reales en CI (ver la tabla de resultados).

---

## 3. Cronología

| Fecha | Sesión | Qué se hizo | Commit / PR |
|---|---|---|---|
| 2026-09-26 | 1 | v1 de punta a punta con Claude Haiku: orquestador en código, SSE, guardrails, mock, UI, Docker | `7e6cd55` |
| 2026-09-26 | 2 | Pivote a Qwen 2.5 local, motor de decisiones tipo JEV con reglas, "generar una vez, decidir muchas", dos imágenes | `cfef9f8` |
| 2026-09-27 | 3 | E2E en CI con Qwen real y límites de pod (D6) | `fd0fb78` |
| 2026-09-27 | 3 | Síntesis honesta, costos de actividades, prompts de investigación (D7, D8) | `b3690cf` |
| 2026-09-27 | 3b | Catálogo curado y benchmark 1.5B vs 3B (D8) | `5978969` |
| 2026-09-27 | 3b | Nota de temporada que tumbaba el viaje (D11) | `e623c89` |
| 2026-09-27 | 3c | Atracciones asociadas a su barrio, `day_plan` por código (D9) | `66e65e0` |
| 2026-09-27 | 3d | Alternativa gratuita por código, idioma, métrica (D10, D13) | `75d2fc6` |
| 2026-09-27 | 3e | Atracciones gratuitas, filtro de narrativa (D10, D12) | `4321762`, `9bbd063` |
| 2026-09-27 | — | **MVP fusionado a `main`** e imágenes publicadas en GHCR | PR #1 → `4c6f232` |
| 2026-09-27 | 4 | Orquestación con LangGraph (D15) | PR #2 → `223bd7e` |
| 2026-09-27 | 5 | Datos en vivo, fase 1: clima, OpenStreetMap, tipo de cambio, fecha de viaje (D17) | `213f143`, `431b239`, instancia Overpass de respaldo |

---

## 4. Pendientes

- [ ] Registrar el demo en la infra de `personal-website` (`infra.bicep` con sidecar Ollama, alta del `projectId`, `shareable: false`) con los datos de `docs/HANDOFF.md`.
- [ ] Revisar a mano las atracciones y barrios de `cities.json`, sobre todo en ciudades conocidas.
- [ ] Ampliar el catálogo de ciudades, y el de acciones de recorte (p. ej. quitar solo un día de actividades de pago).
- [ ] Cuando haya acceso a JEV: `JevDecisionEngine.evaluate()` + `DECISION_ENGINE=jev` (necesita `TYPESAFE_API_KEY` y salida a la red).
- [ ] Mediciones de latencia con varias ejecuciones por escenario (mediana) para el post.
- [ ] Capturas del panel de trace y del grafo (`/api/graph`) para el post.
- [ ] Datos en vivo, fase 2: precios reales de alojamiento/vuelos detrás de un `PriceProvider` (requiere key y acuerdo comercial; Amadeus Self-Service cierra en julio de 2026).
- [ ] Revisar la calidad de OSM en más ciudades fuera del catálogo (nombres de barrios, lugares cerca de los límites).
- [ ] Uso comercial: Open-Meteo gratis es no comercial; si el demo cambia de naturaleza, plan comercial u otro proveedor detrás de la misma interfaz, y Overpass propio (`OVERPASS_URL`).
