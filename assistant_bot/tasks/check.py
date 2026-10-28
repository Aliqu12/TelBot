"""Verbindungstest: ``python -m assistant_bot.tasks.check``.

Liest nur (Projekte und offene Aufgaben) und ändert nichts in Todoist.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ..config import ConfigError, Settings, load_settings
from .factory import create_task_provider
from .models import TaskError


async def run_check(settings: Settings) -> int:
    if settings.tasks is None:
        print("Aufgaben-Integration ist deaktiviert (TASK_PROVIDER ist leer).", file=sys.stderr)
        return 2
    timezone = ZoneInfo(settings.timezone)
    provider = create_task_provider(settings.tasks, timezone)
    try:
        projects = await provider.list_projects()
        tasks = await provider.list_tasks()
    except TaskError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    finally:
        await provider.aclose()

    today = datetime.now(timezone).date()
    due_today = sum(1 for t in tasks if t.due is not None and t.due.date == today)
    overdue = sum(1 for t in tasks if t.due is not None and t.due.date < today)
    print(
        f"Verbindung zu Todoist OK (Zeitzone {settings.timezone}). Projekte: {len(projects)}, "
        f"offene Aufgaben: {len(tasks)}, heute fällig: {due_today}, überfällig: {overdue}."
    )
    return 0


def main() -> int:
    load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    return asyncio.run(run_check(settings))


if __name__ == "__main__":
    sys.exit(main())
