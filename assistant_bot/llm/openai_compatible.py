"""Adapter für alle Anbieter mit OpenAI-kompatibler Chat-Completions-Schnittstelle (OpenRouter, Groq, ...).

Nachrichten, Werkzeuge und Bilder sind bei diesen Anbietern identisch aufgebaut; sie
unterscheiden sich nur in Adresse, API-Key und Fehlerdetails. Die SDK-Fehler werden hier
in die Fehlerklassen aus ``base`` übersetzt.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import openai
from openai import AsyncOpenAI

from ..messages import ChatMessage, ToolCall
from .base import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMInsufficientCreditsError,
    LLMRateLimitError,
    LLMRequestTooLargeError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelTurn,
    answer_text,
)

logger = logging.getLogger(__name__)

_MAX_DETAIL_CHARS = 300


class OpenAICompatibleChatModel:
    """Chat-Completion über das OpenAI-SDK (``AsyncOpenAI``) mit frei wählbarer ``base_url``."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        max_tokens_param: str = "max_tokens",
        provider_name: str = "LLM",
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_tokens_param = max_tokens_param
        self._provider = provider_name

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        choice = await self._request(messages)
        content = answer_text(choice.message.content)
        if not content:
            self._log_empty(choice)
            raise LLMEmptyResponseError()
        if choice.finish_reason == "length":
            logger.info("Antwort wurde durch LLM_MAX_TOKENS gekürzt.")
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
        content = answer_text(choice.message.content)
        if not content and not tool_calls:
            self._log_empty(choice)
            raise LLMEmptyResponseError()
        return ModelTurn(content=content, tool_calls=tool_calls)

    async def _request(self, messages: Sequence[ChatMessage], **extra: Any) -> Any:
        params: dict[str, Any] = {self._max_tokens_param: self._max_tokens}
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[message.to_api_dict() for message in messages],  # type: ignore[misc]
                temperature=self._temperature,
                **params,
                **extra,
            )
        except openai.APIStatusError as exc:
            raise self._translate_status_error(exc) from exc
        # Reihenfolge beachten: APITimeoutError ist eine Unterklasse von APIConnectionError.
        except openai.APITimeoutError as exc:
            logger.warning("Zeitüberschreitung bei der %s-Anfrage.", self._provider)
            raise LLMTimeoutError() from exc
        except openai.APIConnectionError as exc:
            logger.warning("%s ist nicht erreichbar: %s", self._provider, type(exc).__name__)
            raise LLMUnavailableError() from exc
        except openai.APIError as exc:
            logger.error("Unerwartete Antwort von %s: %s", self._provider, type(exc).__name__)
            raise LLMError() from exc

        return self._first_choice(response)

    def _translate_status_error(self, exc: openai.APIStatusError) -> LLMError:
        status = exc.status_code
        provider = self._provider
        if status == 429:
            logger.warning("%s Rate Limit erreicht (HTTP 429).", provider)
            return LLMRateLimitError()
        if status == 408:
            logger.warning("Zeitüberschreitung bei der %s-Anfrage (HTTP 408).", provider)
            return LLMTimeoutError()
        if status == 413:
            # Die Meldung nennt Limits bzw. Tokenzahlen, enthält aber keine Nutzerdaten.
            logger.warning(
                "%s-Anfrage zu groß (HTTP 413, Modell %s): %s", provider, self._model, _error_detail(exc)
            )
            return LLMRequestTooLargeError()
        if status == 402:
            logger.error("%s: Guthaben aufgebraucht (HTTP 402). Guthaben aufladen.", provider)
            return LLMInsufficientCreditsError()
        if status in (401, 404):
            hint = "API-Key prüfen" if status == 401 else f"Modell {self._model!r} nicht gefunden, LLM_MODEL prüfen"
            logger.error("%s hat die Anfrage abgelehnt (HTTP %s): %s.", provider, status, hint)
            return LLMConfigurationError()
        if status == 403:
            # Bei OpenRouter z. B. Moderation oder Budgetgrenze; bei Groq fehlende Berechtigung.
            logger.error("%s hat die Anfrage verweigert (HTTP 403): %s", provider, _error_detail(exc))
            return LLMError()
        if status >= 500:
            logger.warning("%s-Serverfehler (HTTP %s): %s", provider, status, _error_detail(exc))
            return LLMUnavailableError()
        logger.error("%s-Anfrage fehlgeschlagen (HTTP %s): %s", provider, status, _error_detail(exc))
        return LLMError()

    def _first_choice(self, response: Any) -> Any:
        """Liefert die erste Auswahl; erkennt Fehler, die der Anbieter in einer 200-Antwort meldet."""
        choices = getattr(response, "choices", None)
        if not choices:
            detail = _embedded_error(response)
            if detail is not None:
                logger.warning("%s meldet einen Fehler in der Antwort: %s", self._provider, detail)
                raise LLMUnavailableError()
            logger.warning("%s-Antwort enthält keine Auswahl (choices).", self._provider)
            raise LLMEmptyResponseError()
        choice = choices[0]
        if getattr(choice, "finish_reason", None) == "error":
            # OpenRouter: Der Modellanbieter ist nach Annahme der Anfrage ausgefallen.
            logger.warning(
                "%s meldet einen Fehler des Modellanbieters: %s",
                self._provider,
                _embedded_error(choice) or "keine Details",
            )
            raise LLMUnavailableError()
        return choice

    def _log_empty(self, choice: Any) -> None:
        if choice.finish_reason == "length":
            logger.warning(
                "Leere Antwort: Das Token-Limit wurde erreicht, bevor Text entstand (Denkphase?). "
                "LLM_MAX_TOKENS erhöhen."
            )
        else:
            logger.warning("Leere Antwort vom Modell (finish_reason=%s).", choice.finish_reason)

    async def aclose(self) -> None:
        await self._client.close()


def _error_detail(exc: openai.APIStatusError) -> str:
    """Kurze Fehlerbeschreibung der API für das Log (Länge begrenzt)."""
    return " ".join(str(exc.message).split())[:_MAX_DETAIL_CHARS]


def _embedded_error(source: Any) -> str | None:
    """Text eines ``error``-Objekts in der Antwort bzw. Auswahl (Objekt oder Dict), sonst ``None``."""
    error = getattr(source, "error", None)
    if not error:
        return None
    message = error.get("message") if isinstance(error, dict) else getattr(error, "message", None)
    return " ".join(str(message or "unbekannter Fehler").split())[:_MAX_DETAIL_CHARS]
