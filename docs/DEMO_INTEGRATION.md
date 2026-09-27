# Integrar un demo a la infraestructura efímera (alexisalulema.com)

> **Copia este archivo en tu repo de demo.** Es el **contrato** autocontenido que tu proyecto
> debe cumplir para enchufarse al servicio de demos efímeros de `alexisalulema.com`. NO asume
> que tienes acceso al repo de la infra: todo lo que necesitas está aquí.

## Cómo funciona (en una frase)

Empaquetas tu demo como **imagen pública en GHCR**. La infra la provisiona on-demand en un
Resource Group efímero de Azure Container Apps, la expone en `https://demoNN.alexisalulema.com/`
detrás de un **gateway** que hace la autenticación y el ruteo por ti, y la destruye sola
(inactividad / tope de tiempo / kill-switch). Tú solo construyes **un contenedor que sirve
HTTP/WS por ingress interno**.

---

## El contrato (lo que tu demo DEBE cumplir)

1. **Contenedor con ingress interno, puerto fijo.** Sirve en `0.0.0.0:<TU_PUERTO>` (elige uno y
   documéntalo, p.ej. `8080`). El ingress es **interno** → solo el gateway te alcanza. **No
   pongas TLS en tu app**: Azure Container Apps + el gateway terminan TLS.
2. **Cero auth en tu app.** El gateway valida el JWT de sesión **antes** de enrutarte. Confía en
   todo request que te llega (ya pasó el filtro). No implementes login ni tokens propios.
3. **Sírvete desde la raíz `/`.** Te exponen en `https://demoNN.alexisalulema.com/`. Nada de
   rutas con prefijo fijo (`/miapp/...`).
4. **Streaming soportado.** El gateway proxea **WebSocket y SSE** en streaming, y **corta el
   stream cuando la sesión expira** (WS → close code `4001`). Un chat puede usar SSE o WS sin
   lógica extra; solo maneja el cierre abrupto con elegancia.
5. **Stateless / estado solo efímero.** Tu entorno se destruye **en cualquier momento**. No
   dependas de persistencia. Si necesitas estado (índice vectorial, DB), que sea **co-localizado,
   efímero y reconstruible al arranque** (p.ej. un Postgres+pgvector en el mismo contenedor/red,
   sembrado al iniciar desde un dump horneado en la imagen).
6. **Sin tecnología Microsoft en el stack interno** (restricción dura): OSS / terceros. Ej.:
   Claude (no Azure OpenAI), pgvector (no Azure AI Search), `sentence-transformers`, imagen en
   **GHCR** (no ACR). Azure/Cloudflare como *infra* sí están bien (no son "el demo").
7. **Imagen pública en GHCR.** Publica `ghcr.io/<owner>/<nombre>:latest` y marca el package como
   **Public** (la infra la jala sin credenciales). **Solo datos públicos en la imagen**; los
   secretos llegan en runtime (ver punto 8).
8. **Variables de entorno que recibes en provisión:**
   - `PROJECT_ID` — el slug de tu demo (ej. `rag-demo`).
   - `DEMO_SLOT` — el slot asignado (ej. `demo03`).
   - Los **secretos** que declares (ej. `ANTHROPIC_API_KEY`) — inyectados como secret del
     Container App. **Léelos de env; NUNCA los hornees en la imagen** (es pública).

---

## Referencia mínima (inline)

El demo válido más simple es un estático:

```dockerfile
# Dockerfile — sirve / en el puerto 80 (interno); el gateway hace el resto.
FROM nginx:alpine
COPY index.html /usr/share/nginx/html/index.html
```

Una app dinámica con streaming, igual de autocontenida:

```python
# main.py — FastAPI: sirve / + un stream SSE. Sin auth (la hace el gateway).
import os
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
PROJECT_ID = os.environ.get("PROJECT_ID", "")
DEMO_SLOT  = os.environ.get("DEMO_SLOT", "")
# SECRET = os.environ["ANTHROPIC_API_KEY"]   # inyectado en provisión, no en la imagen

@app.get("/")
def root():
    return {"project": PROJECT_ID, "slot": DEMO_SLOT}

@app.get("/stream")
def stream():
    def gen():
        for tok in ("hola ", "mundo"):
            yield f"data: {tok}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```

```dockerfile
# Dockerfile para la app FastAPI (escucha en 0.0.0.0:8080, ingress interno)
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## Estilo / branding (adopta el look de alexisalulema.com)

Para que tu demo se vea como parte del sitio **y adopte sus cambios de estilo
automáticamente**, enlaza **en vivo** la hoja de tema que publica el sitio (mismos design
tokens: colores, tipografías, dark/light) y estila con sus variables CSS:

```html
<link rel="stylesheet" href="https://alexisalulema.com/demo-theme.css">
```
```css
/* Tu UI usa los tokens del tema (no hardcodees colores): */
.panel  { background: var(--color-bg-card); border: 1px solid var(--color-border); border-radius: var(--radius); }
.title  { color: var(--color-text); font-family: var(--font-sans); }
.accent { color: var(--color-accent); }
```

- **Auto-adopción:** enlazas el archivo vivo → el día que el sitio cambie su branding, tu demo
  lo toma solo (no es una copia). No hardcodees la paleta; usa `var(--color-*)`, `--font-*`,
  `--radius`.
- **Dark/light:** el tema es **dark por defecto** y pasa a **light** si el SO lo prefiere
  (`prefers-color-scheme`). Los demos están en otro subdominio, así que **no comparten** el
  toggle del sitio; si quieres un toggle propio, pon `data-theme="light"|"dark"` en `<html>`.
- **Fuentes:** el tema ya importa Inter + JetBrains Mono; con enlazarlo las tienes.
- **Fallback:** ten valores propios mínimos por si el tema no carga (degradación elegante).

---

## Panel "Acerca de este demo" (opcional, recomendado)

Un panel de detalles **consistente para todos los demos, sin código propio** — mismo modelo de
auto-adopción que `demo-theme.css`. Es un botón flotante **"Acerca de"** que abre un drawer lateral
con pestañas (Resumen · Arquitectura · Infraestructura · Diseño y límites) y un **diagrama de
arquitectura** en Mermaid. Sirve de carta de presentación: quien prueba tu demo ve sobre qué corre,
cómo está diseñado y sus limitantes, sin salir del demo.

> **Cada demo es dueño de su propio contenido.** El sitio mantiene el *motor* del panel (estilos,
> widget y este esquema); **la descripción, la arquitectura, el diagrama, la infra y las limitaciones
> las escribe el equipo del demo** —que conoce su sistema—, no el mantenedor del sitio. Rellena tu
> `DEMO_INFO` cuando desarrolles el demo, leyendo este contrato. El bloque de más abajo es un
> **ejemplo ilustrativo**, no contenido que debas copiar tal cual.

**Actívalo en 3 pasos:**

1. Enlaza el tema (ya deberías tenerlo):
   ```html
   <link rel="stylesheet" href="https://alexisalulema.com/demo-theme.css">
   ```
2. Define tu contenido en `window.DEMO_INFO` (todo opcional; solo se renderiza lo que exista):
   ```html
   <script>
     window.DEMO_INFO = { /* ver esquema + ejemplo abajo */ };
   </script>
   ```
3. Carga el widget (una línea, **después** de definir `DEMO_INFO`):
   ```html
   <script src="https://alexisalulema.com/demo-panel.js" defer></script>
   ```

Es **puramente aditivo**: si `DEMO_INFO` no existe o el script no carga, tu demo no se afecta. Todo
el contenido lo escribes tú y se renderiza como **texto** (no se inyecta HTML); solo el string
`diagram` se entrega a Mermaid.

### Esquema de `DEMO_INFO`

| Campo | Tipo | Qué es |
|---|---|---|
| `title` | string | Título del panel (fallback: `document.title`). |
| `overview` | string | Resumen del demo (pestaña Resumen). |
| `architecture.description` | string | Prosa de la arquitectura. |
| `architecture.diagram` | string (Mermaid) | Diagrama; se renderiza a SVG al abrir la pestaña. |
| `infra` | `{ name, role }[]` | Componentes sobre los que corre (pestaña Infraestructura). |
| `sizing` | string | Recursos / tuning (2–4 vCPU, modelo, etc.). |
| `design` | string[] | Decisiones de diseño. |
| `limitations` | string[] | Limitaciones honestas. |
| `links` | `{ repo, blog? }` | Enlaces del pie. **`repo` = URL pública del repo GitHub del demo — recomendado**: es el chip "Código/Source" del panel y parte de la carta de presentación (el lector va al fuente). `blog` opcional (write-up). |
| `lang` | `"en" \| "es" \| "auto"` | Idioma (por defecto `auto`: del `<html lang>` / navegador). |
| `trigger` | `"custom"` | Omite el botón flotante; llámalo tú con `window.DemoPanel.open()`. |

**Bilingüe:** cualquier campo acepta un hermano `…Es` (`overviewEs`, `sizingEs`, `roleEs`,
`designEs`, `limitationsEs`, `descriptionEs`, `titleEs`). Si falta el localizado, usa el base.

**API JS:** `window.DemoPanel.open() / .close() / .toggle()` — para cablear un botón propio en tu
topbar (con `trigger: "custom"`).

### Ejemplo ilustrativo (cómo lo rellenaría rag-blogposts) — escribe el de TU demo

> Es una **referencia de forma y tono**, no contenido para copiar: cada demo describe su propia
> arquitectura, infra y limitaciones.

```html
<script>
window.DEMO_INFO = {
  title:   "RAG Chatbot over Blog Posts",
  titleEs: "Chatbot RAG sobre Posts del Blog",
  overview:   "An interactive retrieval-augmented chatbot that answers strictly from Alexis Alulema's blog posts. Fully self-hosted: multilingual semantic search over a pgvector store plus a local Qwen 2.5 LLM served by Ollama — no external LLM APIs.",
  overviewEs: "Un chatbot RAG interactivo que responde exclusivamente desde los posts del blog de Alexis Alulema. Totalmente autohospedado: búsqueda semántica multilingüe sobre un store pgvector y un LLM Qwen 2.5 local servido por Ollama — sin APIs de LLM externas.",
  architecture: {
    description:   "Your browser talks to a short-lived session (JWT) at the edge; a persistent gateway authenticates and routes to this ephemeral pod. The app embeds your question, runs a top-k vector search and streams a grounded answer token by token.",
    descriptionEs: "Tu navegador habla con una sesión efímera (JWT) en el borde; un gateway persistente autentica y enruta a este pod efímero. La app embebe tu pregunta, hace búsqueda vectorial top-k y transmite una respuesta fundamentada token a token.",
    diagram: `flowchart LR
  U["Browser<br/>(session JWT)"] -->|HTTPS| CF["Cloudflare edge"]
  CF --> GW["Session gateway<br/>auth + routing"]
  GW -->|internal ingress| APP["FastAPI app"]
  subgraph POD["Ephemeral pod - 2 vCPU / 4 GiB"]
    APP -->|embed| EMB["sentence-transformers"]
    APP -->|top-k| DB[("pgvector")]
    APP -->|generate| LLM["Ollama - Qwen2.5 0.5B"]
  end
  APP -.->|SSE tokens| U`
  },
  infra: [
    { name: "Cloudflare edge",       role: "TLS + WAF for demoNN.alexisalulema.com at the edge.",                                   roleEs: "TLS + WAF para demoNN.alexisalulema.com en el borde." },
    { name: "Session gateway",       role: "Persistent reverse proxy: validates the session JWT and routes here. The app has no auth.", roleEs: "Reverse proxy persistente: valida el JWT de sesión y enruta aquí. La app no lleva auth." },
    { name: "FastAPI app",           role: "Orchestrates retrieval + generation, streams tokens over SSE.",                          roleEs: "Orquesta recuperación + generación y transmite tokens por SSE." },
    { name: "sentence-transformers", role: "Multilingual embeddings for the query and the corpus.",                                 roleEs: "Embeddings multilingües para la consulta y el corpus." },
    { name: "pgvector (PostgreSQL)", role: "Vector store of the blog corpus; seeded at startup from a dump baked into the image.",   roleEs: "Store vectorial del corpus; sembrado al arranque desde un dump horneado en la imagen." },
    { name: "Ollama - Qwen2.5 0.5B", role: "Local LLM; grounded answers, token by token. No external API.",                         roleEs: "LLM local; respuestas fundamentadas, token a token. Sin API externa." },
    { name: "GHCR",                  role: "Public images (app / ollama / db), pulled fresh on each provision.",                    roleEs: "Imágenes públicas (app / ollama / db), traídas frescas en cada provisión." }
  ],
  sizing:   "Single ephemeral pod capped at 2 vCPU / 4 GiB (Consumption tier). CPU biased to inference — Ollama 1.5 vCPU, app 0.25, db 0.25. Tuned for CPU-only: Qwen 2.5 0.5B, TOP_K=3, CHUNK_TOKENS=400, ~256 output tokens → first token in ~6-7s.",
  sizingEs: "Un solo pod efímero, tope 2 vCPU / 4 GiB (tier Consumption). CPU sesgado a la inferencia — Ollama 1.5 vCPU, app 0.25, db 0.25. Ajustado para CPU: Qwen 2.5 0.5B, TOP_K=3, CHUNK_TOKENS=400, ~256 tokens de salida → primer token en ~6-7s.",
  design: [
    "Fully self-hosted, no external LLM APIs — privacy, cost, and it showcases an OSS stack.",
    "Grounded-only: answers come strictly from the blog corpus; off-topic questions are refused via a similarity threshold.",
    "CPU-only inference: a small quantized model + short output over a bigger/slower one, to stay snappy for a single live user.",
    "Fresh corpus: re-ingested from the public sitemap on each publish (event-driven), so the bot tracks the live blog."
  ],
  designEs: [
    "Totalmente autohospedado, sin APIs de LLM externas — privacidad, costo, y muestra un stack OSS.",
    "Solo fundamentado: responde exclusivamente desde el corpus del blog; lo fuera de tema se rehúsa vía un umbral de similitud.",
    "Inferencia solo-CPU: modelo pequeño cuantizado + salida corta en vez de uno más grande/lento, para mantenerlo ágil con un usuario en vivo.",
    "Corpus fresco: re-ingesta desde el sitemap público en cada publicación (event-driven), así el bot sigue al blog en vivo."
  ],
  limitations: [
    "CPU inference → first token ~6-7s; no GPU in the ephemeral tier.",
    "0.5B model: fast but phrasing is occasionally rough vs a larger model (latency/quality trade-off).",
    "Grounded to ~34 posts — it won't answer outside the blog by design.",
    "Ephemeral: ~20-min sessions, scale-to-zero, no memory across sessions."
  ],
  limitationsEs: [
    "Inferencia en CPU → primer token ~6-7s; sin GPU en el tier efímero.",
    "Modelo 0.5B: rápido, pero la redacción a veces es tosca frente a uno más grande (trade-off latencia/calidad).",
    "Fundamentado a ~34 posts — por diseño no responde fuera del blog.",
    "Efímero: sesiones ~20 min, scale-to-zero, sin memoria entre sesiones."
  ],
  links: { repo: "https://github.com/alulema/rag-blogposts" }
};
</script>
<script src="https://alexisalulema.com/demo-panel.js" defer></script>
```

### CSP (solo si tu demo define `Content-Security-Policy`)

El widget vive en el origen del sitio y Mermaid se importa **lazy** desde jsDelivr. Si pones CSP,
permite:

```
style-src  'self' https://alexisalulema.com 'unsafe-inline';
script-src 'self' https://alexisalulema.com https://cdn.jsdelivr.net;
```

(`'unsafe-inline'` en `style-src` es solo para los estilos que Mermaid inyecta en el SVG.) Si tu CSP
no permite jsDelivr o Mermaid no carga, el panel **degrada** a mostrar el código del diagrama y el
resto sigue funcionando. **La mayoría de demos no ponen CSP → funciona sin tocar nada.**

> **Nota:** los demos de prueba de infra (`hello`, `heartbeat`) **no** usan el panel; es para demos
> "de producto" que quieren contar su historia técnica.

---

## Documentación de tu demo (bitácora + manual de réplica)

Tu repo debe llevar **dos** documentos con audiencias distintas — no los mezcles:

1. **Bitácora / devlog** (`docs/Devlog.md` o similar) — **interno y cronológico**: actividades,
   decisiones, desafíos superados y cómo se resolvieron. Es tu memoria para reconstruir el demo más
   tarde. Aquí SÍ puedes anotar cualquier detalle (incluida la plataforma). Espejo de lo que el sitio
   lleva en su propio Devlog.

2. **Manual de réplica** (`README.md`) — **público y autocontenido**: que un lector clone tu repo y
   **levante el demo por su cuenta** (p.ej. `docker run`), sin depender de ninguna orquestación
   externa. Estructura sugerida: qué es · arquitectura (+ diagrama) · prerrequisitos · build · run ·
   variables de entorno · uso · limitaciones.

**Límite importante (portabilidad + separación de concerns):** el manual de réplica describe **solo tu
demo**. **No** menciones la integración con `alexisalulema.com` ni la plataforma cloud que lo hospeda:
son irrelevantes para replicar tu demo y lo acoplarían a una infra que no es la tuya. Por contrato tu
demo es un contenedor **OSS que corre en cualquier lugar con Docker** — así que "corre en cualquier
parte" es la **verdad literal**, no una omisión. Si necesitas explicar por qué la app no lleva auth ni
TLS, dilo en **abstracto**: *"diseñada para correr detrás de un reverse proxy / gateway que termina TLS
y hace la autenticación"* — sin nombrar la infra concreta. Fuera del manual: hostnames internos,
JWT/gateway, detalles de la nube y cualquier secreto.

> Las **tres capas** de doc no se duplican, se estratifican: **manual de réplica** (`README.md`) =
> narrativa canónica y portable; **`DEMO_INFO`** (panel) = subconjunto destilado in-demo (resumen +
> arquitectura + limitaciones); **devlog** = registro cronológico interno. Escribe una vez la narrativa
> y destila hacia el panel.

---

## Términos técnicos para el post del blog (glosario clic-para-definir)

Los posts técnicos de alexisalulema.com/blog tienen un **glosario interactivo**: un término
marcado se puede hacer clic (o tap) para abrir un diálogo corto con su definición y un link a
Wikipedia, sin que el lector tenga que salir del post a buscarlo. El mecanismo corre sobre el
HTML ya renderizado (`src/lib/rehype-glossary.mjs` + `GlossaryDialog.astro` en el repo del
sitio) — **tu post se escribe en Markdown/LaTeX normal, sin ninguna sintaxis especial**; el
sitio detecta el término automáticamente.

El glosario en sí vive **centralizado en el repo del sitio** (`src/lib/glossary.mjs`), no en el
tuyo — por eso, si el post técnico de tu demo usa vocabulario poco común fuera de su campo
(siglas de un algoritmo, un protocolo, un término de dominio específico), entrega junto con el
hand-off manifest una tabla así:

| Término, tal como aparece en el post | Definición corta (1-2 frases, en el idioma del post) | Wikipedia |
|---|---|---|
| PPO | *(ejemplo)* Algoritmo de aprendizaje por refuerzo que ajusta la política del agente en pasos acotados, para evitar que una sola actualización la empeore demasiado. | https://en.wikipedia.org/wiki/Proximal_policy_optimization |

Notas para armar esa tabla:
- Un término se marca **una sola vez** por post (su primera aparición), no en cada mención —
  no hace falta pedir que se repita el marcado.
- Si el término tiene forma larga y sigla juntas (p.ej. "GAIL (Generative Adversarial Imitation
  Learning)"), incluye ambas variantes en la fila; el sitio prioriza la forma larga cuando las
  dos aparecen pegadas, para no marcar dos veces lo mismo en la misma frase.
- Preferí la Wikipedia en español si existe página equivalente y el post es en español; si no
  existe (común en jerga técnica muy específica), un link a la inglesa está bien.
- Esta lista es acumulativa entre demos: si un término que necesitas ya está en el glosario del
  sitio (porque otro post lo agregó antes), no hace falta repetirlo en el manifest.

---

## Lo que entregas a la infra (hand-off manifest)

Cuando tu imagen esté lista y probada, pásale al mantenedor de la infra estos datos (él hace el
registro y la provisión en el repo de infra privado; **tú no tocas Azure ni OIDC**):

| Campo | Ejemplo |
|---|---|
| `projectId` (slug) | `rag-demo` |
| Nombre legible (ES / EN) | "RAG Chatbot" |
| Repo GitHub (público) | `https://github.com/alulema/rag-demo` (para el chip "Código" del panel y el `githubUrl` de la card en el sitio) |
| Imagen GHCR + puerto | `ghcr.io/alulema/rag-demo:latest`, `8080` |
| `shareable` | `false` (stateful → un entorno por request) / `true` (stateless reusable) |
| Secretos a inyectar | `ANTHROPIC_API_KEY` |
| Recursos extra | sidecar Postgres+pgvector (si aplica) + cómo levantarlo |

---

## Ciclo de vida que tu app DEBE tolerar

- Sesión por usuario: **~20 min** (hard cap).
- Inactividad: **~8 min** sin tráfico de cliente → teardown.
- Vida máxima del entorno: **~60 min**.
- Kill-switch del admin en cualquier momento.

El gateway cierra WS/SSE al expirar la sesión. **Diséñate para teardown abrupto:** arranque
rápido, sin estado que se pierda. No asumas que la siguiente request verá lo de la anterior si
hubo un corte.

---

## Prueba local (sin la infra)

El gateway es **transparente** (proxy + auth). Regla práctica: **si tu contenedor funciona
golpeándolo directo en su puerto, funciona detrás del gateway.** Así que para el 90% del
desarrollo basta:

```bash
docker build -t mi-demo . && docker run -p 8080:8080 \
  -e PROJECT_ID=mi-demo -e DEMO_SLOT=demo01 mi-demo
curl localhost:8080/         # tu app, directo
```

Para validar el ruteo por subdominio + auth por token end-to-end (opcional, antes de publicar),
coordina con el mantenedor de la infra: tiene un `docker-compose` que levanta el gateway real +
storage emulado y prueba el flujo completo.
