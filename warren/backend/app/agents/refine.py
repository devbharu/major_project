"""Turn whatever a user typed (review instructions, steering notes) into clean, professional guidance before it is stored or used."""
import logging

from ..services import llm
from ..services.safety import clean_untrusted, wrap_untrusted
from . import prompts
from .schemas import Refined

log = logging.getLogger("warren.refine")
KINDS = {"instructions": "repository review instructions for an AI code reviewer",
         "guidance": "guidance for an autonomous CI repair agent",
         "learning": "a team coding convention"}


def refine(text: str | None, kind: str = "instructions") -> str:
    """Never raises: if the model is unavailable the cleaned original is used."""
    original = clean_untrusted(text, 4000).strip()
    if len(original) < 3:
        return original
    try:
        out = llm.ask_json(prompts.REFINE_SYSTEM.replace("{kind}", KINDS[kind]), wrap_untrusted("user_text", original, 4000), Refined, temperature=0.1)
        return out.text.strip()[:2000] or original
    except Exception:
        log.info("refiner unavailable, keeping the original wording")
        return original[:2000]
