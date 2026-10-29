"""LLM-Service: Kommunikation mit der Groq API.

Alle SDK-spezifischen Fehler werden hier in eigene, schmale Fehlerklassen
übersetzt. Die übrigen Schichten kennen dadurch weder Groq noch httpx.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import groq
from groq import AsyncGroq

from .messages import ChatMessage, ToolCall

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)

# Denkphase mancher Modelle (z. B. Qwen), falls sie im Antworttext mitgeliefert wird.
_REASONING_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)


class LLMError(Exception):
    """Basisklasse für Fehler bei der Anfrage an das Sprachmodell."""


class LLMRateLimitError(LLMError):
    """Das Anfragelimit (HTTP 429) ist erreicht."""


class LLMRequestTooLargeError(LLMError):
    """Die Anfrage ist zu groß (HTTP 413), z. B. wegen des Token-Limits pro Minute."""


class LLMTimeoutError(LLMError):
    """Das Modell hat nicht rechtzeitig geantwortet."""


class LLMUnavailableError(LLMError):
    """Netzwerkproblem oder Serverfehler beim Anbieter."""


class LLMConfigurationError(LLMError):
    """API-Key, Berechtigungen oder Modellname sind ungültig."""


class LLMEmptyResponseError(LLMError):
    """Das Modell hat keinen verwertbaren Text geliefert."""


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """Antwort des Modells: entweder Text oder Werkzeugaufrufe (oder beides)."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class ChatModel(Protocol):
    """Schnittstelle, die der Assistent von einem Sprachmodell erwartet."""

    async def complete(self, messages: Sequence[ChatMessage]) -> str: ...

    async def complete_with_tools(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> ModelTurn: ...

    async def aclose(self) -> None: ...


class GroqChatModel:
    """Chat-Completion über das offizielle Groq SDK (AsyncGroq)."""

    def __init__(
        self,
        client: AsyncGroq,
        *,
        model: str,
        temperature: float,
        max_completion_tokens: int,
        extra_params: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_completion_tokens = max_completion_tokens
        self._extra_params = dict(extra_params or {})

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        choice = await self._request(messages)
        content = _answer_text(choice.message.content)
        if not content:
            logger.warning("Leere Antwort vom Modell (finish_reason=%s).", choice.finish_reason)
            raise LLMEmptyResponseError()
        if choice.finish_reason == "length":
            logger.info("Antwort wurde durch GROQ_MAX_COMPLETION_TOKENS gekürzt.")
        return content

    async def complete_with_tools(
        self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]
    ) -> ModelTurn:
        """Wie ``complete``, das Modell darf aber Werkzeugaufrufe anfordern.

        Das Modell führt nichts selbst aus – es liefert nur Name und Argumente.
        """
        choice = await self._request(messages, tools=list(tools), tool_choice="auto")
        tool_calls = tuple(
            ToolCall(call_id=call.id, name=call.function.name, arguments=call.function.arguments or "{}")
            for call in (choice.message.tool_calls or [])
        )
        content = _answer_text(choice.message.content)
        if not content and not tool_calls:
            logger.warning("Leere Antwort vom Modell (finish_reason=%s).", choice.finish_reason)
            raise LLMEmptyResponseError()
        return ModelTurn(content=content, tool_calls=tool_calls)

    async def _request(self, messages: Sequence[ChatMessage], **extra: Any) -> Any:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[message.to_api_dict() for message in messages],  # type: ignore[misc]
                temperature=self._temperature,
                max_completion_tokens=self._max_completion_tokens,
                **self._extra_params,
                **extra,
            )
        # Reihenfolge beachten: speziellere Klassen vor ihren Basisklassen.
        except groq.RateLimitError as exc:
            logger.warning("Groq Rate Limit erreicht (HTTP %s).", exc.status_code)
            raise LLMRateLimitError() from exc
        except groq.APITimeoutError as exc:
            logger.warning("Zeitüberschreitung bei der Groq-Anfrage.")
            raise LLMTimeoutError() from exc
        except groq.APIConnectionError as exc:
            logger.warning("Groq ist nicht erreichbar: %s", type(exc).__name__)
            raise LLMUnavailableError() from exc
        except (groq.AuthenticationError, groq.PermissionDeniedError) as exc:
            logger.error("Groq hat die Anfrage abgelehnt (HTTP %s). GROQ_API_KEY prüfen.", exc.status_code)
            raise LLMConfigurationError() from exc
        except groq.NotFoundError as exc:
            logger.error("Groq-Modell %r nicht gefunden (HTTP 404). Modellnamen in .env prüfen.", self._model)
            raise LLMConfigurationError() from exc
        except groq.APIStatusError as exc:
            if exc.status_code == 413:
                # Die Groq-Meldung nennt Limit und angefragte Tokens, enthält aber keine Nutzerdaten.
                logger.warning(
                    "Groq-Anfrage zu groß (HTTP 413, Modell %s): %s", self._model, _error_detail(exc)
                )
                raise LLMRequestTooLargeError() from exc
            if exc.status_code >= 500:
                logger.warning("Groq-Serverfehler (HTTP %s).", exc.status_code)
                raise LLMUnavailableError() from exc
            logger.error("Groq-Anfrage fehlgeschlagen (HTTP %s).", exc.status_code)
            raise LLMError() from exc
        except groq.APIError as exc:
            logger.error("Unerwartete Antwort der Groq API: %s", type(exc).__name__)
            raise LLMError() from exc

        if not response.choices:
            logger.warning("Groq-Antwort enthält keine Auswahl (choices).")
            raise LLMEmptyResponseError()
        return response.choices[0]

    async def aclose(self) -> None:
        await self._client.close()


def _error_detail(exc: groq.APIStatusError) -> str:
    """Kurze Fehlerbeschreibung der API für das Log (Länge begrenzt)."""
    return " ".join(str(exc.message).split())[:300]


def _answer_text(content: str | None) -> str:
    return _REASONING_BLOCK.sub("", content or "").strip()


def create_groq_chat_model(settings: Settings) -> GroqChatModel:
    """Erzeugt das Groq-Modell inklusive Timeout und automatischer Wiederholungen.

    Das SDK wiederholt Verbindungsfehler, 408/409/429 und 5xx-Antworten selbst
    mit exponentiellem Backoff (``max_retries``).
    """
    return _create_model(settings, settings.groq_model)


def create_groq_image_model(settings: Settings) -> GroqChatModel | None:
    """Erzeugt das Vision-Modell für Bildnachrichten (``None``, wenn GROQ_IMAGE_MODEL leer ist)."""
    if settings.groq_image_model is None:
        return None
    extra: dict[str, Any] = {}
    if settings.groq_image_reasoning_effort is not None:
        extra["reasoning_effort"] = settings.groq_image_reasoning_effort
    return _create_model(settings, settings.groq_image_model, extra)


def _create_model(settings: Settings, model: str, extra_params: Mapping[str, Any] | None = None) -> GroqChatModel:
    client = AsyncGroq(
        api_key=settings.groq_api_key,
        timeout=settings.groq_timeout_seconds,
        max_retries=settings.groq_max_retries,
    )
    return GroqChatModel(
        client,
        model=model,
        temperature=settings.groq_temperature,
        max_completion_tokens=settings.groq_max_completion_tokens,
        extra_params=extra_params,
    )
