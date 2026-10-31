"""Tests der Aufgaben-Erinnerungen: Planung, Speicher und Versand."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from assistant_bot.prompts import REMINDER_HISTORY_MARKER
from assistant_bot.tasks.models import Task, TaskDue, TaskRateLimitError
from assistant_bot.tasks.reminders import (
    STAGE_EARLY,
    STAGE_URGENT,
    ReminderService,
    ReminderStore,
    effective_due,
    plan_reminders,
)

BERLIN = ZoneInfo("Europe/Berlin")
NOON = time(12, 0)
EARLY = timedelta(hours=24)
URGENT = timedelta(hours=2)
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=BERLIN)


def timed(task_id: str, due: datetime, **kwargs: object) -> Task:
    return Task(task_id=task_id, content=f"Aufgabe {task_id}", due=TaskDue(date=due.date(), time=due), **kwargs)


def all_day(task_id: str, day: date, **kwargs: object) -> Task:
    return Task(task_id=task_id, content=f"Aufgabe {task_id}", due=TaskDue(date=day), **kwargs)


def plan(tasks: list[Task], now: datetime = NOW, sent: set | None = None):  # noqa: ANN201
    return plan_reminders(
        tasks, now, timezone=BERLIN, default_time=NOON, early=EARLY, urgent=URGENT, sent=sent or set()
    )


def stages(tasks: list[Task], **kwargs: object) -> list[tuple[str, str]]:
    return [(r.task.task_id, r.stage) for r in plan(tasks, **kwargs)]


# --- Fälligkeit -----------------------------------------------------------------


def test_effective_due_uses_default_time_for_date_only_tasks() -> None:
    assert effective_due(all_day("a", date(2026, 11, 4)), BERLIN, NOON) == datetime(2026, 11, 4, 12, 0, tzinfo=BERLIN)


def test_effective_due_converts_to_local_timezone_and_ignores_tasks_without_due() -> None:
    utc = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    assert effective_due(timed("a", utc), BERLIN, NOON) == datetime(2026, 11, 4, 10, 0, tzinfo=BERLIN)
    assert effective_due(Task(task_id="b", content="ohne"), BERLIN, NOON) is None


# --- Planung --------------------------------------------------------------------


def test_nothing_is_due_outside_the_early_window() -> None:
    assert stages([timed("a", NOW + EARLY + timedelta(minutes=1))]) == []


def test_early_reminder_inside_24_hours() -> None:
    assert stages([timed("a", NOW + timedelta(hours=23))]) == [("a", STAGE_EARLY)]
    assert stages([timed("a", NOW + EARLY)]) == [("a", STAGE_EARLY)]


def test_urgent_reminder_inside_2_hours() -> None:
    assert stages([timed("a", NOW + timedelta(hours=1, minutes=59))]) == [("a", STAGE_URGENT)]
    assert stages([timed("a", NOW + URGENT)]) == [("a", STAGE_URGENT)]


def test_missed_early_reminder_is_skipped_inside_the_urgent_window() -> None:
    # Nur die dringliche Erinnerung, nicht zusätzlich die frühe.
    assert stages([timed("a", NOW + timedelta(hours=1))]) == [("a", STAGE_URGENT)]


def test_missed_early_reminder_is_made_up_before_the_urgent_window() -> None:
    # Bot war offline, Termin ist noch 10 h entfernt: frühe Erinnerung kommt verspätet.
    assert stages([timed("a", NOW + timedelta(hours=10))]) == [("a", STAGE_EARLY)]


def test_past_and_current_due_times_are_never_reminded() -> None:
    assert stages([timed("a", NOW), timed("b", NOW - timedelta(hours=1))]) == []


def test_date_only_task_counts_from_12_00() -> None:
    day = NOW.date() + timedelta(days=1)  # 4.11. 12:00 ist 26 h entfernt
    assert stages([all_day("a", day)]) == []
    assert stages([all_day("a", day)], now=datetime(2026, 11, 3, 12, 0, tzinfo=BERLIN)) == [("a", STAGE_EARLY)]
    assert stages([all_day("a", day)], now=datetime(2026, 11, 4, 10, 0, tzinfo=BERLIN)) == [("a", STAGE_URGENT)]
    assert stages([all_day("a", day)], now=datetime(2026, 11, 4, 12, 0, tzinfo=BERLIN)) == []


def test_sent_reminders_are_not_repeated_but_other_stages_and_new_due_times_are() -> None:
    due = NOW + timedelta(hours=1)
    task = timed("a", due)
    key_early = ("a", due.isoformat(), STAGE_EARLY)
    key_urgent = ("a", due.isoformat(), STAGE_URGENT)

    assert stages([task], sent={key_urgent}) == []
    assert stages([task], sent={key_early}) == [("a", STAGE_URGENT)]
    # Termin verschoben (oder Wiederholung): neuer Schlüssel, neue Erinnerung.
    moved = timed("a", due + timedelta(minutes=30))
    assert stages([moved], sent={key_urgent}) == [("a", STAGE_URGENT)]


def test_task_created_after_the_early_trigger_gets_no_early_reminder() -> None:
    due = NOW + timedelta(hours=20)  # frühe Erinnerung wäre vor 4 h gefällig gewesen
    created_recently = NOW - timedelta(minutes=5)
    created_long_ago = NOW - timedelta(days=3)

    assert stages([timed("new", due, created_at=created_recently)]) == []
    assert stages([timed("old", due, created_at=created_long_ago)]) == [("old", STAGE_EARLY)]
    assert stages([timed("unknown", due, created_at=None)]) == [("unknown", STAGE_EARLY)]


def test_task_created_inside_the_urgent_window_still_gets_the_urgent_reminder() -> None:
    task = timed("a", NOW + timedelta(hours=1), created_at=NOW - timedelta(minutes=5))
    assert stages([task]) == [("a", STAGE_URGENT)]


# --- Speicher -------------------------------------------------------------------


def test_store_remembers_claims_across_instances_and_can_release(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = ReminderStore(path)
    store.initialize()
    key = ("a", "2026-11-04T10:00:00+01:00", STAGE_EARLY)
    other = ("b", "2026-11-04T10:00:00+01:00", STAGE_URGENT)

    asyncio.run(store.claim([key, other]))
    asyncio.run(store.claim([key]))  # doppelt: kein Fehler

    assert asyncio.run(ReminderStore(path).sent_keys()) == {key, other}
    asyncio.run(store.release([key]))
    assert asyncio.run(store.sent_keys()) == {other}


def test_store_cleanup_removes_old_entries(tmp_path: Path) -> None:
    now = [1_000_000.0]
    store = ReminderStore(tmp_path / "db.sqlite", clock=lambda: now[0])
    store.initialize()
    asyncio.run(store.claim([("old", "x", STAGE_EARLY)]))
    now[0] += 31 * 86400
    asyncio.run(store.claim([("new", "x", STAGE_EARLY)]))

    assert asyncio.run(store.cleanup()) == 1
    assert asyncio.run(store.sent_keys()) == {("new", "x", STAGE_EARLY)}


# --- Service --------------------------------------------------------------------


class FakeTasks:
    def __init__(self, tasks: list[Task], now: datetime = NOW) -> None:
        self.tasks = tasks
        self._now = now
        self.timezone = BERLIN
        self.error: Exception | None = None

    def now(self) -> datetime:
        return self._now

    async def list_open_tasks(self) -> list[Task]:
        if self.error is not None:
            raise self.error
        return self.tasks


class Harness:
    def __init__(self, tmp_path: Path, tasks: list[Task]) -> None:
        self.tasks = FakeTasks(tasks)
        self.assistant = AsyncMock()
        self.assistant.write_notice.side_effect = lambda instruction, fallback, timeout: f"TEXT[{fallback}]"
        self.sent: list[str] = []
        self.send_error: Exception | None = None
        self.store = ReminderStore(tmp_path / "db.sqlite")
        self.store.initialize()
        self.service = ReminderService(
            tasks=self.tasks,  # type: ignore[arg-type]
            assistant=self.assistant,
            store=self.store,
            chat_id=42,
            send=self._send,
            owner_name="Ali",
            poll_seconds=60,
            default_time=NOON,
            early=EARLY,
            urgent=URGENT,
            initial_delay=0,
        )

    async def _send(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)


def test_service_sends_each_stage_once_and_records_history(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [timed("a", NOW + timedelta(hours=5)), timed("b", NOW + timedelta(hours=1))])

    assert asyncio.run(harness.service.check_once()) == 2
    assert asyncio.run(harness.service.check_once()) == 0  # nichts doppelt

    assert len(harness.sent) == 2
    assert "Aufgabe a" in harness.sent[0] and "Aufgabe b" in harness.sent[1]
    instructions = [call.args[0] for call in harness.assistant.write_notice.call_args_list]
    assert "dringlicher" not in instructions[0] and "dringlicher" in instructions[1]
    assert "Aufgabe a" in instructions[0] and "Aufgabe b" in instructions[1]
    assert harness.assistant.record_notice.await_count == 2
    harness.assistant.record_notice.assert_any_await(42, REMINDER_HISTORY_MARKER, harness.sent[0])


def test_service_bundles_tasks_of_the_same_stage_into_one_message(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [timed("a", NOW + timedelta(hours=5)), timed("b", NOW + timedelta(hours=6))])

    assert asyncio.run(harness.service.check_once()) == 1

    assert len(harness.sent) == 1
    instruction = harness.assistant.write_notice.call_args.args[0]
    assert "Aufgabe a" in instruction and "Aufgabe b" in instruction


def test_service_survives_restart_without_duplicates(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [timed("a", NOW + timedelta(hours=5))])
    asyncio.run(harness.service.check_once())

    restarted = Harness(tmp_path, [timed("a", NOW + timedelta(hours=5))])

    assert asyncio.run(restarted.service.check_once()) == 0
    assert restarted.sent == []


def test_failed_send_is_retried_on_the_next_run(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [timed("a", NOW + timedelta(hours=5))])
    harness.send_error = RuntimeError("telegram down")

    with pytest.raises(RuntimeError):
        asyncio.run(harness.service.check_once())
    assert asyncio.run(harness.store.sent_keys()) == set()
    harness.assistant.record_notice.assert_not_awaited()

    harness.send_error = None
    assert asyncio.run(harness.service.check_once()) == 1
    assert len(harness.sent) == 1


def test_history_failure_does_not_undo_a_sent_reminder(tmp_path: Path) -> None:
    harness = Harness(tmp_path, [timed("a", NOW + timedelta(hours=5))])
    harness.assistant.record_notice.side_effect = OSError("disk full")

    assert asyncio.run(harness.service.check_once()) == 1
    assert asyncio.run(harness.service.check_once()) == 0


def test_run_loop_survives_errors_and_backs_off_on_rate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(tmp_path, [])
    delays: list[float] = []
    errors = [RuntimeError("boom"), TaskRateLimitError("limit", retry_after=900), None]

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) > 3:
            raise asyncio.CancelledError

    async def check_once() -> int:
        error = errors.pop(0) if errors else None
        if error is not None:
            raise error
        return 0

    monkeypatch.setattr("assistant_bot.tasks.reminders.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(harness.service, "check_once", check_once)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(harness.service.run())

    # Startverzögerung, dann: Fehler -> normales Intervall, Limit -> längere Pause, Erfolg -> Intervall.
    assert delays == [0, 60, 900, 60]
