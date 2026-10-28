"""Auswahl des Aufgaben-Providers anhand der Konfiguration (TASK_PROVIDER)."""

from __future__ import annotations

from datetime import tzinfo

from ..config import TaskSettings
from .provider import TaskProvider
from .todoist import TodoistTaskProvider


def create_task_provider(settings: TaskSettings, timezone: tzinfo) -> TaskProvider:
    """Erzeugt den konfigurierten Provider.

    Neue Provider (z. B. Microsoft To Do) werden hier ergänzt und in
    ``config.SUPPORTED_TASK_PROVIDERS`` eingetragen – der ``TaskService``
    bleibt unverändert.
    """
    if settings.provider == "todoist":
        return TodoistTaskProvider(
            settings.api_token,
            timezone=timezone,
            timeout_seconds=settings.timeout_seconds,
        )
    raise ValueError(f"Unbekannter Aufgaben-Provider: {settings.provider!r}")
