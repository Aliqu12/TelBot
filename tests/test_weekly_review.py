"""Tests des Wochenrückblicks: Termine, Aufteilung der Aufgaben, Speicher und Versand."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from assistant_bot.prompts import WEEKLY_REVIEW_HISTORY_MARKER
from assistant_bot.tasks.models import Task, TaskConnectionError, TaskDue
from assistant_bot.tasks.service import CompletedListing
from assistant_bot.tasks.weekly_review import (
    MAX_LISTED_TASKS,
    WeeklyReviewService,
    WeeklyReviewStore,
    latest_slot,
    split_open_tasks,
)

BERLIN = ZoneInfo("Europe/Berlin")
FRIDAY = 4
AT = time(13, 0)
SLOT = datetime(2026, 11, 6, 13, 0, tzinfo=BERLIN)  # Freitag
PREVIOUS = datetime(2026, 10, 30, 13, 0, tzinfo=BERLIN)
NEXT = datetime(2026, 11, 13, 13, 0, tzinfo=BERLIN)


def timed(task_id: str, due: datetime, **kwargs: object) -> Task:
    return Task(task_id=task_id, content=f"Aufgabe {task_id}", due=TaskDue(date=due.date(), time=due), **kwargs)


def all_day(task_id: str, day: date, **kwargs: object) -> Task:
    return Task(task_id=task_id, content=f"Aufgabe {task_id}", due=TaskDue(date=day), **kwargs)


def done(task_id: str, completed_at: datetime, project_id: str | None = None) -> Task:
    return Task(task_id=task_id, content=f"Erledigt {task_id}", completed_at=completed_at, project_id=project_id)


# --- Termine --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 6, 13, 0, tzinfo=BERLIN), SLOT),  # genau zum Termin
        (datetime(2026, 11, 6, 18, 30, tzinfo=BERLIN), SLOT),
        (datetime(2026, 11, 7, 9, 0, tzinfo=BERLIN), SLOT),  # Samstag
        (datetime(2026, 11, 9, 8, 0, tzinfo=BERLIN), SLOT),  # Montag danach
        (datetime(2026, 11, 6, 12, 59, tzinfo=BERLIN), PREVIOUS),  # noch vor dem Termin
        (datetime(2026, 11, 2, 8, 0, tzinfo=BERLIN), PREVIOUS),
    ],
)
def test_latest_slot_is_the_most_recent_friday_13_00(now: datetime, expected: datetime) -> None:
    assert latest_slot(now, FRIDAY, AT, BERLIN) == expected


def test_latest_slot_uses_the_given_weekday_and_converts_to_the_timezone() -> None:
    monday_9 = latest_slot(datetime(2026, 11, 6, 13, 0, tzinfo=BERLIN), 0, time(9, 0), BERLIN)
    assert monday_9 == datetime(2026, 11, 2, 9, 0, tzinfo=BERLIN)
    utc_now = datetime(2026, 11, 6, 12, 0, tzinfo=ZoneInfo("UTC"))  # = 13:00 Berlin
    assert latest_slot(utc_now, FRIDAY, AT, BERLIN) == SLOT


def test_slots_stay_at_13_00_wall_clock_across_the_time_change() -> None:
    # Sommerzeit beginnt am 28.3.2027: Die Woche davor hat 167 statt 168 Stunden.
    slot = latest_slot(datetime(2027, 4, 2, 14, 0, tzinfo=BERLIN), FRIDAY, AT, BERLIN)

    assert slot == datetime(2027, 4, 2, 13, 0, tzinfo=BERLIN)
    previous = latest_slot(slot - timedelta(minutes=1), FRIDAY, AT, BERLIN)
    assert previous == datetime(2027, 3, 26, 13, 0, tzinfo=BERLIN)
    assert slot.astimezone(UTC) - previous.astimezone(UTC) == timedelta(hours=167)


# --- Aufgaben aufteilen ---------------------------------------------------------


def split(tasks: list[Task]) -> tuple[list[str], list[str]]:
    upcoming, overdue = split_open_tasks(tasks, SLOT, NEXT, BERLIN)
    return [t.task_id for t in upcoming], [t.task_id for t in overdue]


def test_timed_tasks_are_split_at_the_slot_boundaries() -> None:
    tasks = [
        timed("early", SLOT - timedelta(minutes=1)),
        timed("at-slot", SLOT),
        timed("late", NEXT - timedelta(minutes=1)),
        timed("at-next", NEXT),
    ]

    assert split(tasks) == (["at-slot", "late"], ["early"])


def test_date_only_tasks_count_by_day_so_today_is_upcoming_and_the_next_slot_day_is_not() -> None:
    tasks = [
        all_day("yesterday", date(2026, 11, 5)),
        all_day("today", date(2026, 11, 6)),  # Freitag, noch nicht überfällig
        all_day("thursday", date(2026, 11, 12)),
        all_day("next-friday", date(2026, 11, 13)),  # gehört zum nächsten Rückblick
    ]

    assert split(tasks) == (["today", "thursday"], ["yesterday"])


def test_tasks_without_due_are_ignored_and_results_are_sorted_by_time() -> None:
    tasks = [
        Task(task_id="none", content="ohne"),
        timed("b", SLOT + timedelta(days=2)),
        all_day("a", date(2026, 11, 7)),
        timed("c", SLOT + timedelta(hours=1)),
    ]

    assert split(tasks) == (["c", "a", "b"], [])


# --- Speicher -------------------------------------------------------------------


def test_store_claims_each_slot_once_across_instances_and_can_release(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = WeeklyReviewStore(path)
    store.initialize()
    key = SLOT.isoformat()

    assert asyncio.run(store.claim(key)) is True
    assert asyncio.run(WeeklyReviewStore(path).claim(key)) is False
    asyncio.run(store.release(key))
    assert asyncio.run(store.claim(key)) is True
    assert asyncio.run(store.claim(NEXT.isoformat())) is True


# --- Service --------------------------------------------------------------------


class FakeTasks:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self.timezone = BERLIN
        self.open_tasks: list[Task] = []
        self.completed = CompletedListing(tasks=[], total=0, project_names={})
        self.error: Exception | None = None
        self.completed_calls: list[tuple[datetime, datetime, int]] = []

    def now(self) -> datetime:
        return self._now

    async def list_completed_tasks(self, *, since: datetime, until: datetime, limit: int, **_: object) -> CompletedListing:
        self.completed_calls.append((since, until, limit))
        if self.error is not None:
            raise self.error
        return self.completed

    async def list_open_tasks(self) -> list[Task]:
        return self.open_tasks


class Harness:
    def __init__(self, tmp_path: Path, now: datetime = SLOT + timedelta(minutes=5)) -> None:
        self.tasks = FakeTasks(now)
        self.assistant = AsyncMock()
        self.assistant.write_notice.side_effect = lambda instruction, fallback, **_: f"TEXT[{fallback}]"
        self.sent: list[str] = []
        self.send_error: Exception | None = None
        self.store = WeeklyReviewStore(tmp_path / "db.sqlite")
        self.store.initialize()
        self.service = WeeklyReviewService(
            tasks=self.tasks,  # type: ignore[arg-type]
            assistant=self.assistant,
            store=self.store,
            chat_id=42,
            send=self._send,
            owner_name="Ali",
            weekday=FRIDAY,
            send_time=AT,
            initial_delay=0,
        )

    async def _send(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)

    @property
    def instruction(self) -> str:
        return self.assistant.write_notice.call_args.args[0]


def test_review_is_sent_once_with_the_right_periods_and_recorded_in_the_history(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.tasks.completed = CompletedListing(
        tasks=[done("1", PREVIOUS + timedelta(days=1), project_id="p1"), done("2", SLOT - timedelta(hours=2))],
        total=2,
        project_names={"p1": "Haushalt"},
    )
    harness.tasks.open_tasks = [
        timed("next", SLOT + timedelta(days=2, hours=1)),
        all_day("old", date(2026, 11, 3)),
        timed("far", NEXT + timedelta(days=1)),
    ]

    assert asyncio.run(harness.service.check_once()) is True
    assert asyncio.run(harness.service.check_once()) is False  # nichts doppelt

    assert len(harness.sent) == 1 and harness.sent[0].startswith("TEXT[")
    assert harness.tasks.completed_calls == [(PREVIOUS, SLOT, 100)]
    text = harness.instruction
    assert "Freitag, 30.10. 13:00 Uhr" in text and "Freitag, 06.11. 13:00 Uhr" in text
    assert "Freitag, 13.11. 13:00 Uhr" in text
    assert "Erledigt 1 (Haushalt) – erledigt Sa 31.10." in text and "Erledigt 2" in text
    assert "Aufgabe next – So 08.11. um 14:00 Uhr" in text
    assert "Aufgabe far" not in text
    assert "überfällig (1 Aufgabe(n))" in text and "Aufgabe old – Di 03.11. (ganztägig)" in text
    assert "Ali" in text
    kwargs = harness.assistant.write_notice.call_args.kwargs
    assert kwargs["chat_id"] == 42 and kwargs["max_chars"] > 800
    harness.assistant.record_notice.assert_awaited_once_with(42, WEEKLY_REVIEW_HISTORY_MARKER, harness.sent[0])


def test_empty_week_is_still_reported_without_overdue_block(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    assert asyncio.run(harness.service.check_once()) is True

    text = harness.instruction
    assert "(0 Aufgabe(n))" in text and "- (keine)" in text
    assert "überfällig" not in text
    assert "Wochenrückblick: 0 Aufgaben erledigt, 0 Aufgaben in der kommenden Woche." in harness.sent[0]


def test_long_lists_are_shortened_with_the_real_total(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.tasks.completed = CompletedListing(
        tasks=[done(str(i), SLOT - timedelta(hours=1)) for i in range(MAX_LISTED_TASKS + 5)],
        total=MAX_LISTED_TASKS + 30,
        project_names={},
    )

    asyncio.run(harness.service.check_once())

    text = harness.instruction
    assert f"({MAX_LISTED_TASKS + 30} Aufgabe(n))" in text
    assert "- … und 30 weitere" in text
    assert f"Erledigt {MAX_LISTED_TASKS - 1}" in text and f"Erledigt {MAX_LISTED_TASKS}\n" not in text


def test_fallback_is_neutral_and_lists_upcoming_and_overdue(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.tasks.open_tasks = [timed(str(i), SLOT + timedelta(hours=i + 1)) for i in range(7)] + [
        all_day("late", date(2026, 11, 1))
    ]
    harness.tasks.completed = CompletedListing(tasks=[done("1", SLOT)], total=1, project_names={})

    asyncio.run(harness.service.check_once())

    fallback = harness.sent[0].removeprefix("TEXT[").removesuffix("]")
    assert fallback.startswith("Wochenrückblick: 1 Aufgabe erledigt, 7 Aufgaben in der kommenden Woche.")
    assert "Aufgabe 0; Aufgabe 1; Aufgabe 2; Aufgabe 3; Aufgabe 4; … und 2 weitere" in fallback
    assert fallback.endswith("Überfällig: 1 Aufgabe.")


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(minutes=-1), False),  # vor dem Termin: der vorige Rückblick ist schon zu alt
        (timedelta(hours=11), True),  # Bot war kurz offline: wird nachgeholt
        (timedelta(hours=12, minutes=1), False),  # zu spät
    ],
)
def test_review_is_only_sent_inside_the_catch_up_window(tmp_path: Path, offset: timedelta, expected: bool) -> None:
    harness = Harness(tmp_path, SLOT + offset)

    assert asyncio.run(harness.service.check_once()) is expected
    assert bool(harness.sent) is expected


def test_failed_send_is_retried_at_the_next_check(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.send_error = RuntimeError("Telegram down")

    with pytest.raises(RuntimeError):
        asyncio.run(harness.service.check_once())
    harness.assistant.record_notice.assert_not_awaited()

    harness.send_error = None
    assert asyncio.run(harness.service.check_once()) is True
    assert len(harness.sent) == 1


def test_todoist_error_releases_the_slot_and_is_retried(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.tasks.error = TaskConnectionError("weg")

    with pytest.raises(TaskConnectionError):
        asyncio.run(harness.service.check_once())
    assert harness.sent == []

    harness.tasks.error = None
    assert asyncio.run(harness.service.check_once()) is True


def test_history_failure_does_not_undo_the_sent_review(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.assistant.record_notice.side_effect = OSError("disk")

    assert asyncio.run(harness.service.check_once()) is True
    assert asyncio.run(harness.service.check_once()) is False
    assert len(harness.sent) == 1
