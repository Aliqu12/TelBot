"""Erzeugt die Sprachmodelle aus der Konfiguration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from .base import ChatModel
from .openai_compatible import OpenAICompatibleChatModel

if TYPE_CHECKING:
    from ..config import Settings


@dataclass(frozen=True)
class ProviderSpec:
    """Anbieterspezifische Werte eines OpenAI-kompatiblen Dienstes."""

    display_name: str
    base_url: str
    # Name des Parameters für die maximale Antwortlänge (die Anbieter akzeptieren unterschiedliche Namen).
    max_tokens_param: str


# Die Schlüssel müssen zu ``config.SUPPORTED_LLM_PROVIDERS`` passen.
PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec("OpenRouter", "https://openrouter.ai/api/v1", "max_tokens"),
    "groq": ProviderSpec("Groq", "https://api.groq.com/openai/v1", "max_completion_tokens"),
}


def create_chat_model(settings: Settings) -> OpenAICompatibleChatModel:
    """Erzeugt das Textmodell inklusive Timeout und automatischer Wiederholungen.

    Das SDK wiederholt Verbindungsfehler, 408/409/429 und 5xx-Antworten selbst
    mit exponentiellem Backoff (``max_retries``) und beachtet dabei ``Retry-After``.
    """
    return _create_model(settings, settings.llm_model)


def create_image_model(settings: Settings, chat_model: ChatModel) -> ChatModel:
    """Liefert das Modell für Bildnachrichten.

    Ohne ``LLM_IMAGE_MODEL`` (oder bei gleichem Namen) wird das Textmodell selbst
    zurückgegeben, es entsteht also kein zweiter Client.
    """
    if settings.llm_image_model is None or settings.llm_image_model == settings.llm_model:
        return chat_model
    return _create_model(settings, settings.llm_image_model)


def _create_model(settings: Settings, model: str) -> OpenAICompatibleChatModel:
    spec = PROVIDERS[settings.llm_provider]
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=spec.base_url,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    return OpenAICompatibleChatModel(
        client,
        model=model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        max_tokens_param=spec.max_tokens_param,
        provider_name=spec.display_name,
    )
