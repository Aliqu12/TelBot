"""Anwendungslogik: verbindet Systemprompt, Gesprächsverlauf und Sprachmodell.

Diese Klasse ist bewusst unabhängig von Telegram. Zukünftige Funktionen
(Tools, Erinnerungen, Homeserver-Integrationen) können hier ansetzen, ohne
die Telegram- oder Groq-Anbindung zu verändern.
"""

from __future__ import annotations

import logging

from .conversation import ConversationService
from .llm import ChatModel
from .messages import ChatMessage

logger = logging.getLogger(__name__)


class Assistant:
    def __init__(self, chat_model: ChatModel, conversations: ConversationService, system_prompt: str) -> None:
        self._chat_model = chat_model
        self._conversations = conversations
        self._system_prompt = system_prompt

    async def reply(self, chat_id: int, user_text: str) -> str:
        """Erzeugt eine Antwort und speichert den Austausch erst nach Erfolg.

        Schlägt die LLM-Anfrage fehl, bleibt der Verlauf unverändert. So entstehen
        keine "verwaisten" Nutzernachrichten ohne Antwort im Kontext.
        """
        history = await self._conversations.get_history(chat_id)
        messages = [
            ChatMessage(role="system", content=self._system_prompt),
            *history,
            ChatMessage(role="user", content=user_text),
        ]
        answer = await self._chat_model.complete(messages)
        await self._conversations.add_exchange(chat_id, user_text, answer)
        logger.info(
            "Antwort erzeugt (chat_id=%s, Kontext=%d Nachrichten, Eingabe=%d Zeichen, Antwort=%d Zeichen).",
            chat_id,
            len(history),
            len(user_text),
            len(answer),
        )
        return answer

    async def reset(self, chat_id: int) -> None:
        deleted = await self._conversations.reset(chat_id)
        logger.info("Gesprächsverlauf zurückgesetzt (chat_id=%s, %d Nachrichten gelöscht).", chat_id, deleted)

    async def aclose(self) -> None:
        await self._chat_model.aclose()
