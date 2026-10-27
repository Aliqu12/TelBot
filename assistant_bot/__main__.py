"""Einstiegspunkt: ``python -m assistant_bot``."""

from __future__ import annotations

import logging
import sqlite3
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.error import InvalidToken

from .assistant import Assistant
from .config import ConfigError, load_settings
from .conversation import ConversationService
from .llm import create_groq_chat_model
from .logging_config import configure_logging
from .prompts import build_system_prompt
from .telegram_bot.application import build_application

logger = logging.getLogger("assistant_bot")


def main() -> int:
    # Lokal: Werte aus .env laden. Bereits gesetzte Umgebungsvariablen
    # (z. B. aus Docker Compose) haben Vorrang und werden nicht überschrieben.
    load_dotenv()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    configure_logging(settings.log_level, secrets=settings.secrets)
    logger.info(
        "Starte Assistent (Modell=%s, Kontext=%d Nachrichten, Datenbank=%s)",
        settings.groq_model,
        settings.history_max_messages,
        settings.database_path,
    )

    conversations = ConversationService(settings.database_path, settings.history_max_messages)
    try:
        conversations.initialize()
    except (OSError, sqlite3.Error) as exc:
        logger.critical("Datenbank %s konnte nicht initialisiert werden: %s", settings.database_path, exc)
        return 1

    assistant = Assistant(
        chat_model=create_groq_chat_model(settings),
        conversations=conversations,
        system_prompt=build_system_prompt(settings.owner_name),
    )
    application = build_application(settings, assistant)

    try:
        # Long Polling; beendet sich sauber bei SIGINT/SIGTERM (z. B. docker stop).
        application.run_polling(allowed_updates=[Update.MESSAGE])
    except InvalidToken:
        logger.critical("Telegram hat den Bot-Token abgelehnt. TELEGRAM_BOT_TOKEN prüfen.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
