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
from .mail.factory import create_email_provider
from .mail.service import EmailService
from .mail.tools import build_email_tools
from .prompts import build_system_prompt
from .telegram_bot.application import build_application
from .tools import ToolRegistry

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

    email_service: EmailService | None = None
    tools: ToolRegistry | None = None
    if settings.email is not None:
        email_service = EmailService(
            create_email_provider(settings.email),
            allowed_recipients=settings.email.allowed_recipients,
            draft_ttl_seconds=settings.email.draft_ttl_seconds,
            max_body_chars=settings.email.max_body_chars,
        )
        tools = ToolRegistry(build_email_tools(email_service))
        logger.info(
            "E-Mail-Integration aktiv (Provider=%s, Empfänger-Freigabeliste=%s).",
            email_service.provider_name,
            "ja" if settings.email.allowed_recipients else "nein",
        )

    assistant = Assistant(
        chat_model=create_groq_chat_model(settings),
        conversations=conversations,
        system_prompt=build_system_prompt(settings.owner_name, email_enabled=email_service is not None),
        tools=tools,
    )
    application = build_application(settings, assistant, email_service)

    try:
        # Long Polling; beendet sich sauber bei SIGINT/SIGTERM (z. B. docker stop).
        # CALLBACK_QUERY wird für die Schaltflächen der E-Mail-Freigabe benötigt.
        application.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    except InvalidToken:
        logger.critical("Telegram hat den Bot-Token abgelehnt. TELEGRAM_BOT_TOKEN prüfen.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
