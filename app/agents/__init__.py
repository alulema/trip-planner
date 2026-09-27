"""Domain agents of the chain. Each one reads the full `SharedContext` and returns only
its own section; merging is the orchestrator's job."""

import json

from ..models import UserRequest

LANG_NAMES = {"es": "Spanish", "en": "English"}


def language_rule(req: UserRequest) -> str:
    return f"Write every human-readable string in {LANG_NAMES[req.lang]}."


def compact(data) -> str:
    """Compact JSON for prompts — every token counts against the session budget."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
