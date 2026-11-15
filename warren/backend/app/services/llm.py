"""LLM access through OmniRoute (OpenAI-compatible gateway) using LangChain's ChatOpenAI.

Example of the raw gateway call this wraps:
    curl http://localhost:20128/v1/models -H "Authorization: Bearer $OMNIROUTE_API_KEY"
"""
import json
import logging
from typing import Any, TypeVar

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from ..config import settings

log = logging.getLogger("warren.llm")
T = TypeVar("T", bound=BaseModel)


def chat_model(temperature: float = 0.1, timeout: int = 180) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "sk_omniroute",
        model=settings.llm_model,
        temperature=temperature,
        timeout=timeout,
        max_retries=2,
    )


def health() -> dict:
    """Ping OmniRoute's /models endpoint (shown in the dashboard status strip)."""
    try:
        r = requests.get(f"{settings.llm_base_url}/models", timeout=3,
                         headers={"Authorization": f"Bearer {settings.llm_api_key}"})
        return {"ok": r.ok, "model": settings.llm_model, "status": r.status_code}
    except requests.RequestException as exc:
        return {"ok": False, "model": settings.llm_model, "error": str(exc)[:120]}


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of an LLM reply (tolerates fences and chatter)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return decoder.raw_decode(text[i:])[0]
            except ValueError:
                continue
    raise ValueError("no JSON found in model output")


def ask_json(system: str, user: str, schema: type[T], temperature: float = 0.1) -> T:
    """One LLM call that must return JSON matching `schema` (one automatic repair retry)."""
    llm = chat_model(temperature)
    messages = [SystemMessage(content=system + "\n\nReply with ONLY valid JSON. No markdown, no commentary."),
                HumanMessage(content=user)]
    last_error = None
    for _ in range(2):
        reply = llm.invoke(messages)
        try:
            return schema.model_validate(extract_json(reply.content if isinstance(reply.content, str) else str(reply.content)))
        except (ValueError, ValidationError) as exc:
            last_error = exc
            messages += [reply, HumanMessage(content=f"That was not valid for the schema ({str(exc)[:300]}). Return ONLY the corrected JSON.")]
    raise ValueError(f"model did not return valid JSON: {last_error}")
