"""Erinnerungen an fällige Aufgaben (z. B. 24 h und 2 h vorher), jede Stufe nur einmal.

- ``plan_reminders`` ist reine Logik ohne Netzwerk und Datenbank: Sie entscheidet anhand der Aufgaben, der
  Uhrzeit und der bereits gesendeten Erinnerungen, was jetzt gesendet werden muss.
- ``ReminderStore`` merkt sich gesendete Erinnerungen in SQLite (überlebt Neustarts). Der Schlüssel besteht aus
  Aufgabe, Fälligkeit und Stufe: Wird ein Termin verschoben oder wiederholt sich eine Aufgabe, gilt das als neuer
  Termin und wird neu erinnert.
- ``ReminderService`` fragt die Aufgaben ab, formuliert die Nachricht in der Persönlichkeit des Assistenten
  und sendet sie. Eingetragen wird vor dem Senden; scheitert das Senden, wird der Eintrag zurückgenommen.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path

from ..assistant import Assistant
from ..prompts import REMINDER_HISTORY_MARKER, REMINDER_INSTRUCTION, REMINDER_URGENT_INSTRUCTION
from .models import Task, TaskRateLimitError
from .service import TaskService

logger = logging.getLogger(__name__)

STAGE_EARLY = "early"
STAGE_URGENT = "urgent"
MAX_LISTED_TASKS = 10
MAX_TITLE_CHARS = 120
RETENTION_DAYS = 30
INITIAL_DELAY_SECONDS = 30.0  # Beim Start zuerst die Begrüßung durchlassen
REMINDER_TIMEOUT_SECONDS = 20.0
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_reminders (
    task_id TEXT NOT NULL,
    due_at  TEXT NOT NULL,
    stage   TEXT NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (task_id, due_at, stage)
)
"""

Key = tuple[str, str, str]  # (task_id, Fälligkeit als ISO-Text, Stufe)


@dataclass(frozen=True, slots=True)
class Reminder:
    task: Task
    due_at: datetime
    stage: str

    @property
    def key(self) -> Key:
        return (self.task.task_id, self.due_at.isoformat(), self.stage)


def effective_due(task: Task, timezone: tzinfo, default_time: dtime) -> datetime | None:
    """Fälligkeitszeitpunkt der Aufgabe; Aufgaben nur mit Datum gelten ab ``default_time``."""
    if task.due is None:
        return None
    if task.due.time is not None:
        return task.due.time.astimezone(timezone)
    return datetime.combine(task.due.date, default_time.replace(tzinfo=None), tzinfo=timezone)


def plan_reminders(
    tasks: Iterable[Task],
    now: datetime,
    *,
    timezone: tzinfo,
    default_time: dtime,
    early: timedelta,
    urgent: timedelta,
    sent: set[Key],
) -> list[Reminder]:
    """Bestimmt, welche Erinnerungen jetzt gesendet werden müssen.

    - Liegt der Termin innerhalb von ``urgent``, kommt (nur) die dringliche Erinnerung; eine verpasste
      frühe Erinnerung wird nicht nachgeholt.
    - Liegt er zwischen ``urgent`` und ``early``, kommt die frühe Erinnerung, auch verspätet (z. B. nach einer
      Downtime). Wurde die Aufgabe erst nach dem Zeitpunkt der frühen Erinnerung angelegt, entfällt sie.
    - Termine in der Vergangenheit werden nie erinnert.
    """
    result: list[Reminder] = []
    for task in tasks:
        due_at = effective_due(task, timezone, default_time)
        if due_at is None or due_at <= now:
            continue
        remaining = due_at - now
        if remaining <= urgent:
            stage = STAGE_URGENT
        elif remaining <= early:
            stage = STAGE_EARLY
            if task.created_at is not None and task.created_at > due_at - early:
                continue
        else:
            continue
        reminder = Reminder(task=task, due_at=due_at, stage=stage)
        if reminder.key not in sent:
            result.append(reminder)
    result.sort(key=lambda r: (r.due_at, r.task.task_id))
    return result


class ReminderStore:
    """Gesendete Erinnerungen in SQLite (je Operation eine kurze Verbindung, per Thread ausgelagert)."""

    def __init__(self, database_path: Path, clock: Callable[[], float] = time.time) -> None:
        self._database_path = database_path
        self._clock = clock

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    async def sent_keys(self) -> set[Key]:
        return await asyncio.to_thread(self._sent_keys)

    async def claim(self, keys: Iterable[Key]) -> None:
        await asyncio.to_thread(self._claim, list(keys))

    async def release(self, keys: Iterable[Key]) -> None:
        await asyncio.to_thread(self._release, list(keys))

    async def cleanup(self) -> int:
        return await asyncio.to_thread(self._cleanup)

    def _sent_keys(self) -> set[Key]:
        with self._connect() as connection:
            return {(a, b, c) for a, b, c in connection.execute("SELECT task_id, due_at, stage FROM task_reminders")}

    def _claim(self, keys: list[Key]) -> None:
        now = self._clock()
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO task_reminders (task_id, due_at, stage, sent_at) VALUES (?, ?, ?, ?)",
                [(*key, now) for key in keys],
            )

    def _release(self, keys: list[Key]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM task_reminders WHERE task_id = ? AND due_at = ? AND stage = ?", keys
            )

    def _cleanup(self) -> int:
        cutoff = self._clock() - RETENTION_DAYS * 86400
        with self._connect() as connection:
            return connection.execute("DELETE FROM task_reminders WHERE sent_at < ?", (cutoff,)).rowcount

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        try:
            with connection:  # commit bei Erfolg, rollback bei Exception
                connection.execute(_SCHEMA)
                yield connection
        finally:
            connection.close()


class ReminderService:
    def __init__(
        self,
        *,
        tasks: TaskService,
        assistant: Assistant,
        store: ReminderStore,
        chat_id: int,
        send: Callable[[str], Awaitable[None]],
        owner_name: str,
        poll_seconds: float,
        default_time: dtime,
        early: timedelta,
        urgent: timedelta,
        initial_delay: float = INITIAL_DELAY_SECONDS,
    ) -> None:
        self._tasks = tasks
        self._assistant = assistant
        self._store = store
        self._chat_id = chat_id
        self._send = send
        self._owner_name = owner_name
        self._poll_seconds = poll_seconds
        self._default_time = default_time
        self._early = early
        self._urgent = urgent
        self._initial_delay = initial_delay

    async def run(self) -> None:
        """Endlosschleife; Fehler beenden sie nie, nur Abbruch (``CancelledError``) tut das."""
        await asyncio.sleep(self._initial_delay)
        logger.info("Erinnerungen aktiv (Abfrage alle %d s).", self._poll_seconds)
        while True:
            delay = self._poll_seconds
            try:
                await self.check_once()
            except TaskRateLimitError as exc:
                delay = max(delay, exc.retry_after or 0)
                logger.warning("Erinnerungen: Todoist-Limit erreicht, nächster Versuch in %s s.", delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Hintergrundschleife darf nicht sterben
                logger.warning("Erinnerungsdurchlauf fehlgeschlagen (%s), nächster Versuch folgt.", type(exc).__name__)
            await asyncio.sleep(delay)

    async def check_once(self) -> int:
        """Ein Durchlauf; gibt die Anzahl gesendeter Nachrichten zurück."""
        tasks = await self._tasks.list_open_tasks()
        now = self._tasks.now()
        due = plan_reminders(
            tasks,
            now,
            timezone=self._tasks.timezone,
            default_time=self._default_time,
            early=self._early,
            urgent=self._urgent,
            sent=await self._store.sent_keys(),
        )
        messages = 0
        for stage in (STAGE_EARLY, STAGE_URGENT):
            group = [reminder for reminder in due if reminder.stage == stage]
            if group:
                await self._deliver(stage, group)
                messages += 1
        await self._store.cleanup()
        return messages

    async def _deliver(self, stage: str, group: list[Reminder]) -> None:
        keys = [reminder.key for reminder in group]
        await self._store.claim(keys)
        try:
            instruction = REMINDER_URGENT_INSTRUCTION if stage == STAGE_URGENT else REMINDER_INSTRUCTION
            text = await self._assistant.write_notice(
                instruction.format(owner_name=self._owner_name, tasks=_describe(group)),
                _fallback(group, stage),
                timeout=REMINDER_TIMEOUT_SECONDS,
            )
            await self._send(text)
        except BaseException:
            # Nicht gesendet (Telegram-Fehler, Abbruch): beim nächsten Durchlauf erneut versuchen.
            await self._store.release(keys)
            raise
        logger.info("Erinnerung gesendet (Stufe=%s, %d Aufgabe(n)).", stage, len(group))
        try:
            await self._assistant.record_notice(self._chat_id, REMINDER_HISTORY_MARKER, text)
        except Exception as exc:  # noqa: BLE001 - gesendet ist gesendet, der Verlauf ist nur Komfort
            logger.warning("Erinnerung nicht im Verlauf gespeichert (%s).", type(exc).__name__)


def _title(task: Task) -> str:
    title = " ".join(task.content.split())
    return title if len(title) <= MAX_TITLE_CHARS else title[: MAX_TITLE_CHARS - 1] + "…"


def _describe(group: list[Reminder]) -> str:
    lines = []
    for reminder in group[:MAX_LISTED_TASKS]:
        due_at = reminder.due_at
        day = f"{_WEEKDAYS[due_at.weekday()]}, {due_at.strftime('%d.%m.')}"
        has_time = reminder.task.due is not None and reminder.task.due.time is not None
        when = f"{day} um {due_at.strftime('%H:%M')} Uhr" if has_time else f"{day} (ganztägig, ohne Uhrzeit)"
        lines.append(f"- {_title(reminder.task)} – fällig {when}")
    if len(group) > MAX_LISTED_TASKS:
        lines.append(f"- … und {len(group) - MAX_LISTED_TASKS} weitere")
    return "\n".join(lines)


def _fallback(group: list[Reminder], stage: str) -> str:
    titles = ", ".join(f"„{_title(r.task)}“" for r in group[:3])
    if len(group) > 3:
        titles += f" und {len(group) - 3} weitere"
    if stage == STAGE_URGENT:
        return f"Gleich ist es so weit: {titles} {'ist' if len(group) == 1 else 'sind'} bald fällig."
    return f"Zur Erinnerung: {titles} {'ist' if len(group) == 1 else 'sind'} bald fällig."
