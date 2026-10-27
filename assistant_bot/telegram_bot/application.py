"""Aufbau der python-telegram-bot Application."""

from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters

from ..assistant import Assistant
from ..config import Settings
from .access import AccessPolicy
from .handlers import BotHandlers

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Begrüßung"),
    BotCommand("help", "Hilfe und Funktionen"),
    BotCommand("reset", "Gesprächsverlauf löschen"),
    BotCommand("whoami", "Eigene Telegram User-ID anzeigen"),
]


def build_application(settings: Settings, assistant: Assistant) -> Application:
    access = AccessPolicy(settings.allowed_telegram_user_id)
    handlers = BotHandlers(
        assistant=assistant,
        access=access,
        owner_name=settings.owner_name,
        max_input_chars=settings.max_input_chars,
    )

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot verbunden als @%s", application.bot.username)
        if not access.is_configured:
            logger.warning(
                "ALLOWED_TELEGRAM_USER_ID ist nicht gesetzt: Einrichtungsmodus aktiv. "
                "Nur /whoami funktioniert, es werden keine KI-Anfragen gestellt."
            )

    async def post_shutdown(application: Application) -> None:
        await assistant.aclose()
        logger.info("Bot beendet.")

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
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
    application.add_handler(MessageHandler(base & ~filters.TEXT, handlers.unsupported_message))
    application.add_error_handler(handlers.on_error)
    return application
