"""Einstiegspunkt: ``python -m assistant_bot``."""

from __future__ import annotations

import logging
import sqlite3
import sys
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.error import InvalidToken

from .assistant import Assistant
from .config import ConfigError, load_personality, load_settings
from .conversation import ConversationService
from .llm import create_chat_model, create_image_model
from .logging_config import configure_logging
from .mail.factory import create_email_provider
from .mail.service import EmailService
from .mail.tools import build_email_tools
from .prompts import build_system_prompt
from .status_limiter import StatusRateLimiter
from .tasks.factory import create_task_provider
from .tasks.service import TaskService
from .tasks.tools import build_task_tools
from .telegram_bot.application import build_application
from .tools import Tool, ToolRegistry
from .web.factory import create_web_provider
from .web.service import WebService
from .web.tools import build_web_tools

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
    try:
        personality = load_personality(settings.personality_file)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    configure_logging(settings.log_level, secrets=settings.secrets)
    logger.info(
        "Starte Assistent (Anbieter=%s, Modell=%s, Bildmodell=%s, Kontext=%d Nachrichten, Datenbank=%s, "
        "Persönlichkeit=%s)",
        settings.llm_provider,
        settings.llm_model,
        settings.llm_image_model or "wie Textmodell",
        settings.history_max_messages,
        settings.database_path,
        settings.personality_file,
    )

    conversations = ConversationService(settings.database_path, settings.history_max_messages)
    try:
        conversations.initialize()
    except (OSError, sqlite3.Error) as exc:
        logger.critical("Datenbank %s konnte nicht initialisiert werden: %s", settings.database_path, exc)
        return 1

    timezone = ZoneInfo(settings.timezone)
    email_service: EmailService | None = None
    task_service: TaskService | None = None
    web_service: WebService | None = None
    tools: list[Tool] = []
    if settings.email is not None:
        email_service = EmailService(
            create_email_provider(settings.email),
            allowed_recipients=settings.email.allowed_recipients,
            draft_ttl_seconds=settings.email.draft_ttl_seconds,
            max_body_chars=settings.email.max_body_chars,
        )
        tools += build_email_tools(email_service)
        logger.info(
            "E-Mail-Integration aktiv (Provider=%s, Empfänger-Freigabeliste=%s).",
            email_service.provider_name,
            "ja" if settings.email.allowed_recipients else "nein",
        )
    if settings.tasks is not None:
        task_service = TaskService(create_task_provider(settings.tasks, timezone), timezone=timezone)
        tools += build_task_tools(task_service)
        logger.info(
            "Aufgaben-Integration aktiv (Provider=%s, Zeitzone=%s).", task_service.provider_name, settings.timezone
        )
    if settings.web is not None:
        web_service = WebService(
            create_web_provider(settings.web),
            max_results=settings.web.max_results,
            max_chars=settings.web.max_chars,
            max_calls_per_day=settings.web.max_calls_per_day,
            timezone=timezone,
        )
        tools += build_web_tools(web_service)
        logger.info(
            "Websuche aktiv (Provider=%s, Tageslimit=%s).",
            web_service.provider_name,
            settings.web.max_calls_per_day or "unbegrenzt",
        )

    chat_model = create_chat_model(settings)
    image_model = create_image_model(settings, chat_model)
    assistant = Assistant(
        chat_model=chat_model,
        conversations=conversations,
        system_prompt=build_system_prompt(
            personality,
            settings.owner_name,
            email_enabled=email_service is not None,
            tasks_enabled=task_service is not None,
            images_enabled=image_model is not None,
            web_enabled=web_service is not None,
        ),
        tools=ToolRegistry(tools) if tools else None,
        timezone=timezone,
        image_model=image_model,
        image_history_messages=settings.image_history_max_messages,
    )
    application = build_application(
        settings,
        assistant,
        email_service,
        task_service,
        StatusRateLimiter(settings.database_path, settings.status_llm_max_per_minute),
        web_service=web_service,
    )

    try:
        # Long Polling; beendet sich sauber bei SIGINT/SIGTERM (z. B. docker stop).
        # CALLBACK_QUERY wird für die Schaltflächen (E-Mail-Freigabe, Aufgaben löschen) benötigt.
        application.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    except InvalidToken:
        logger.critical("Telegram hat den Bot-Token abgelehnt. TELEGRAM_BOT_TOKEN prüfen.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
