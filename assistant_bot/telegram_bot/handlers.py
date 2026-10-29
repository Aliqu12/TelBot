"""Telegram-Handler: Befehle, Textnachrichten und Fehlerbehandlung.

Die Handler sind dünn: Sie prüfen Zugriff und Eingabe, delegieren an den
``Assistant`` und übersetzen Fehler in verständliche Nachrichten.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from telegram import Bot, Message, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TelegramError
from telegram.ext import ContextTypes

from ..assistant import Assistant
from ..images import DEFAULT_MAX_IMAGE_BYTES, ImageRejectedError, load_image, too_large_message
from ..llm import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMInsufficientCreditsError,
    LLMRateLimitError,
    LLMRequestTooLargeError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from .access import AccessPolicy
from .email_confirmation import EmailConfirmationHandlers
from .message_split import split_message
from .task_confirmation import TaskConfirmationHandlers

logger = logging.getLogger(__name__)

# Telegram zeigt "schreibt..." ca. 5 Sekunden lang an.
_TYPING_INTERVAL_SECONDS = 4.0

SETUP_HINT = (
    "Hallo! Dieser Assistent ist noch nicht eingerichtet.\n\n"
    "1. Sende /whoami, um deine Telegram User-ID zu erfahren.\n"
    "2. Trage sie als ALLOWED_TELEGRAM_USER_ID in die Konfiguration (.env) ein.\n"
    "3. Starte den Bot neu."
)

_GENERIC_ERROR_MESSAGE = "Da ist leider etwas schiefgelaufen. Bitte versuche es später noch einmal."

_LLM_ERROR_MESSAGES: dict[type[LLMError], str] = {
    LLMRateLimitError: (
        "Das Anfragelimit des Sprachmodells ist gerade erreicht. Bitte warte einen Moment und versuche es dann erneut."
    ),
    LLMRequestTooLargeError: (
        "Die Anfrage ist für das Sprachmodell zu groß. Schick /reset, um den Verlauf zu löschen, "
        "und versuche es dann noch einmal. Bei Bildern hilft auch ein kleineres Foto."
    ),
    LLMInsufficientCreditsError: (
        "Das Guthaben beim Sprachmodell-Anbieter ist aufgebraucht. Es muss erst wieder aufgeladen werden."
    ),
    LLMTimeoutError: "Das Sprachmodell hat nicht rechtzeitig geantwortet. Bitte versuche es noch einmal.",
    LLMUnavailableError: "Das Sprachmodell ist momentan nicht erreichbar. Bitte versuche es später erneut.",
    LLMConfigurationError: (
        "Der Zugriff auf das Sprachmodell ist fehlgeschlagen. Bitte API-Key und Modellnamen in der Konfiguration prüfen."
    ),
    LLMEmptyResponseError: "Ich habe leider keine Antwort erhalten. Formuliere die Frage gern etwas anders.",
}

# Wird verwendet, wenn ein Bild ohne Bildunterschrift kommt.
DEFAULT_IMAGE_PROMPT = (
    "Was ist auf diesem Bild zu sehen? Falls es Text enthält, gib den Text vollständig und wortgetreu wieder."
)
IMAGES_DISABLED_MESSAGE = "Bilder kann ich im Moment nicht verarbeiten."
_IMAGE_DOWNLOAD_ERROR = "Ich konnte das Bild nicht von Telegram laden. Bitte schick es noch einmal."
_IMAGE_LLM_ERROR = (
    "Das Bild konnte nicht verarbeitet werden. Versuch es bitte mit einem anderen Foto oder schick es "
    "als normales Foto statt als Datei."
)


class BotHandlers:
    def __init__(
        self,
        *,
        assistant: Assistant,
        access: AccessPolicy,
        owner_name: str,
        max_input_chars: int,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        email_confirmation: EmailConfirmationHandlers | None = None,
        task_confirmation: TaskConfirmationHandlers | None = None,
    ) -> None:
        self._assistant = assistant
        self._access = access
        self._owner_name = owner_name
        self._max_input_chars = max_input_chars
        self._max_image_bytes = max_image_bytes
        self._email_confirmation = email_confirmation
        self._task_confirmation = task_confirmation

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not await self._authorize(user.id, message, "/start"):
            return
        await _reply(
            message,
            f"Hallo {self._owner_name}! Ich bin dein persönlicher KI-Assistent.\n\n"
            "Schreib mir einfach eine Nachricht. Mit /help siehst du, was ich kann.",
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not await self._authorize(user.id, message, "/help"):
            return
        access_line = ""
        if self._email_confirmation is not None:
            access_line += (
                "- Ich kann dein E-Mail-Postfach lesen, durchsuchen und Entwürfe schreiben. "
                'Versendet wird erst, wenn du beim Entwurf auf "Senden" tippst.\n'
            )
        if self._task_confirmation is not None:
            access_line += (
                "- Ich verwalte deine Todoist-Aufgaben: anlegen (auch \"erinnere mich ...\"), anzeigen, "
                'verschieben und erledigen. Gelöscht wird erst, wenn du auf "Löschen" tippst.\n'
            )
        if self._assistant.supports_images:
            access_line += (
                "- Schick mir ein Foto (gern mit einer Frage als Bildunterschrift), dann lese ich den Text "
                "daraus oder beschreibe, was zu sehen ist.\n"
            )
        access_line += "- Ich habe keinen Zugriff auf das Internet oder auf deine Dateien.\n\n"
        await _reply(
            message,
            "So funktioniere ich:\n"
            "- Schreib mir einfach eine Textnachricht, ich antworte direkt.\n"
            "- Ich merke mir die letzten Nachrichten unseres Gesprächs, auch nach einem Neustart.\n"
            + access_line
            + "Befehle:\n"
            "/start - Begrüßung\n"
            "/help - Diese Hilfe\n"
            "/reset - Gesprächsverlauf löschen und neu beginnen\n"
            "/whoami - Deine Telegram User-ID anzeigen",
        )

    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or chat is None:
            return
        if not await self._authorize(user.id, message, "/reset"):
            return
        await self._assistant.reset(chat.id)
        await _reply(message, "Der Gesprächsverlauf wurde gelöscht. Wir beginnen von vorn.")

    async def whoami_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None:
            return
        if not self._access.may_use_whoami(user.id):
            logger.warning("Zugriff verweigert: user_id=%s, Aktion=/whoami", user.id)
            return
        text = f"Deine Telegram User-ID: {user.id}"
        if not self._access.is_configured:
            text += "\n\nTrage diese Zahl als ALLOWED_TELEGRAM_USER_ID in die .env ein und starte den Bot neu."
        await _reply(message, text)

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or chat is None:
            return
        # Zugriffsprüfung VOR jeder LLM-Anfrage: Fremde verursachen keine Kosten.
        if not await self._authorize(user.id, message, "Nachricht"):
            return

        text = (message.text or "").strip()
        if not text:
            return
        if len(text) > self._max_input_chars:
            await _reply(
                message,
                f"Deine Nachricht ist mit {len(text)} Zeichen zu lang. "
                f"Bitte kürze sie auf höchstens {self._max_input_chars} Zeichen.",
            )
            return

        async with _typing_indicator(context.bot, chat.id):
            try:
                answer = await self._assistant.reply(chat.id, text)
            except LLMError as exc:
                answer = _LLM_ERROR_MESSAGES.get(type(exc), _GENERIC_ERROR_MESSAGE)
        await _reply(message, answer)
        await self._present_confirmations(context.bot, chat.id)

    async def image_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Foto oder Bilddatei: wird mit der Bildunterschrift an das Vision-Modell gegeben."""
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or chat is None:
            return
        # Zugriffsprüfung VOR dem Herunterladen und jeder LLM-Anfrage.
        if not await self._authorize(user.id, message, "Bild"):
            return
        if not self._assistant.supports_images:
            await _reply(message, IMAGES_DISABLED_MESSAGE)
            return

        caption = (message.caption or "").strip()
        if len(caption) > self._max_input_chars:
            await _reply(
                message,
                f"Die Bildunterschrift ist mit {len(caption)} Zeichen zu lang. "
                f"Bitte kürze sie auf höchstens {self._max_input_chars} Zeichen.",
            )
            return
        # Bei Fotos liefert Telegram mehrere Größen; die letzte ist die größte.
        attachment = message.photo[-1] if message.photo else message.document
        if attachment is None:
            return
        if attachment.file_size and attachment.file_size > self._max_image_bytes:
            await _reply(message, too_large_message(attachment.file_size, self._max_image_bytes))
            return

        async with _typing_indicator(context.bot, chat.id):
            try:
                telegram_file = await attachment.get_file()
                data = bytes(await telegram_file.download_as_bytearray())
            except TelegramError as exc:
                logger.warning("Bild konnte nicht von Telegram geladen werden: %s", type(exc).__name__)
                await _reply(message, _IMAGE_DOWNLOAD_ERROR)
                return
            try:
                image = load_image(data, self._max_image_bytes)
            except ImageRejectedError as exc:
                logger.info("Bild abgelehnt (%d Bytes): %s", len(data), exc)
                await _reply(message, str(exc))
                return
            try:
                answer = await self._assistant.reply(chat.id, caption or DEFAULT_IMAGE_PROMPT, image=image)
            except LLMError as exc:
                answer = _LLM_ERROR_MESSAGES.get(type(exc), _IMAGE_LLM_ERROR)
        await _reply(message, answer)
        await self._present_confirmations(context.bot, chat.id)

    async def _present_confirmations(self, bot: Bot, chat_id: int) -> None:
        if self._email_confirmation is not None:
            # Vom Modell erstellte/geänderte Entwürfe mit Freigabe-Schaltflächen anzeigen.
            await self._email_confirmation.present_pending_draft(bot, chat_id)
        if self._task_confirmation is not None:
            # Vom Modell vorbereitete Löschungen mit Bestätigungs-Schaltflächen anzeigen.
            await self._task_confirmation.present_pending_deletion(bot, chat_id)

    async def unsupported_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not await self._authorize(user.id, message, "Nicht-Text"):
            return
        if self._assistant.supports_images:
            await _reply(message, "Ich kann derzeit nur Text- und Bildnachrichten (Fotos) verarbeiten.")
        else:
            await _reply(message, "Ich kann derzeit nur Textnachrichten verarbeiten.")

    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not await self._authorize(user.id, message, "unbekannter Befehl"):
            return
        await _reply(message, "Diesen Befehl kenne ich nicht. Mit /help siehst du alle Befehle.")

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if isinstance(error, NetworkError):
            # Kurzzeitige Verbindungsprobleme (z. B. beim Polling) sind normal und
            # werden von python-telegram-bot automatisch erneut versucht.
            logger.warning("Netzwerkproblem bei der Kommunikation mit Telegram: %s", error)
            return

        logger.error("Unerwarteter Fehler bei der Verarbeitung eines Updates.", exc_info=error)
        if not isinstance(update, Update):
            return
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not self._access.is_authorized(user.id):
            return
        with contextlib.suppress(TelegramError):
            await message.reply_text(_GENERIC_ERROR_MESSAGE)

    async def _authorize(self, user_id: int, message: Message, action: str) -> bool:
        if self._access.is_authorized(user_id):
            return True
        if not self._access.is_configured:
            logger.info("Einrichtungsmodus: Hinweis an user_id=%s gesendet (Aktion=%s).", user_id, action)
            await _reply(message, SETUP_HINT)
        else:
            # Fremde Nutzer erhalten keine Antwort, damit der Bot nicht "gefunden" wird.
            logger.warning("Zugriff verweigert: user_id=%s, Aktion=%s", user_id, action)
        return False


async def _reply(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply_text(chunk)


@contextlib.asynccontextmanager
async def _typing_indicator(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """Zeigt "schreibt..." an, solange der Block läuft."""

    async def keep_typing() -> None:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except TelegramError as exc:
                logger.debug("Tipp-Indikator konnte nicht gesendet werden: %s", exc)
            await asyncio.sleep(_TYPING_INTERVAL_SECONDS)

    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
