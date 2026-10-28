"""Anwendungslogik: verbindet Systemprompt, Gesprächsverlauf und Sprachmodell.

Diese Klasse ist bewusst unabhängig von Telegram. Zukünftige Funktionen
(Tools, Erinnerungen, Homeserver-Integrationen) können hier ansetzen, ohne
die Telegram- oder Groq-Anbindung zu verändern.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, tzinfo

from .conversation import ConversationService
from .llm import ChatModel
from .messages import ChatMessage
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

# Obergrenze für Modell-Runden mit Werkzeugaufrufen pro Nutzernachricht (Kosten-/Schleifenschutz).
MAX_TOOL_ROUNDS = 5
TOOL_LIMIT_MESSAGE = "Ich konnte die Anfrage nicht vollständig bearbeiten. Formuliere sie bitte etwas einfacher."
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


class Assistant:
    def __init__(
        self,
        chat_model: ChatModel,
        conversations: ConversationService,
        system_prompt: str,
        tools: ToolRegistry | None = None,
        timezone: tzinfo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_model = chat_model
        self._conversations = conversations
        self._system_prompt = system_prompt
        self._tools = tools
        self._timezone = timezone
        self._now = now or (lambda: datetime.now(timezone))

    async def reply(self, chat_id: int, user_text: str) -> str:
        """Erzeugt eine Antwort und speichert den Austausch erst nach Erfolg.

        Schlägt die LLM-Anfrage fehl, bleibt der Verlauf unverändert. So entstehen
        keine "verwaisten" Nutzernachrichten ohne Antwort im Kontext.
        """
        history = await self._conversations.get_history(chat_id)
        system_prompt = self._system_prompt
        if self._tools:
            system_prompt += "\n" + self._date_context()
        messages = [
            ChatMessage(role="system", content=system_prompt),
            *history,
            ChatMessage(role="user", content=user_text),
        ]
        if self._tools:
            answer = await self._reply_with_tools(chat_id, messages)
        else:
            answer = await self._chat_model.complete(messages)
        # Gespeichert werden nur Frage und Antwort – keine Werkzeugergebnisse (z. B. E-Mail-Inhalte).
        await self._conversations.add_exchange(chat_id, user_text, answer)
        logger.info(
            "Antwort erzeugt (chat_id=%s, Kontext=%d Nachrichten, Eingabe=%d Zeichen, Antwort=%d Zeichen).",
            chat_id,
            len(history),
            len(user_text),
            len(answer),
        )
        return answer

    async def _reply_with_tools(self, chat_id: int, messages: list[ChatMessage]) -> str:
        assert self._tools is not None
        specs = self._tools.specs()
        for _ in range(MAX_TOOL_ROUNDS):
            turn = await self._chat_model.complete_with_tools(messages, specs)
            if not turn.tool_calls:
                return turn.content
            messages.append(ChatMessage(role="assistant", content=turn.content, tool_calls=turn.tool_calls))
            for call in turn.tool_calls:
                result = await self._tools.execute(chat_id, call)
                messages.append(ChatMessage(role="tool", content=result, tool_call_id=call.call_id))
        logger.warning("Maximale Anzahl an Werkzeugrunden erreicht (chat_id=%s).", chat_id)
        return TOOL_LIMIT_MESSAGE

    def _date_context(self) -> str:
        """Datum, Wochentag und Uhrzeit, damit das Modell "morgen" oder "Freitag" korrekt berechnet."""
        now = self._now()
        text = (
            f"Heutiges Datum: {_WEEKDAYS[now.weekday()]}, {now.strftime('%d.%m.%Y')} ({now.date().isoformat()}), "
            f"aktuelle Uhrzeit: {now.strftime('%H:%M')}"
        )
        if self._timezone is not None:
            text += f" (Zeitzone {self._timezone})"
        return text

    async def reset(self, chat_id: int) -> None:
        deleted = await self._conversations.reset(chat_id)
        logger.info("Gesprächsverlauf zurückgesetzt (chat_id=%s, %d Nachrichten gelöscht).", chat_id, deleted)

    async def aclose(self) -> None:
        await self._chat_model.aclose()
