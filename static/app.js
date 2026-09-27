// Trip Planner UI — consumes the agent chain over SSE (/api/plan-trip/stream).
// No framework, no build step. All server text is rendered with textContent (never HTML).
(() => {
  "use strict";

  const I18N = {
    es: {
      subtitle: "Una cadena de agentes planifica tu viaje en vivo",
      about: "Acerca de", destination: "Destino", days: "Días", budget: "Presupuesto (USD)",
      travelers: "Viajeros", interests: "Intereses (separados por coma)", interestsPh: "comida, templos",
      plan: "Planificar viaje", planning: "Planificando…", lowball: "Probar presupuesto irreal ($50)",
      disclaimer: "Estimaciones generadas por IA, no tarifas en tiempo real.",
      trace: "Agent trace", itinerary: "Itinerario",
      traceEmpty: "Envía un viaje para ver a los agentes trabajar.",
      resultEmpty: "El itinerario aparecerá aquí día por día.",
      a_orchestrator: "Orquestador", a_destination_research: "Investigación", a_itinerary_planning: "Itinerario",
      a_budget: "Presupuesto", a_conflict_resolution: "Conflictos", a_synthesis: "Síntesis",
      ev_plan_created: "plan creado", ev_started: "trabajando…", ev_completed: "listo",
      ev_conflict_detected: "conflicto detectado", ev_skipped: "omitido", ev_failed: "falló", ev_finished: "fin de la cadena",
      day: "Día", activities: "actividades", lodging: "Alojamiento", food: "Comida", acts: "Actividades",
      transport: "Transporte", total: "Total estimado", of: "de", within: "Dentro del presupuesto",
      over: "Excede presupuesto", working: "En curso", season: "Temporada", areas: "Zonas",
      conflictTitle: "Resolución de conflictos", iteration: "iteración", draft: "borrador", revision: "revisión",
      tokens: "tokens", mock: "modo offline (simulado)", disconnected: "Conexión interrumpida.",
      ev_decision: "decide", generating: "generando…", warming: "cargando modelo…", rules: "reglas",
      q_category: "categoría de", q_harm: "¿afecta los intereses?", q_plan: "¿el plan conserva los intereses?",
      decisionsBy: "decisiones", profile: "Intereses", adjusted: "ajustado",
      srcCatalog: "datos: catálogo", srcModel: "datos: modelo", highlights: "Imperdibles",
    },
    en: {
      subtitle: "A chain of agents plans your trip, live",
      about: "About", destination: "Destination", days: "Days", budget: "Budget (USD)",
      travelers: "Travelers", interests: "Interests (comma separated)", interestsPh: "food, temples",
      plan: "Plan trip", planning: "Planning…", lowball: "Try an unrealistic budget ($50)",
      disclaimer: "AI-generated estimates, not live prices.",
      trace: "Agent trace", itinerary: "Itinerary",
      traceEmpty: "Submit a trip to watch the agents work.",
      resultEmpty: "The itinerary will appear here day by day.",
      a_orchestrator: "Orchestrator", a_destination_research: "Research", a_itinerary_planning: "Itinerary",
      a_budget: "Budget", a_conflict_resolution: "Conflicts", a_synthesis: "Synthesis",
      ev_plan_created: "plan created", ev_started: "working…", ev_completed: "done",
      ev_conflict_detected: "conflict detected", ev_skipped: "skipped", ev_failed: "failed", ev_finished: "chain finished",
      day: "Day", activities: "activities", lodging: "Lodging", food: "Food", acts: "Activities",
      transport: "Transport", total: "Estimated total", of: "of", within: "Within budget",
      over: "Over budget", working: "Working", season: "Season", areas: "Areas",
      conflictTitle: "Conflict resolution", iteration: "iteration", draft: "draft", revision: "revision",
      tokens: "tokens", mock: "offline mode (simulated)", disconnected: "Connection lost.",
      ev_decision: "decides", generating: "generating…", warming: "loading model…", rules: "rules",
      q_category: "category of", q_harm: "hurts interests?", q_plan: "plan still matches interests?",
      decisionsBy: "decisions", profile: "Interests", adjusted: "adjusted",
      srcCatalog: "data: catalog", srcModel: "data: model", highlights: "Highlights",
    },
  };
  const ICON = { decision: "🎯", started: "⏳", completed: "✅", conflict_detected: "⚠️", failed: "❌", skipped: "⏭️", plan_created: "🧭", finished: "🏁" };

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const usd = (v) => "$" + Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });

  // Resolved early by demo-info.js (so the About panel uses the same language).
  let lang = document.documentElement.lang === "en" ? "en" : "es";
  const t = (k) => (I18N[lang] && I18N[lang][k]) || k;

  let source = null;
  let startedAt = 0;
  let tokenLimit = 0;
  let running = false;

  function applyI18n() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach((n) => { n.textContent = t(n.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((n) => { n.placeholder = t(n.dataset.i18nPlaceholder); });
    $("lang-toggle").textContent = lang === "es" ? "EN" : "ES";
    if (running) $("submit-btn").textContent = t("planning");
  }

  async function loadConfig() {
    try {
      const cfg = await (await fetch("api/config")).json();
      tokenLimit = cfg.max_tokens_per_session;
      const badge = $("mode-badge");
      badge.hidden = false;
      const llm = cfg.llm_mode === "mock" ? t("mock") : cfg.model + (cfg.llm_ready ? "" : ` (${t("warming")})`);
      badge.textContent = `${llm} · ${t("decisionsBy")}: ${t(cfg.decision_engine)}`;
      badge.dataset.mode = cfg.llm_mode;
    } catch (_) { /* optional */ }
  }

  // ------------------------------------------------------------------ rendering

  function resetUI() {
    $("trace").replaceChildren();
    $("days").replaceChildren();
    ["profile", "research", "budget-box", "conflict-box", "summary", "status-pill"].forEach((id) => { $(id).hidden = true; });
    $("result-empty").hidden = false;
    $("token-meter").textContent = "";
    document.querySelectorAll("#chain li").forEach((li) => { li.className = ""; });
  }

  function setChain(agent, state) {
    const li = document.querySelector(`#chain li[data-agent="${agent}"]`);
    if (li) li.className = state;
  }

  function addTrace(entry) {
    const li = el("li", entry.event === "conflict_detected" ? "conflict" : entry.event);
    // Relative to the first server timestamp, so client/server clock skew doesn't matter.
    const ts = new Date(entry.timestamp).getTime();
    if (!startedAt) startedAt = ts;
    const secs = (Math.max(0, ts - startedAt) / 1000).toFixed(1);
    li.append(el("span", "icon", ICON[entry.event] || "•"), el("span", "t", `+${secs}s`));
    const body = el("span");
    body.append(el("span", "agent", t("a_" + entry.agent)), document.createTextNode(" " + t("ev_" + entry.event)));
    if (entry.decision) body.append(el("span", "msg", " · " + describeDecision(entry.decision)));
    const extra = [];
    if (entry.message) extra.push(entry.message);
    if (entry.duration_ms != null) extra.push((entry.duration_ms / 1000).toFixed(1) + "s");
    if (entry.tokens) extra.push(`${entry.tokens} ${t("tokens")}`);
    if (extra.length) body.append(el("span", "msg", " · " + extra.join(" · ")));
    if (entry.event === "started") {
      const prog = el("span", "msg progress");
      body.append(prog);
      liveLines[entry.agent] = prog;
    }
    li.append(body);
    $("trace").append(li);
    $("trace").scrollTop = $("trace").scrollHeight;

    // Chain strip state.
    if (entry.event === "completed" || entry.event === "failed") {
      if (liveLines[entry.agent]) liveLines[entry.agent].textContent = "";
      delete liveLines[entry.agent];
    }
    if (entry.event === "started") setChain(entry.agent, "working");
    else if (entry.event === "completed") setChain(entry.agent, "done");
    else if (entry.event === "failed") setChain(entry.agent, "failed");
    else if (entry.event === "conflict_detected") { setChain("budget", "conflict"); setChain("conflict_resolution", "conflict"); }
    else if (entry.event === "plan_created") setChain("orchestrator", "working");
    else if (entry.event === "finished") setChain("orchestrator", "done");

    if (entry.agent === "orchestrator" && entry.tokens) updateMeter(entry.tokens);
  }

  // Typed decisions (rules today, Jev-ready): question → answer (probability · source).
  function describeDecision(d) {
    const [kind, subject] = d.question.includes(":") ? d.question.split(/:(.*)/s) : [d.question, ""];
    const src = t(d.source);
    if (kind === "category") {
      const p = d.probabilities[d.answer] ?? 0;
      return `${t("q_category")} «${subject}» → ${d.answer} (p=${p.toFixed(2)} · ${src})`;
    }
    if (kind === "harm") return `${subject}: ${t("q_harm")} p=${Number(d.answer).toFixed(2)} (${src})`;
    if (kind === "plan_matches_interests") return `${t("q_plan")} p=${Number(d.answer).toFixed(2)} (${src})`;
    return `${d.question} → ${d.answer} (${src})`;
  }

  const liveLines = {};
  function onProgress(p) {
    const line = liveLines[p.agent];
    if (line) line.textContent = ` · ${t("generating")} ${p.tokens} ${t("tokens")}`;
  }

  function onToken(tok) {
    const s = $("summary");
    if (s.hidden) { s.textContent = ""; s.hidden = false; s.classList.add("streaming"); }
    s.textContent += tok.text;
  }

  function renderProfile(p) {
    if (!p || !p.matches.length) return;
    const box = $("profile");
    box.replaceChildren(el("span", null, `${t("profile")}: `));
    p.matches.forEach((m) => box.append(el("span", "chip", `${m.interest} → ${m.category}`)));
    box.hidden = false;
  }

  let tokensSoFar = 0;
  function updateMeter(total) {
    tokensSoFar = total;
    $("token-meter").textContent = tokenLimit ? `${tokensSoFar} / ${tokenLimit} ${t("tokens")}` : `${tokensSoFar} ${t("tokens")}`;
  }

  function renderResearch(r) {
    const box = $("research");
    box.replaceChildren();
    const season = el("div", null, `${t("season")}: ${r.season_notes} `);
    season.append(el("span", "tag", r.source === "catalog" ? t("srcCatalog") : t("srcModel")));
    box.append(season);
    box.append(el("div", "areas", `${t("areas")}: ${r.recommended_areas.join(" · ")}`));
    if (r.highlights && r.highlights.length) box.append(el("div", "areas", `${t("highlights")}: ${r.highlights.join(" · ")}`));
    box.hidden = false;
  }

  // Days are revealed one by one so the plan "builds" in front of the user.
  let revealToken = 0;
  function renderDays(days, revision) {
    const myToken = ++revealToken;
    const list = $("days");
    list.replaceChildren();
    $("result-empty").hidden = true;
    days.forEach((d, i) => {
      setTimeout(() => {
        if (myToken !== revealToken) return;
        const li = el("li", "day" + (revision ? " revised" : ""));
        const head = el("header");
        const title = el("span", null, `${t("day")} ${d.day} · `);
        title.append(el("span", "area", d.area));
        const cost = el("span", "cost", `${usd(d.estimated_cost_usd)} ${t("activities")}`);
        if (d.adjusted) cost.prepend(el("span", "tag", t("adjusted")));
        head.append(title, cost);
        const ul = el("ul");
        d.activities.forEach((a) => ul.append(el("li", null, a)));
        li.append(head, ul);
        list.append(li);
      }, i * 140);
    });
  }

  function renderDraft(draft) {
    renderDays(draft.days, draft.revision);
    const pill = $("status-pill");
    pill.hidden = false;
    pill.className = "pill working";
    pill.textContent = `${t("working")} · ${draft.revision ? t("revision") + " " + draft.revision : t("draft")}`;
  }

  function renderBudget(b, budgetUsd) {
    const box = $("budget-box");
    box.replaceChildren();
    const total = el("div", "budget-total");
    total.append(el("span", null, t("total")), el("span", null, `${usd(b.estimated_total_usd)} ${t("of")} ${usd(budgetUsd)}`));
    const grid = el("div", "budget-grid");
    [["lodging", b.breakdown.lodging], ["food", b.breakdown.food], ["acts", b.breakdown.activities], ["transport", b.breakdown.transport]]
      .forEach(([k, v]) => { const c = el("div"); c.append(el("small", null, t(k)), document.createTextNode(usd(v))); grid.append(c); });
    box.append(total, grid);
    box.hidden = false;
  }

  function renderConflict(c) {
    if (!c || !c.triggered) return;
    const box = $("conflict-box");
    box.replaceChildren();
    box.append(el("strong", null, `⚠️ ${t("conflictTitle")} · ${c.iterations} ${t("iteration")}(s)`));
    const ul = el("ul");
    c.actions_taken.forEach((a) => ul.append(el("li", null, a)));
    box.append(ul);
    box.hidden = false;
  }

  function renderFinal(f) {
    renderDays(f.days, 0);
    const s = $("summary");
    s.textContent = f.summary;
    s.hidden = false;
    s.classList.remove("streaming");
    const pill = $("status-pill");
    pill.hidden = false;
    pill.className = "pill " + (f.within_budget ? "ok" : "over");
    pill.textContent = f.within_budget ? `✅ ${t("within")}` : `⚠️ ${t("over")} (${usd(f.total_cost_usd - f.budget_usd)})`;
  }

  function showError(message) {
    const li = el("li", "error");
    li.append(el("span", null, "❌"), el("span"), el("span", null, message));
    $("trace").append(li);
    const pill = $("status-pill");
    pill.hidden = true;
  }

  // ------------------------------------------------------------------ streaming

  function finish() {
    running = false;
    if (source) { source.close(); source = null; }
    $("submit-btn").disabled = false;
    $("lowball-btn").disabled = false;
    $("submit-btn").textContent = t("plan");
  }

  function start(overrides) {
    if (running) return;
    const form = new FormData($("trip-form"));
    const params = new URLSearchParams({
      destination: form.get("destination"),
      days: form.get("days"),
      budget_usd: overrides && overrides.budget_usd ? overrides.budget_usd : form.get("budget_usd"),
      travelers: form.get("travelers"),
      interests: form.get("interests"),
      lang,
    });
    resetUI();
    tokensSoFar = 0;
    running = true;
    startedAt = 0;
    $("submit-btn").disabled = true;
    $("lowball-btn").disabled = true;
    $("submit-btn").textContent = t("planning");
    const budgetUsd = Number(params.get("budget_usd"));

    source = new EventSource("api/plan-trip/stream?" + params.toString());
    source.addEventListener("trace", (e) => addTrace(JSON.parse(e.data)));
    source.addEventListener("section", (e) => {
      const { key, value } = JSON.parse(e.data);
      if (!value) return;
      if (key === "interest_profile") renderProfile(value);
      else if (key === "destination_research") renderResearch(value);
      else if (key === "itinerary_draft") renderDraft(value);
      else if (key === "budget_analysis") renderBudget(value, budgetUsd);
      else if (key === "conflict_resolution") renderConflict(value);
      else if (key === "final_itinerary") renderFinal(value);
    });
    source.addEventListener("progress", (e) => onProgress(JSON.parse(e.data)));
    source.addEventListener("token", (e) => onToken(JSON.parse(e.data)));
    source.addEventListener("done", (e) => {
      const ctx = JSON.parse(e.data);
      const total = Object.values(ctx.token_usage || {}).reduce((s, u) => s + u.input_tokens + u.output_tokens, 0);
      updateMeter(total);
      finish();
    });
    source.addEventListener("error", (e) => {
      // Server-sent `error` events carry data; a bare transport error does not.
      if (e.data) {
        showError(JSON.parse(e.data).message);
      } else if (running) {
        showError(t("disconnected"));
      }
      finish(); // never let EventSource auto-reconnect (each reconnect is a new paid run)
    });
  }

  // ------------------------------------------------------------------ wiring

  $("trip-form").addEventListener("submit", (e) => { e.preventDefault(); start(); });
  $("lowball-btn").addEventListener("click", () => start({ budget_usd: 50 }));
  $("lang-toggle").addEventListener("click", () => {
    if (running) return;
    try { localStorage.setItem("tp-lang", lang === "es" ? "en" : "es"); } catch (_) {}
    // Reload so the shared About panel (language fixed at init) switches too.
    location.reload();
  });
  $("about-btn").addEventListener("click", () => {
    if (window.DemoPanel) window.DemoPanel.open();
  });
  // The panel is optional: hide its button if the shared script didn't load.
  window.addEventListener("load", () => { if (!window.DemoPanel) $("about-btn").hidden = true; });

  applyI18n();
  loadConfig();
})();
