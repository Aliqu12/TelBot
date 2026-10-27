"""LLM-Service: Kommunikation mit der Groq API.

Alle SDK-spezifischen Fehler werden hier in eigene, schmale Fehlerklassen
übersetzt. Die übrigen Schichten kennen dadurch weder Groq noch httpx.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import groq
from groq import AsyncGroq

from .messages import ChatMessage

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Basisklasse für Fehler bei der Anfrage an das Sprachmodell."""


class LLMRateLimitError(LLMError):
    """Das Anfragelimit (HTTP 429) ist erreicht."""


class LLMTimeoutError(LLMError):
    """Das Modell hat nicht rechtzeitig geantwortet."""


class LLMUnavailableError(LLMError):
    """Netzwerkproblem oder Serverfehler beim Anbieter."""


class LLMConfigurationError(LLMError):
    """API-Key, Berechtigungen oder Modellname sind ungültig."""


class LLMEmptyResponseError(LLMError):
    """Das Modell hat keinen verwertbaren Text geliefert."""


class ChatModel(Protocol):
    """Schnittstelle, die der Assistent von einem Sprachmodell erwartet."""

    async def complete(self, messages: Sequence[ChatMessage]) -> str: ...

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
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_completion_tokens = max_completion_tokens

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[message.to_api_dict() for message in messages],  # type: ignore[misc]
                temperature=self._temperature,
                max_completion_tokens=self._max_completion_tokens,
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
            logger.error("Groq-Modell %r nicht gefunden (HTTP 404). GROQ_MODEL prüfen.", self._model)
            raise LLMConfigurationError() from exc
        except groq.APIStatusError as exc:
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

        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            logger.warning("Leere Antwort vom Modell (finish_reason=%s).", choice.finish_reason)
            raise LLMEmptyResponseError()
        if choice.finish_reason == "length":
            logger.info("Antwort wurde durch GROQ_MAX_COMPLETION_TOKENS gekürzt.")
        return content

    async def aclose(self) -> None:
        await self._client.close()


def create_groq_chat_model(settings: Settings) -> GroqChatModel:
    """Erzeugt das Groq-Modell inklusive Timeout und automatischer Wiederholungen.

    Das SDK wiederholt Verbindungsfehler, 408/409/429 und 5xx-Antworten selbst
    mit exponentiellem Backoff (``max_retries``).
    """
    client = AsyncGroq(
        api_key=settings.groq_api_key,
        timeout=settings.groq_timeout_seconds,
        max_retries=settings.groq_max_retries,
    )
    return GroqChatModel(
        client,
        model=settings.groq_model,
        temperature=settings.groq_temperature,
        max_completion_tokens=settings.groq_max_completion_tokens,
    )
