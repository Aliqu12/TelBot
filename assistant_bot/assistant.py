"""Anwendungslogik: verbindet Systemprompt, Gesprächsverlauf und Sprachmodell.

Diese Klasse ist bewusst unabhängig von Telegram. Zukünftige Funktionen
(Tools, Erinnerungen, Homeserver-Integrationen) können hier ansetzen, ohne
die Telegram- oder LLM-Anbindung zu verändern.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, tzinfo

from .config import DEFAULT_IMAGE_HISTORY_MESSAGES
from .conversation import ConversationService
from .llm import ChatModel, LLMError
from .messages import ChatMessage, ImageInput
from .prompts import ERROR_INSTRUCTION
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

# Obergrenze für Modell-Runden mit Werkzeugaufrufen pro Nutzernachricht (Kosten-/Schleifenschutz).
MAX_TOOL_ROUNDS = 20
TOOL_LIMIT_MESSAGE = "Ich konnte die Anfrage nicht vollständig bearbeiten. Formuliere sie bitte etwas einfacher."
TOOL_LIMIT_SITUATION = (
    "Die Anfrage war zu verwickelt, du bist beim Bearbeiten nicht zum Ziel gekommen und hast sie nicht "
    "vollständig erledigt. Der Nutzer soll sie bitte einfacher oder in kleineren Schritten formulieren."
)
# Kurze Nachrichten sollen schnell kommen; klappt die Formulierung durch das Modell nicht, gilt der feste Text.
NOTICE_TIMEOUT_SECONDS = 10.0
MAX_NOTICE_CHARS = 800
# So viele der letzten Verlaufsnachrichten sehen Begrüßungen/Abschiede als Stilanker (Sprache, Anrede, Ton).
NOTICE_STYLE_MESSAGES = 4
# Platzhalter im gespeicherten Verlauf für Nachrichten mit Bild.
IMAGE_HISTORY_PREFIX = "[Bild gesendet]"
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
        image_model: ChatModel | None = None,
        image_history_messages: int = DEFAULT_IMAGE_HISTORY_MESSAGES,
    ) -> None:
        self._chat_model = chat_model
        self._image_model = image_model
        self._image_history_messages = image_history_messages
        self._conversations = conversations
        self._system_prompt = system_prompt
        self._tools = tools
        self._timezone = timezone
        self._now = now or (lambda: datetime.now(timezone))

    @property
    def supports_images(self) -> bool:
        return self._image_model is not None

    async def reply(self, chat_id: int, user_text: str, image: ImageInput | None = None) -> str:
        """Erzeugt eine Antwort und speichert den Austausch erst nach Erfolg.

        Schlägt die LLM-Anfrage fehl, bleibt der Verlauf unverändert. So entstehen
        keine "verwaisten" Nutzernachrichten ohne Antwort im Kontext.

        Mit ``image`` beantwortet das Vision-Modell die Nachricht. Das Bild selbst wird
        nicht gespeichert, im Verlauf steht nur ein Platzhalter mit dem Text.
        """
        if image is not None and self._image_model is None:
            raise ValueError("Bildverarbeitung ist nicht aktiviert (kein Bildmodell konfiguriert).")
        model = self._chat_model if image is None else self._image_model
        assert model is not None
        history = await self._conversations.get_history(
            chat_id, limit=None if image is None else self._image_history_messages
        )
        system_prompt = self._system_prompt
        if self._tools:
            system_prompt += "\n" + self._date_context()
        messages = [
            ChatMessage(role="system", content=system_prompt),
            *history,
            ChatMessage(role="user", content=user_text, images=() if image is None else (image,)),
        ]
        if self._tools:
            answer = await self._reply_with_tools(chat_id, messages, model)
        else:
            answer = await model.complete(messages)
        # Gespeichert werden nur Frage und Antwort – keine Werkzeugergebnisse (z. B. E-Mail-Inhalte) und keine Bilder.
        stored_text = user_text if image is None else f"{IMAGE_HISTORY_PREFIX} {user_text}"
        await self._conversations.add_exchange(chat_id, stored_text, answer)
        logger.info(
            "Antwort erzeugt (chat_id=%s, Kontext=%d Nachrichten, Eingabe=%d Zeichen, Bild=%s, Antwort=%d Zeichen).",
            chat_id,
            len(history),
            len(user_text),
            "nein" if image is None else f"{image.mime_type}/{len(image.data)} Bytes",
            len(answer),
        )
        return answer

    async def announce(self, instruction: str, chat_id: int | None = None) -> str:
        """Lässt das Modell eine kurze, eigenständige Nachricht schreiben (z. B. Start-/Stoppmeldung).

        Mit Systemprompt und aktuellem Datum, ohne Werkzeuge. Mit ``chat_id`` gehen die letzten Nachrichten des
        Chats als Stilanker voran, damit Sprache, Anrede und Ton zum bisherigen Gespräch passen. Es wird nichts
        gespeichert.
        """
        system_prompt = f"{self._system_prompt}\n{self._date_context()}"
        anchor: list[ChatMessage] = []
        if chat_id is not None:
            try:
                anchor = await self._conversations.get_history(chat_id, limit=NOTICE_STYLE_MESSAGES)
            except (sqlite3.Error, OSError) as exc:
                logger.warning("Stilanker nicht verfügbar (%s), schreibe ohne Verlauf.", type(exc).__name__)
        return await self._chat_model.complete(
            [
                ChatMessage(role="system", content=system_prompt),
                *anchor,
                ChatMessage(role="user", content=instruction),
            ]
        )

    async def write_notice(
        self,
        instruction: str,
        fallback: str,
        timeout: float = NOTICE_TIMEOUT_SECONDS,
        chat_id: int | None = None,
        max_chars: int = MAX_NOTICE_CHARS,
    ) -> str:
        """Lässt das Modell eine kurze Nachricht in der Persönlichkeit des Assistenten schreiben (Begrüßung, Fehlererklärung usw.).

        Bei Modellfehler, Zeitüberschreitung oder unbrauchbarer Antwort wird der feste ``fallback``
        zurückgegeben; diese Methode löst nie einen Modellfehler aus.
        """
        try:
            text = await asyncio.wait_for(self.announce(instruction, chat_id=chat_id), timeout)
        except (LLMError, TimeoutError) as exc:
            logger.warning("Nachricht nicht vom Modell formuliert (%s), nutze festen Text.", type(exc).__name__)
            return fallback
        text = text.strip()
        if not 0 < len(text) <= max_chars:
            logger.warning("Nachricht des Modells unbrauchbar (%d Zeichen), nutze festen Text.", len(text))
            return fallback
        return text

    async def record_notice(self, chat_id: int, marker: str, text: str) -> None:
        """Speichert eine selbst gesendete Nachricht im Verlauf, damit der Nutzer direkt darauf antworten kann.

        ``marker`` steht als Nutzerzeile davor, damit der Verlauf mit einer Nutzernachricht beginnt.
        """
        await self._conversations.add_exchange(chat_id, marker, text)

    async def explain_problem(self, situation: str, fallback: str) -> str:
        """Lässt das Modell einen Fehler menschlich erklären; ``situation`` beschreibt sachlich, was schiefging."""
        return await self.write_notice(ERROR_INSTRUCTION.format(situation=situation), fallback)

    async def _reply_with_tools(self, chat_id: int, messages: list[ChatMessage], model: ChatModel) -> str:
        assert self._tools is not None
        specs = self._tools.specs()
        for _ in range(MAX_TOOL_ROUNDS):
            turn = await model.complete_with_tools(messages, specs)
            if not turn.tool_calls:
                return turn.content
            messages.append(ChatMessage(role="assistant", content=turn.content, tool_calls=turn.tool_calls))
            for call in turn.tool_calls:
                result = await self._tools.execute(chat_id, call)
                messages.append(ChatMessage(role="tool", content=result, tool_call_id=call.call_id))
        logger.warning("Maximale Anzahl an Werkzeugrunden erreicht (chat_id=%s).", chat_id)
        return await self.explain_problem(TOOL_LIMIT_SITUATION, TOOL_LIMIT_MESSAGE)

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
        if self._image_model is not None and self._image_model is not self._chat_model:
            await self._image_model.aclose()
