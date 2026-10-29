"""Aufbau der python-telegram-bot Application."""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, BotCommand
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from ..assistant import Assistant
from ..config import Settings
from ..llm import LLMError
from ..mail.service import EmailService
from ..prompts import SHUTDOWN_INSTRUCTION, STARTUP_INSTRUCTION
from ..status_limiter import StatusRateLimiter
from ..tasks.service import TaskService
from .access import AccessPolicy
from .email_confirmation import CALLBACK_PATTERN, EmailConfirmationHandlers
from .handlers import BotHandlers
from .task_confirmation import CALLBACK_PATTERN as TASK_CALLBACK_PATTERN
from .task_confirmation import TaskConfirmationHandlers

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Begrüßung"),
    BotCommand("help", "Hilfe und Funktionen"),
    BotCommand("reset", "Gesprächsverlauf löschen"),
    BotCommand("whoami", "Eigene Telegram User-ID anzeigen"),
]

# Feste Texte, falls das Sprachmodell nicht rechtzeitig antwortet (Limit, Netzwerk, Zeitlimit).
STARTUP_FALLBACK = "Hallo {owner}, Firzana ist online und einsatzbereit."
SHUTDOWN_FALLBACK = "Firzana ist kurz nicht erreichbar (Neustart oder Wartung). Ich melde mich, sobald ich wieder da bin."
# Beim Start darf die Begrüßung etwas dauern; beim Stoppen muss sie vor dem SIGKILL von Docker (Standard 10 s) fertig sein.
STARTUP_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
MAX_STATUS_CHARS = 800


async def notify_owner(
    bot: Bot,
    assistant: Assistant,
    owner_id: int | None,
    instruction: str,
    fallback: str,
    timeout: float,
    limiter: StatusRateLimiter | None = None,
) -> None:
    """Schickt dem Besitzer eine vom Sprachmodell formulierte Statusmeldung.

    Bei Fehler, Zeitüberschreitung oder unbrauchbarer Antwort wird der feste ``fallback`` gesendet.
    Ist das Limit des ``limiter`` erreicht, wird gar nicht erst beim Sprachmodell angefragt.
    Auch Sendefehler werden nur geloggt, der Bot läuft bzw. stoppt trotzdem.
    """
    if owner_id is None:
        return
    text = fallback
    if limiter is not None and not await limiter.try_acquire():
        logger.info("Limit für KI-Statusmeldungen erreicht, sende festen Text ohne LLM-Anfrage.")
    else:
        try:
            generated = (await asyncio.wait_for(assistant.announce(instruction), timeout)).strip()
            if 0 < len(generated) <= MAX_STATUS_CHARS:
                text = generated
            else:
                logger.warning(
                    "Statusmeldung des Modells unbrauchbar (%d Zeichen), nutze festen Text.", len(generated)
                )
        except (LLMError, TimeoutError) as exc:
            logger.warning("Statusmeldung nicht vom Modell erzeugt (%s), nutze festen Text.", type(exc).__name__)
    try:
        await bot.send_message(chat_id=owner_id, text=text)
    except TelegramError as exc:
        logger.warning("Statusmeldung konnte nicht gesendet werden: %s", type(exc).__name__)


def build_application(
    settings: Settings,
    assistant: Assistant,
    email_service: EmailService | None = None,
    task_service: TaskService | None = None,
    status_limiter: StatusRateLimiter | None = None,
) -> Application:
    access = AccessPolicy(settings.allowed_telegram_user_id)
    email_confirmation = (
        EmailConfirmationHandlers(email=email_service, access=access) if email_service is not None else None
    )
    task_confirmation = (
        TaskConfirmationHandlers(tasks=task_service, access=access) if task_service is not None else None
    )
    handlers = BotHandlers(
        assistant=assistant,
        access=access,
        owner_name=settings.owner_name,
        max_input_chars=settings.max_input_chars,
        max_image_bytes=settings.image_max_bytes,
        email_confirmation=email_confirmation,
        task_confirmation=task_confirmation,
    )

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot verbunden als @%s", application.bot.username)
        if not access.is_configured:
            logger.warning(
                "ALLOWED_TELEGRAM_USER_ID ist nicht gesetzt: Einrichtungsmodus aktiv. "
                "Nur /whoami funktioniert, es werden keine KI-Anfragen gestellt."
            )
        if not settings.status_messages_enabled:
            logger.info("Statusmeldungen sind deaktiviert (STATUS_MESSAGES_ENABLED=false).")
            return
        await notify_owner(
            application.bot,
            assistant,
            settings.allowed_telegram_user_id,
            STARTUP_INSTRUCTION.format(owner_name=settings.owner_name),
            STARTUP_FALLBACK.format(owner=settings.owner_name),
            STARTUP_TIMEOUT_SECONDS,
            status_limiter,
        )

    async def post_stop(application: Application) -> None:
        # Läuft nach dem Ende des Pollings, aber bevor der Bot-Client geschlossen wird (docker stop = SIGTERM).
        if not settings.status_messages_enabled:
            return
        await notify_owner(
            application.bot,
            assistant,
            settings.allowed_telegram_user_id,
            SHUTDOWN_INSTRUCTION.format(owner_name=settings.owner_name),
            SHUTDOWN_FALLBACK,
            SHUTDOWN_TIMEOUT_SECONDS,
            status_limiter,
        )

    async def post_shutdown(application: Application) -> None:
        await assistant.aclose()
        if task_service is not None:
            await task_service.aclose()
        logger.info("Bot beendet.")

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Nur neue Nachrichten in privaten Chats; Gruppen und bearbeitete Nachrichten werden ignoriert.
    base = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

    application.add_handler(CommandHandler("start", handlers.start_command, filters=base))
    application.add_handler(CommandHandler("help", handlers.help_command, filters=base))
    application.add_handler(CommandHandler("reset", handlers.reset_command, filters=base))
    application.add_handler(CommandHandler("whoami", handlers.whoami_command, filters=base))
    application.add_handler(MessageHandler(base & filters.COMMAND, handlers.unknown_command))
    application.add_handler(MessageHandler(base & filters.TEXT, handlers.text_message))
    application.add_handler(MessageHandler(base & (filters.PHOTO | filters.Document.IMAGE), handlers.image_message))
    application.add_handler(MessageHandler(base & ~filters.TEXT, handlers.unsupported_message))
    if email_confirmation is not None:
        application.add_handler(CallbackQueryHandler(email_confirmation.callback, pattern=CALLBACK_PATTERN))
    if task_confirmation is not None:
        application.add_handler(CallbackQueryHandler(task_confirmation.callback, pattern=TASK_CALLBACK_PATTERN))
    application.add_error_handler(handlers.on_error)
    return application
