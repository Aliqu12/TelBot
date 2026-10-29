"""Anbieterunabhängige Bausteine der LLM-Schicht: Schnittstelle, Fehlerklassen und Antwortbereinigung.

Alle SDK-spezifischen Fehler werden in den Adaptern in diese schmalen Fehlerklassen
übersetzt. Die übrigen Schichten kennen dadurch weder einen Anbieter noch httpx.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..messages import ChatMessage, ToolCall

# Denkphase mancher Modelle (z. B. Qwen), falls sie im Antworttext mitgeliefert wird.
_REASONING_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)


class LLMError(Exception):
    """Basisklasse für Fehler bei der Anfrage an das Sprachmodell."""


class LLMRateLimitError(LLMError):
    """Das Anfragelimit (HTTP 429) ist erreicht."""


class LLMRequestTooLargeError(LLMError):
    """Die Anfrage ist zu groß (HTTP 413), z. B. wegen Kontextlänge oder Token-Limit."""


class LLMInsufficientCreditsError(LLMError):
    """Das Guthaben beim Anbieter ist aufgebraucht (HTTP 402)."""


class LLMTimeoutError(LLMError):
    """Das Modell hat nicht rechtzeitig geantwortet."""


class LLMUnavailableError(LLMError):
    """Netzwerkproblem, Serverfehler oder Fehler des dahinterliegenden Modellanbieters."""


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


def answer_text(content: str | None) -> str:
    """Antworttext ohne eingebettete Denkphase und ohne umgebende Leerzeichen."""
    return _REASONING_BLOCK.sub("", content or "").strip()
