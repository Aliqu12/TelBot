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
from ..prompts import START_COMMAND_INSTRUCTION
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

# Fehler werden nicht als feste Texte ausgegeben: Das Modell formuliert sie in der Persönlichkeit des Assistenten.
# Jeder Eintrag besteht aus der sachlichen Beschreibung ("Situation") für das Modell und einem festen
# Ersatztext, der nur gilt, wenn das Modell die Antwort nicht rechtzeitig liefern kann.
_Problem = tuple[str, str]

_GENERIC_PROBLEM: _Problem = (
    "Bei der Bearbeitung ist etwas Unerwartetes schiefgelaufen. Der Nutzer soll es später noch einmal versuchen.",
    "Sorry, da ist gerade etwas schiefgelaufen. Versuch es bitte gleich noch einmal.",
)

_LLM_PROBLEMS: dict[type[LLMError], _Problem] = {
    LLMRateLimitError: (
        "Du bist gerade überlastet: Es gab zu viele Anfragen in kurzer Zeit (Anfragelimit erreicht). "
        "Der Nutzer soll einen Moment warten und es dann noch einmal versuchen.",
        "Sorry, ich komme gerade nicht hinterher, das Anfragelimit ist erreicht. Gib mir einen Moment "
        "und versuch es dann noch einmal.",
    ),
    LLMRequestTooLargeError: (
        "Die Anfrage war zu umfangreich, um sie zu bearbeiten, oft wegen des langen Gesprächsverlaufs. "
        "Mit dem Befehl /reset kann der Nutzer den Verlauf löschen und neu beginnen. Bei Bildern hilft auch ein kleineres Foto.",
        "Sorry, das war zu viel auf einmal. Schick /reset, um den Verlauf zu löschen, und versuch es dann "
        "noch einmal. Bei Bildern hilft auch ein kleineres Foto.",
    ),
    LLMInsufficientCreditsError: (
        "Das Guthaben für die Arbeit ist aufgebraucht, du kannst im Moment nicht antworten. "
        "Es muss erst wieder aufgeladen werden.",
        "Sorry, das Guthaben ist aufgebraucht, ich kann gerade nicht arbeiten. Es muss erst wieder aufgeladen werden.",
    ),
    LLMTimeoutError: (
        "Du hast zu lange gebraucht und bist nicht rechtzeitig fertig geworden. Der Nutzer soll es noch einmal versuchen.",
        "Sorry, das hat zu lange gedauert. Versuch es bitte noch einmal.",
    ),
    LLMUnavailableError: (
        "Du bist gerade nicht erreichbar bzw. kommst nicht an das, was du zum Antworten brauchst. "
        "Der Nutzer soll es später noch einmal versuchen.",
        "Sorry, ich komme gerade nicht durch. Versuch es später bitte noch einmal.",
    ),
    LLMConfigurationError: (
        "Du hast keinen Zugang mehr zu dem, was du zum Antworten brauchst (Zugangsdaten oder Einstellungen "
        "sind falsch). Der Nutzer muss die Konfiguration prüfen, du kannst das nicht selbst beheben.",
        "Sorry, ich komme gerade nicht an meine Arbeitsmittel. Bitte API-Key und Modellnamen in der "
        "Konfiguration prüfen.",
    ),
    LLMEmptyResponseError: (
        "Dir ist auf die Nachricht keine Antwort eingefallen. Der Nutzer soll die Frage gern etwas anders formulieren.",
        "Dazu ist mir gerade nichts eingefallen. Formuliere die Frage gern etwas anders.",
    ),
}

# Wird verwendet, wenn ein Bild ohne Bildunterschrift kommt.
DEFAULT_IMAGE_PROMPT = (
    "Was ist auf diesem Bild zu sehen? Falls es Text enthält, gib den Text vollständig und wortgetreu wieder."
)
_IMAGES_DISABLED_SITUATION = "Der Nutzer hat dir ein Bild geschickt, aber Bilder kannst du im Moment nicht ansehen."
IMAGES_DISABLED_MESSAGE = "Bilder kann ich im Moment leider nicht ansehen."
_IMAGE_DOWNLOAD_PROBLEM: _Problem = (
    "Das Bild ist bei dir nicht angekommen, es konnte nicht geladen werden. Der Nutzer soll es noch einmal schicken.",
    "Ich konnte das Bild nicht von Telegram laden. Bitte schick es noch einmal.",
)
_IMAGE_LLM_PROBLEM: _Problem = (
    "Du konntest das Bild nicht auswerten. Der Nutzer soll es mit einem anderen Foto versuchen oder es als normales "
    "Foto statt als Datei schicken.",
    "Das Bild konnte nicht verarbeitet werden. Versuch es bitte mit einem anderen Foto oder schick es "
    "als normales Foto statt als Datei.",
)
_UNSUPPORTED_PROBLEM: _Problem = (
    "Der Nutzer hat dir etwas geschickt (z. B. Sprachnachricht, Sticker oder Datei), das du nicht verarbeiten kannst. "
    "Du kannst nur Text lesen.",
    "Ich kann derzeit nur Textnachrichten verarbeiten.",
)
_UNSUPPORTED_WITH_IMAGES_PROBLEM: _Problem = (
    "Der Nutzer hat dir etwas geschickt (z. B. Sprachnachricht, Sticker oder Datei), das du nicht verarbeiten kannst. "
    "Du kannst nur Text und Fotos ansehen.",
    "Ich kann derzeit nur Text- und Bildnachrichten (Fotos) verarbeiten.",
)
_UNKNOWN_COMMAND_PROBLEM: _Problem = (
    "Der Nutzer hat einen Befehl (/...) geschickt, den es nicht gibt. Mit /help bekommt er alle Befehle angezeigt.",
    "Diesen Befehl kenne ich nicht. Mit /help siehst du alle Befehle.",
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
        web_enabled: bool = False,
    ) -> None:
        self._assistant = assistant
        self._access = access
        self._owner_name = owner_name
        self._max_input_chars = max_input_chars
        self._max_image_bytes = max_image_bytes
        self._email_confirmation = email_confirmation
        self._task_confirmation = task_confirmation
        self._web_enabled = web_enabled

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not await self._authorize(user.id, message, "/start"):
            return
        greeting = await self._assistant.write_notice(
            START_COMMAND_INSTRUCTION.format(owner_name=self._owner_name),
            f"Hallo {self._owner_name}! Schreib mir einfach eine Nachricht. "
            "Mit /help siehst du, was ich kann.",
        )
        await _reply(message, greeting)

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
        if self._web_enabled:
            access_line += (
                "- Ich kann im Internet suchen und Webseiten lesen, z. B. für aktuelle Infos oder wenn du mir "
                "einen Link schickst.\n"
                "- Auf deine Dateien habe ich keinen Zugriff.\n\n"
            )
        else:
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
            await self._reply_problem(
                message,
                f"Die Nachricht des Nutzers hat {len(text)} Zeichen, erlaubt sind höchstens {self._max_input_chars}. "
                "Der Nutzer soll sie bitte kürzen.",
                f"Deine Nachricht ist mit {len(text)} Zeichen zu lang. "
                f"Bitte kürze sie auf höchstens {self._max_input_chars} Zeichen.",
            )
            return

        async with _typing_indicator(context.bot, chat.id):
            try:
                answer = await self._assistant.reply(chat.id, text)
            except LLMError as exc:
                answer = await self._llm_problem_text(exc, _GENERIC_PROBLEM)
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
            await self._reply_problem(message, _IMAGES_DISABLED_SITUATION, IMAGES_DISABLED_MESSAGE)
            return

        caption = (message.caption or "").strip()
        if len(caption) > self._max_input_chars:
            await self._reply_problem(
                message,
                f"Die Bildunterschrift hat {len(caption)} Zeichen, erlaubt sind höchstens "
                f"{self._max_input_chars}. Der Nutzer soll sie bitte kürzen.",
                f"Die Bildunterschrift ist mit {len(caption)} Zeichen zu lang. "
                f"Bitte kürze sie auf höchstens {self._max_input_chars} Zeichen.",
            )
            return
        # Bei Fotos liefert Telegram mehrere Größen; die letzte ist die größte.
        attachment = message.photo[-1] if message.photo else message.document
        if attachment is None:
            return
        if attachment.file_size and attachment.file_size > self._max_image_bytes:
            problem = too_large_message(attachment.file_size, self._max_image_bytes)
            await self._reply_problem(message, f"Das Bild ist zu groß: {problem}", problem)
            return

        async with _typing_indicator(context.bot, chat.id):
            try:
                telegram_file = await attachment.get_file()
                data = bytes(await telegram_file.download_as_bytearray())
            except TelegramError as exc:
                logger.warning("Bild konnte nicht von Telegram geladen werden: %s", type(exc).__name__)
                await self._reply_problem(message, *_IMAGE_DOWNLOAD_PROBLEM)
                return
            try:
                image = load_image(data, self._max_image_bytes)
            except ImageRejectedError as exc:
                logger.info("Bild abgelehnt (%d Bytes): %s", len(data), exc)
                await self._reply_problem(message, f"Das Bild kann nicht verarbeitet werden: {exc}", str(exc))
                return
            try:
                answer = await self._assistant.reply(chat.id, caption or DEFAULT_IMAGE_PROMPT, image=image)
            except LLMError as exc:
                answer = await self._llm_problem_text(exc, _IMAGE_LLM_PROBLEM)
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
            await self._reply_problem(message, *_UNSUPPORTED_WITH_IMAGES_PROBLEM)
        else:
            await self._reply_problem(message, *_UNSUPPORTED_PROBLEM)

    async def unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user = update.effective_message, update.effective_user
        if message is None or user is None or not await self._authorize(user.id, message, "unbekannter Befehl"):
            return
        await self._reply_problem(message, *_UNKNOWN_COMMAND_PROBLEM)

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
            await message.reply_text(await self._assistant.explain_problem(*_GENERIC_PROBLEM))

    async def _reply_problem(self, message: Message, situation: str, fallback: str) -> None:
        """Antwortet mit einer vom Modell formulierten Fehlererklärung (oder dem festen Ersatztext)."""
        await _reply(message, await self._assistant.explain_problem(situation, fallback))

    async def _llm_problem_text(self, error: LLMError, default: _Problem) -> str:
        situation, fallback = _LLM_PROBLEMS.get(type(error), default)
        return await self._assistant.explain_problem(situation, fallback)

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
