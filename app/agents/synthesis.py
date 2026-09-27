"""5. Synthesis Agent — the final summary, in two parts with different owners.

* Narrative (generative): Qwen writes a short, warm description of the trip, streamed token
  by token. It never sees any number and is told not to talk about money: a 1.5B model
  asked to reason about totals and budgets invents figures (measured in the first real run).
* Budget paragraph (deterministic): written by code from `budget_analysis` and
  `conflict_resolution` — so the honest part of the result can't be hallucinated.

If the model can't run (token budget exhausted, model error) the narrative is replaced by
a one-line template; the budget paragraph is always there."""

from __future__ import annotations

import re

from .. import catalog
from ..decisions.rules import normalize
from ..decisions.taxonomy import ACTIONS_BY_ID
from ..guardrails import TokenBudget
from ..llm_client import LLMClient, OnToken
from ..models import FinalItinerary, FxQuote, SharedContext, TokenUsage
from . import compact, language_rule

SYSTEM = """You write a short, warm description of a trip itinerary for the traveller.
Plain text, one paragraph, at most 70 words, no lists, no markdown.
Describe the experience day by day using the given highlights and interests.
Never mention prices, costs, money, numbers or the budget."""

# Safety net: drop any sentence of the narrative that still talks about money.
MONEY = re.compile(
    r"[$€£¥]|\d+\s*(usd|d[oó]lar|dollar|euro)"
    r"|\b(usd|budget|presupuesto|cost[eo]?s?|costar|cuestan?|price[sd]?|precios?|dinero|money|gast[oa]s?|spend|cheap(er)?|barat[oa]s?|expensive)\b", re.I)


async def run(ctx: SharedContext, llm: LLMClient, budget: TokenBudget,
              on_token: OnToken | None = None) -> tuple[FinalItinerary, TokenUsage]:
    req = ctx.user_request
    draft = ctx.itinerary_draft
    assert draft is not None and ctx.budget_analysis is not None
    user = compact({
        "destination": req.destination,
        "interests": req.interests,
        "highlights": [f"Day {d.day} in {d.area}: {', '.join(d.activities[:2])}" for d in draft.days],
    }) + "\n" + language_rule(req)

    text, usage = await llm.complete_text(
        agent="synthesis", system=SYSTEM, user=user, max_tokens=240, budget=budget,
        mock=lambda: _mock_narrative(ctx), on_token=on_token,
    )
    paragraph = budget_paragraph(ctx)
    if on_token:
        await on_token("\n\n" + paragraph)
    narrative = clean_narrative(text, known_place_stems(ctx)) or fallback_summary(ctx).split("\n\n")[0]
    return build_final(ctx, narrative + "\n\n" + paragraph), usage


def clean_narrative(text: str, known_places: set[str] | None = None) -> str:
    """Make the model's prose safe to show:
    * strip markdown the prompt forbids (bold, headings, bullets, code),
    * drop a trailing sentence cut off by the token limit,
    * drop sentences about money (the budget paragraph is written by code),
    * drop sentences naming a place that isn't in the plan ("Parque Nacional de Higashiyama",
      "Parque Lagoa…"), when the set of known place stems is given."""
    text = re.sub(r"(\*\*|__|`+)", "", text)
    text = re.sub(r"(?m)^\s*(#{1,6}\s+|[-*•]\s+)", "", text)
    # Per line: a trailing piece without final punctuation is a heading ("Día 1 en Gion") or,
    # on the last line, a sentence cut off by the token limit — drop it either way.
    sentences = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        parts = [s for s in re.split(r"(?<=[.!?…])\s+", " ".join(line.split())) if s]
        if parts and not re.search(r"[.!?…][\"')»]*$", parts[-1]) and (len(parts) > 1 or len(lines) > 1):
            parts = parts[:-1]
        sentences += parts
    kept = [s for s in sentences if not MONEY.search(s)]
    if known_places is not None:
        kept = [s for s in kept if not unknown_places(s, known_places)]
    return " ".join(kept).strip()


# A place noun followed by a proper name: "Parque Nacional", "estación Kyoto", "Museo del Ámbito".
PLACE = re.compile(
    r"\b(?i:parque|park|estaci[oó]n|station|museo|museum|templo|temple|santuario|shrine|catedral|"
    r"cathedral|iglesia|church|plaza|square|mercado|market|palacio|palace|laguna|lagoa|lago|lake|"
    r"puerto|port|calle|street|barrio|district|jard[ií]n|garden|torre|tower|puente|bridge|"
    r"castillo|castle|playa|beach|monte|mount|mirador|viewpoint)\s+"
    r"(?:(?i:de|del|of|the|la|el|los|las)\s+){0,2}([A-ZÁÉÍÓÚÑ][\w'’-]+(?:\s+[A-ZÁÉÍÓÚÑ][\w'’-]+){0,2})")


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", normalize(text)) if len(w) >= 4}


def unknown_places(sentence: str, known_places: set[str]) -> list[str]:
    """Proper names after a place noun whose stem appears nowhere in the plan."""
    return [name for name in PLACE.findall(sentence)
            if _stems(name) and not (_stems(name) & known_places)]


def known_place_stems(ctx: SharedContext) -> set[str]:
    req, research, draft = ctx.user_request, ctx.destination_research, ctx.itinerary_draft
    texts = [req.destination]
    city = catalog.lookup(req.destination)
    if city is not None:
        texts.append(city.name)
    if research is not None:
        texts += research.recommended_areas + research.highlights
    if draft is not None:
        texts += [a for d in draft.days for a in d.activities]
    return set().union(*(_stems(t) for t in texts))


def build_final(ctx: SharedContext, summary: str) -> FinalItinerary:
    draft, analysis = ctx.itinerary_draft, ctx.budget_analysis
    assert draft is not None and analysis is not None
    fx = local_fx(ctx)
    return FinalItinerary(
        summary=summary.strip(),
        days=draft.days,
        total_cost_usd=analysis.estimated_total_usd,
        budget_usd=ctx.user_request.budget_usd,
        within_budget=analysis.within_budget,
        breakdown=analysis.breakdown,
        fx=fx,
        total_local=round(analysis.estimated_total_usd * fx.rate, 2) if fx else None,
    )


def local_fx(ctx: SharedContext) -> FxQuote | None:
    """The live USD → local rate, when the destination doesn't already use USD."""
    fx = ctx.live_data.fx if ctx.live_data else None
    return fx if fx is not None and fx.currency != "USD" else None


def budget_paragraph(ctx: SharedContext) -> str:
    """The factual part of the summary, always written by code."""
    req, analysis, conflict = ctx.user_request, ctx.budget_analysis, ctx.conflict_resolution
    assert analysis is not None
    es = req.lang == "es"
    total, cap = f"${analysis.estimated_total_usd:,.0f}", f"${req.budget_usd:,.0f}"
    actions = [ACTIONS_BY_ID[a] for a in conflict.applied_action_ids if a in ACTIONS_BY_ID]
    names = "; ".join((a.description_es if es else a.description_en).lower() for a in actions)
    fx = local_fx(ctx)
    local = f" (≈ {analysis.estimated_total_usd * fx.rate:,.0f} {fx.currency})" if fx else ""
    if es:
        text = f"Costo estimado: {total} USD{local} para un presupuesto de {cap}."
        if actions:
            text += f" Para recortar se aplicó: {names}."
        if analysis.within_budget:
            text += f" Queda dentro del presupuesto (margen de ${req.budget_usd - analysis.estimated_total_usd:,.0f})."
        else:
            text += (f" Aun así excede el presupuesto por ${analysis.over_budget_by_usd:,.0f}: "
                     "considera ampliar el presupuesto o acortar el viaje.")
        if fx:
            text += f" Cambio: 1 USD = {fx.rate:,.4g} {fx.currency} ({fx.source.name}, {fx.as_of})."
        return text + " Costos estimados (catálogo o IA), no tarifas en tiempo real."
    text = f"Estimated cost: {total} USD{local} against a {cap} budget."
    if actions:
        text += f" To cut costs we applied: {names}."
    if analysis.within_budget:
        text += f" It fits the budget (${req.budget_usd - analysis.estimated_total_usd:,.0f} to spare)."
    else:
        text += (f" It is still ${analysis.over_budget_by_usd:,.0f} over budget: consider raising the "
                 "budget or shortening the trip.")
    if fx:
        text += f" Rate: 1 USD = {fx.rate:,.4g} {fx.currency} ({fx.source.name}, {fx.as_of})."
    return text + " Costs are estimates (catalog or AI), not live prices."


def fallback_summary(ctx: SharedContext) -> str:
    req = ctx.user_request
    intro = (f"Tu plan de {req.days} día(s) en {req.destination} está listo."
             if req.lang == "es" else f"Your {req.days}-day plan for {req.destination} is ready.")
    return intro + "\n\n" + budget_paragraph(ctx)


def _mock_narrative(ctx: SharedContext) -> str:
    req, draft = ctx.user_request, ctx.itinerary_draft
    areas = ", ".join(dict.fromkeys(d.area for d in draft.days))  # type: ignore[union-attr]
    if req.lang == "es":
        return f"Recorrerás {req.destination} a tu ritmo, pasando por {areas} con tiempo para lo que más te gusta."
    return f"You'll explore {req.destination} at your own pace, through {areas}, with time for what you love."
