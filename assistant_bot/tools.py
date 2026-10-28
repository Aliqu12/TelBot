"""Minimale Werkzeug-Schicht für Tool Calling.

Das Sprachmodell wählt nur Werkzeug und Argumente aus. Ausgeführt wird
ausschließlich, was hier registriert ist – mit Validierung durch die
Anwendung. Es gibt keine generische Ausführung von Befehlen.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .messages import ToolCall

logger = logging.getLogger(__name__)

ToolHandler = Callable[[int, dict[str, Any]], Awaitable[str]]


class ToolError(Exception):
    """Erwarteter Fehler eines Werkzeugs; die Meldung wird dem Modell zurückgegeben."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema der Argumente
    handler: ToolHandler  # (chat_id, Argumente) -> Ergebnistext für das Modell

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def __bool__(self) -> bool:
        return bool(self._tools)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    async def execute(self, chat_id: int, call: ToolCall) -> str:
        """Führt einen Werkzeugaufruf aus und liefert immer einen Text für das Modell."""
        tool = self._tools.get(call.name)
        if tool is None:
            logger.warning("Unbekanntes Werkzeug angefordert: %r", call.name[:50])
            return f"Fehler: Das Werkzeug {call.name!r} gibt es nicht."
        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return "Fehler: Die Argumente sind kein gültiges JSON."
        if not isinstance(arguments, dict):
            return "Fehler: Die Argumente müssen ein JSON-Objekt sein."

        # Argumente nicht loggen: Sie können personenbezogene Daten enthalten.
        logger.info("Werkzeug ausgeführt: %s (chat_id=%s)", tool.name, chat_id)
        try:
            return await tool.handler(chat_id, arguments)
        except ToolError as exc:
            return f"Fehler: {exc}"
        except Exception:
            logger.exception("Unerwarteter Fehler im Werkzeug %s.", tool.name)
            return "Fehler: Das Werkzeug ist unerwartet fehlgeschlagen."
