"""Fixed interest taxonomy and the preset catalog of budget-cutting actions.

Preset, reviewable data instead of generated text: the conflict loop picks from this
catalog (via the decision engine) and code applies the chosen actions.
"""

from __future__ import annotations

from dataclasses import dataclass

# Category → keywords (es/en, lowercase, accent-free). Matched as substrings of the
# normalized interest/activity text.
INTEREST_CATEGORIES: dict[str, tuple[str, ...]] = {
    "food": ("food", "comida", "gastronom", "cocina", "culinar", "restaur", "ramen", "sushi", "tapas",
             "street food", "mercado", "market", "vino", "wine", "cafe", "coffee", "cerveza", "beer", "comer"),
    "religion_heritage": ("templo", "temple", "iglesia", "church", "catedral", "cathedral", "santuario",
                          "shrine", "mezquita", "mosque", "monaster", "pagoda", "sagrad", "religi"),
    "history_museums": ("museo", "museum", "histori", "history", "castillo", "castle", "ruina", "ruin",
                        "arqueolog", "archaeolog", "palacio", "palace", "monument"),
    "nature_outdoors": ("naturaleza", "nature", "parque", "park", "jardin", "garden", "montan", "mountain",
                        "senderismo", "hiking", "hike", "trek", "bosque", "forest", "lago", "lake", "volcan"),
    "beach": ("playa", "beach", "mar", "sea", "surf", "snorkel", "buceo", "diving", "isla", "island"),
    "art_culture": ("arte", "art", "galeria", "gallery", "arquitect", "architect", "teatro", "theater",
                    "theatre", "musica", "music", "cultura", "culture", "festival", "danza", "dance"),
    "nightlife": ("noche", "night", "bar", "club", "fiesta", "party", "cocktail", "coctel", "vida nocturna"),
    "shopping": ("compras", "shopping", "tienda", "shop", "moda", "fashion", "artesan", "craft", "souvenir"),
    "adventure": ("aventura", "adventure", "kayak", "rafting", "escalada", "climbing", "ski", "esqui",
                  "parapente", "paraglid", "bici", "bike", "cycling", "tour en"),
    "relaxation": ("relax", "descanso", "rest", "spa", "onsen", "termal", "hot spring", "bienestar",
                   "wellness", "yoga", "tranquil"),
}
OTHER = "other"
CATEGORY_OPTIONS: tuple[str, ...] = (*INTEREST_CATEGORIES, OTHER)


@dataclass(frozen=True)
class CatalogAction:
    id: str
    description_es: str
    description_en: str
    # Categories this action may hurt if they are among the traveller's interests.
    harms: tuple[str, ...]
    # Question asked to the decision engine to judge the harm (Jev-style phrasing).
    harm_question: str


ACTION_CATALOG: tuple[CatalogAction, ...] = (
    CatalogAction(
        id="free_alternatives",
        description_es="Cambiar actividades de pago por la alternativa gratuita prevista para cada día",
        description_en="Swap paid activities for each day's planned free alternative",
        harms=("history_museums", "adventure", "art_culture"),
        harm_question="Would replacing paid activities (entrance fees, tours) with free alternatives "
                      "noticeably hurt this traveller's main interests?",
    ),
    CatalogAction(
        id="cheaper_lodging",
        description_es="Bajar a un alojamiento más económico (hostal o guesthouse, ~30% menos por noche)",
        description_en="Move to cheaper lodging (hostel or guesthouse, ~30% less per night)",
        harms=("relaxation",),
        harm_question="Would staying in cheaper, more basic lodging noticeably hurt this traveller's "
                      "main interests?",
    ),
    CatalogAction(
        id="street_food",
        description_es="Comer más en mercados y puestos callejeros (~35% menos en comida)",
        description_en="Eat more at markets and street stalls (~35% less on food)",
        harms=(),  # for food lovers this is arguably a feature, not a loss
        harm_question="Would eating mostly at markets and street stalls instead of restaurants "
                      "noticeably hurt this traveller's main interests?",
    ),
    CatalogAction(
        id="transit_pass",
        description_es="Usar pases de transporte y caminar más (~30% menos en transporte)",
        description_en="Use transit day passes and walk more (~30% less on transport)",
        harms=(),
        harm_question="Would relying on transit passes and walking noticeably hurt this traveller's "
                      "main interests?",
    ),
)
ACTIONS_BY_ID = {a.id: a for a in ACTION_CATALOG}

# Deterministic savings factors applied by code (arithmetic never goes to a model).
LODGING_CUT = 0.30
FOOD_CUT = 0.35
TRANSPORT_CUT = 0.30
FREE_ALTERNATIVE_COST_USD = 0.0
