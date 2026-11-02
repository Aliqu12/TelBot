"""Wöchentlicher Rückblick: erledigte Aufgaben der Woche und Ausblick auf die nächste Woche.

- Der Rückblick kommt einmal pro Woche zum konfigurierten Zeitpunkt (Standard: Freitag 13:00 Uhr, in der
  Zeitzone der Aufgaben-Integration). Der Zeitraum geht vom vorigen bis zum aktuellen Termin, der Ausblick vom
  aktuellen bis zum nächsten Termin. Die Grenzen sind feste Wanduhrzeiten, auch über Zeitumstellungen hinweg.
- ``latest_slot``, ``split_open_tasks`` und die Textbausteine sind reine Logik ohne Netzwerk und Datenbank.
- ``WeeklyReviewStore`` merkt sich den gesendeten Termin in SQLite (überlebt Neustarts, verhindert Doppelsendungen).
- ``WeeklyReviewService`` fragt Todoist ab, lässt das Sprachmodell den Text in der Persönlichkeit schreiben und sendet
  ihn. Der Termin wird vor dem Abfragen eingetragen; scheitert etwas, wird er zurückgenommen und später erneut
  versucht, aber nur innerhalb des Nachholfensters (war der Bot kurz offline, kommt der Rückblick verspätet).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path

from ..assistant import Assistant
from ..prompts import WEEKLY_REVIEW_HISTORY_MARKER, WEEKLY_REVIEW_INSTRUCTION, WEEKLY_REVIEW_OVERDUE_BLOCK
from .models import Task, TaskRateLimitError
from .service import MAX_COMPLETED_RESULTS, TaskService

logger = logging.getLogger(__name__)

POLL_SECONDS = 300.0
INITIAL_DELAY_SECONDS = 60.0  # Beim Start zuerst Begrüßung und Erinnerungen durchlassen
CATCH_UP = timedelta(hours=12)  # So lange nach dem Termin wird ein verpasster Rückblick noch nachgeholt
REVIEW_TIMEOUT_SECONDS = 45.0
MAX_REVIEW_CHARS = 3500  # Telegram erlaubt 4096 Zeichen je Nachricht
MAX_LISTED_TASKS = 40
MAX_TITLE_CHARS = 120
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_SHORT_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_reviews (
    slot    TEXT PRIMARY KEY,
    sent_at REAL NOT NULL
)
"""


def slot_at(day: date, at: dtime, timezone: tzinfo) -> datetime:
    """Zeitpunkt ``at`` (Wanduhrzeit) am Tag ``day`` in der Zeitzone."""
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=timezone)


def latest_slot(now: datetime, weekday: int, at: dtime, timezone: tzinfo) -> datetime:
    """Jüngster Termin (Wochentag ``weekday``, Uhrzeit ``at``), der nicht nach ``now`` liegt."""
    now = now.astimezone(timezone)
    day = now.date() - timedelta(days=(now.weekday() - weekday) % 7)
    slot = slot_at(day, at, timezone)
    if slot > now:
        slot = slot_at(day - timedelta(days=7), at, timezone)
    return slot


def split_open_tasks(
    tasks: list[Task], slot: datetime, next_slot: datetime, timezone: tzinfo
) -> tuple[list[Task], list[Task]]:
    """Teilt offene Aufgaben in (anstehend in [slot, next_slot), überfällig vor slot).

    Aufgaben mit Uhrzeit zählen nach dem Zeitpunkt, Aufgaben nur mit Datum nach dem Tag: Was am Tag des nächsten
    Termins fällig ist, gehört zum nächsten Rückblick (dort ist es "heute" und damit anstehend).
    """
    upcoming: list[tuple[datetime, Task]] = []
    overdue: list[tuple[datetime, Task]] = []
    for task in tasks:
        if task.due is None:
            continue
        if task.due.time is not None:
            moment = task.due.time.astimezone(timezone)
            is_overdue, is_upcoming = moment < slot, slot <= moment < next_slot
        else:
            moment = datetime.combine(task.due.date, dtime.min, tzinfo=timezone)
            is_overdue = task.due.date < slot.date()
            is_upcoming = slot.date() <= task.due.date < next_slot.date()
        if is_overdue:
            overdue.append((moment, task))
        elif is_upcoming:
            upcoming.append((moment, task))
    return [t for _, t in sorted(upcoming, key=_by_moment)], [t for _, t in sorted(overdue, key=_by_moment)]


def _by_moment(item: tuple[datetime, Task]) -> tuple[datetime, str]:
    return item[0], item[1].task_id


class WeeklyReviewStore:
    """Gesendete Rückblicke in SQLite (je Operation eine kurze Verbindung, per Thread ausgelagert)."""

    def __init__(self, database_path: Path, clock: Callable[[], float] = time.time) -> None:
        self._database_path = database_path
        self._clock = clock

    def initialize(self) -> None:
        with self._connect():
            pass

    async def claim(self, slot: str) -> bool:
        """Trägt den Termin ein; ``False``, wenn er schon eingetragen war."""
        return await asyncio.to_thread(self._claim, slot)

    async def release(self, slot: str) -> None:
        await asyncio.to_thread(self._release, slot)

    def _claim(self, slot: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO weekly_reviews (slot, sent_at) VALUES (?, ?)", (slot, self._clock())
            )
            return cursor.rowcount == 1

    def _release(self, slot: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM weekly_reviews WHERE slot = ?", (slot,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        try:
            with connection:  # commit bei Erfolg, rollback bei Exception
                connection.execute(_SCHEMA)
                yield connection
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class ReviewData:
    since: datetime
    until: datetime
    next_until: datetime
    done: list[Task]
    done_total: int
    upcoming: list[Task]
    overdue: list[Task]
    project_names: dict[str, str]


class WeeklyReviewService:
    def __init__(
        self,
        *,
        tasks: TaskService,
        assistant: Assistant,
        store: WeeklyReviewStore,
        chat_id: int,
        send: Callable[[str], Awaitable[None]],
        owner_name: str,
        weekday: int,
        send_time: dtime,
        poll_seconds: float = POLL_SECONDS,
        initial_delay: float = INITIAL_DELAY_SECONDS,
        catch_up: timedelta = CATCH_UP,
    ) -> None:
        self._tasks = tasks
        self._assistant = assistant
        self._store = store
        self._chat_id = chat_id
        self._send = send
        self._owner_name = owner_name
        self._weekday = weekday
        self._send_time = send_time
        self._poll_seconds = poll_seconds
        self._initial_delay = initial_delay
        self._catch_up = catch_up

    async def run(self) -> None:
        """Endlosschleife; Fehler beenden sie nie, nur Abbruch (``CancelledError``) tut das."""
        await asyncio.sleep(self._initial_delay)
        logger.info(
            "Wochenrückblick aktiv (%s, %s Uhr).", _WEEKDAYS[self._weekday], self._send_time.strftime("%H:%M")
        )
        while True:
            delay = self._poll_seconds
            try:
                await self.check_once()
            except TaskRateLimitError as exc:
                delay = max(delay, exc.retry_after or 0)
                logger.warning("Wochenrückblick: Todoist-Limit erreicht, nächster Versuch in %s s.", delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Hintergrundschleife darf nicht sterben
                logger.warning("Wochenrückblick fehlgeschlagen (%s), nächster Versuch folgt.", type(exc).__name__)
            await asyncio.sleep(delay)

    async def check_once(self) -> bool:
        """Sendet den Rückblick, falls er fällig und noch nicht gesendet ist; ``True`` = gesendet."""
        timezone = self._tasks.timezone
        now = self._tasks.now()
        slot = latest_slot(now, self._weekday, self._send_time, timezone)
        if now - slot > self._catch_up:
            return False
        key = slot.isoformat()
        if not await self._store.claim(key):
            return False
        try:
            text = await self._compose(slot)
            await self._send(text)
        except BaseException:
            # Nicht gesendet (Todoist-/Telegram-Fehler, Abbruch): beim nächsten Durchlauf erneut versuchen.
            await self._store.release(key)
            raise
        logger.info("Wochenrückblick gesendet.")
        try:
            await self._assistant.record_notice(self._chat_id, WEEKLY_REVIEW_HISTORY_MARKER, text)
        except Exception as exc:  # noqa: BLE001 - gesendet ist gesendet, der Verlauf ist nur Komfort
            logger.warning("Wochenrückblick nicht im Verlauf gespeichert (%s).", type(exc).__name__)
        return True

    async def _compose(self, slot: datetime) -> str:
        data = await self._collect(slot)
        return await self._assistant.write_notice(
            build_instruction(data, self._owner_name),
            build_fallback(data),
            timeout=REVIEW_TIMEOUT_SECONDS,
            chat_id=self._chat_id,
            max_chars=MAX_REVIEW_CHARS,
        )

    async def _collect(self, slot: datetime) -> ReviewData:
        timezone = self._tasks.timezone
        since = slot_at(slot.date() - timedelta(days=7), self._send_time, timezone)
        next_slot = slot_at(slot.date() + timedelta(days=7), self._send_time, timezone)
        completed = await self._tasks.list_completed_tasks(since=since, until=slot, limit=MAX_COMPLETED_RESULTS)
        open_tasks = await self._tasks.list_open_tasks()
        upcoming, overdue = split_open_tasks(open_tasks, slot, next_slot, timezone)
        return ReviewData(
            since=since,
            until=slot,
            next_until=next_slot,
            done=completed.tasks,
            done_total=completed.total,
            upcoming=upcoming,
            overdue=overdue,
            project_names=completed.project_names,
        )


def build_instruction(data: ReviewData, owner_name: str) -> str:
    overdue_block = ""
    if data.overdue:
        overdue_block = WEEKLY_REVIEW_OVERDUE_BLOCK.format(
            overdue_count=len(data.overdue), overdue=_lines(data.overdue, _describe_open)
        )
    return WEEKLY_REVIEW_INSTRUCTION.format(
        owner_name=owner_name,
        since=_stamp(data.since),
        until=_stamp(data.until),
        next_until=_stamp(data.next_until),
        done_count=data.done_total,
        done=_lines(data.done, lambda t: _describe_done(t, data.project_names), total=data.done_total),
        upcoming_count=len(data.upcoming),
        upcoming=_lines(data.upcoming, _describe_open),
        overdue_block=overdue_block,
    )


def build_fallback(data: ReviewData) -> str:
    """Fester, neutraler Ersatztext für den Fall, dass das Sprachmodell nicht rechtzeitig antwortet."""
    parts = [
        f"Wochenrückblick: {data.done_total} {_noun(data.done_total)} erledigt, "
        f"{len(data.upcoming)} {_noun(len(data.upcoming))} in der kommenden Woche."
    ]
    if data.upcoming:
        titles = "; ".join(_title(t) for t in data.upcoming[:5])
        if len(data.upcoming) > 5:
            titles += f"; … und {len(data.upcoming) - 5} weitere"
        parts.append(f"Anstehend: {titles}.")
    if data.overdue:
        parts.append(f"Überfällig: {len(data.overdue)} {_noun(len(data.overdue))}.")
    return "\n".join(parts)


def _noun(count: int) -> str:
    return "Aufgabe" if count == 1 else "Aufgaben"


def _title(task: Task) -> str:
    title = " ".join(task.content.split())
    return title if len(title) <= MAX_TITLE_CHARS else title[: MAX_TITLE_CHARS - 1] + "…"


def _stamp(moment: datetime) -> str:
    return f"{_WEEKDAYS[moment.weekday()]}, {moment.strftime('%d.%m.')} {moment.strftime('%H:%M')} Uhr"


def _day(moment: datetime | date) -> str:
    return f"{_SHORT_WEEKDAYS[moment.weekday()]} {moment.strftime('%d.%m.')}"


def _describe_done(task: Task, project_names: dict[str, str]) -> str:
    project = project_names.get(task.project_id or "")
    line = f"- {_title(task)}"
    if project:
        line += f" ({project})"
    if task.completed_at is not None:
        line += f" – erledigt {_day(task.completed_at)}"
    return line


def _describe_open(task: Task) -> str:
    line = f"- {_title(task)}"
    if task.due is None:
        return line
    if task.due.time is not None:
        return f"{line} – {_day(task.due.time)} um {task.due.time.strftime('%H:%M')} Uhr"
    return f"{line} – {_day(task.due.date)} (ganztägig)"


def _lines(tasks: list[Task], describe: Callable[[Task], str], total: int | None = None) -> str:
    if not tasks:
        return "- (keine)"
    lines = [describe(task) for task in tasks[:MAX_LISTED_TASKS]]
    remaining = (total if total is not None else len(tasks)) - len(lines)
    if remaining > 0:
        lines.append(f"- … und {remaining} weitere")
    return "\n".join(lines)
