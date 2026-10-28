"""Tests für TaskService: Zeiträume, Zeitzone/Sommerzeit, Mehrdeutigkeit, Duplikate, Wiederholungen, Löschfreigabe."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from assistant_bot.tasks.models import (
    NewTask,
    Project,
    Task,
    TaskAmbiguousError,
    TaskChanges,
    TaskDue,
    TaskNotFoundError,
    TaskTimeoutError,
    TaskValidationError,
)
from assistant_bot.tasks.service import (
    DeletionAlreadyDoneError,
    DeletionExpiredError,
    DeletionNotFoundError,
    TaskService,
)

BERLIN = ZoneInfo("Europe/Berlin")
CHAT = 42
NOW = datetime(2026, 10, 28, 10, 0, tzinfo=BERLIN)  # Mittwoch


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BERLIN)


def make_task(task_id: str, content: str, due: TaskDue | None = None, **kwargs: object) -> Task:
    return Task(task_id=task_id, content=content, due=due, **kwargs)  # type: ignore[arg-type]


def all_day(day: date, recurring: bool = False, text: str = "") -> TaskDue:
    return TaskDue(date=day, is_recurring=recurring, text=text)


def timed(day: date, hour: int, minute: int = 0, recurring: bool = False, text: str = "") -> TaskDue:
    return TaskDue(date=day, time=at(day, hour, minute), is_recurring=recurring, text=text)


class FakeTaskProvider:
    name = "fake"

    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.tasks = {t.task_id: t for t in tasks or []}
        self.projects = [Project("inbox", "Inbox", True), Project("p-haus", "Haushalt"), Project("p-arb", "Arbeit")]
        self.created: list[NewTask] = []
        self.updates: list[tuple[str, TaskChanges]] = []
        self.completed: list[str] = []
        self.deleted: list[str] = []
        self.list_calls: list[str | None] = []
        self.create_error: Exception | None = None
        self.create_delay = 0.0
        self.next_due_after_complete: TaskDue | None = None

    async def list_tasks(self, project_id: str | None = None) -> list[Task]:
        self.list_calls.append(project_id)
        return [t for t in self.tasks.values() if project_id is None or t.project_id == project_id]

    async def get_task(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise TaskNotFoundError("nicht gefunden")
        return self.tasks[task_id]

    async def create_task(self, task: NewTask) -> Task:
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        if self.create_error:
            raise self.create_error
        self.created.append(task)
        new = Task(task_id=f"new{len(self.created)}", content=task.content)
        self.tasks[new.task_id] = new
        return new

    async def update_task(self, task_id: str, changes: TaskChanges) -> Task:
        self.updates.append((task_id, changes))
        return self.tasks[task_id]

    async def complete_task(self, task_id: str) -> None:
        self.completed.append(task_id)
        task = self.tasks[task_id]
        if task.due is not None and task.due.is_recurring and self.next_due_after_complete:
            self.tasks[task_id] = replace(task, due=self.next_due_after_complete)
        else:
            del self.tasks[task_id]

    async def delete_task(self, task_id: str) -> None:
        self.deleted.append(task_id)
        self.tasks.pop(task_id, None)

    async def list_projects(self) -> list[Project]:
        return self.projects

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


TODAY = NOW.date()
TOMORROW = date(2026, 10, 29)
YESTERDAY = date(2026, 10, 27)


def default_tasks() -> list[Task]:
    return [
        make_task("t-allday", "Rechnung bezahlen", all_day(TODAY)),
        make_task("t-morning", "Arzt anrufen", timed(TODAY, 9)),  # heute, Uhrzeit schon vorbei
        make_task("t-evening", "Sport", timed(TODAY, 18), priority=1),
        make_task("t-tomorrow", "Rechnung Strom prüfen", all_day(TOMORROW), project_id="p-haus"),
        make_task("t-old", "Steuer", all_day(YESTERDAY)),
        make_task("t-fri", "Waschmaschine reparieren", timed(date(2026, 10, 30), 16)),
        make_task("t-later", "Urlaub planen", all_day(date(2026, 11, 20))),
        make_task("t-nodate", "Buch lesen"),
        make_task("t-muell", "Müll rausbringen", all_day(date(2026, 11, 1), True, "jeden Sonntag")),
    ]


def setup(tasks: list[Task] | None = None, now: datetime = NOW) -> tuple[TaskService, FakeTaskProvider, Clock]:
    provider = FakeTaskProvider(default_tasks() if tasks is None else tasks)
    clock = Clock()
    service = TaskService(provider, timezone=BERLIN, clock=clock, now=lambda: now)
    return service, provider, clock


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def ids(tasks: list[Task]) -> list[str]:
    return [t.task_id for t in tasks]


# --- Zeiträume ------------------------------------------------------------------


def test_today_is_sorted_by_time_and_reports_overdue_count() -> None:
    service, _, _ = setup()

    listing = run(service.list_tasks(CHAT, scope="today"))

    assert ids(listing.tasks) == ["t-morning", "t-evening", "t-allday"]
    assert listing.total == 3 and listing.overdue_count == 1


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("tomorrow", ["t-tomorrow"]),
        ("overdue", ["t-old", "t-morning"]),
        ("week", ["t-morning", "t-evening", "t-allday", "t-tomorrow", "t-fri", "t-muell"]),
        ("no_date", ["t-nodate"]),
    ],
)
def test_scopes(scope: str, expected: list[str]) -> None:
    service, _, _ = setup()
    assert ids(run(service.list_tasks(CHAT, scope=scope)).tasks) == expected


def test_specific_date_search_project_and_limit() -> None:
    service, provider, _ = setup()

    assert ids(run(service.list_tasks(CHAT, scope="date", on=date(2026, 10, 30))).tasks) == ["t-fri"]
    assert ids(run(service.list_tasks(CHAT, scope="all", search="rechnung")).tasks) == ["t-allday", "t-tomorrow"]
    assert ids(run(service.list_tasks(CHAT, scope="all", project="haus")).tasks) == ["t-tomorrow"]
    assert provider.list_calls[-1] == "p-haus"
    limited = run(service.list_tasks(CHAT, scope="all", limit=2))
    assert len(limited.tasks) == 2 and limited.total == 9
    with pytest.raises(TaskValidationError):
        run(service.list_tasks(CHAT, scope="date"))
    with pytest.raises(TaskNotFoundError, match="Haushalt"):
        run(service.list_tasks(CHAT, project="Garten"))


def test_today_follows_configured_timezone_not_utc() -> None:
    # 23:30 UTC am 28.10. ist in Berlin bereits der 29.10. (00:30) -> "heute" ist der 29.10.
    late = datetime(2026, 10, 28, 23, 30, tzinfo=UTC).astimezone(BERLIN)
    service, _, _ = setup(now=late)

    assert ids(run(service.list_tasks(CHAT, scope="today")).tasks) == ["t-tomorrow"]


# --- Bezug auf Aufgaben ---------------------------------------------------------


def test_numbers_refer_to_last_listing_and_expire() -> None:
    service, provider, clock = setup()
    run(service.list_tasks(CHAT, scope="today"))

    result = run(service.complete_task(CHAT, number=2))

    assert result.task.task_id == "t-evening" and provider.completed == ["t-evening"]
    with pytest.raises(TaskValidationError, match="Nummer 9"):
        run(service.complete_task(CHAT, number=9))
    clock.now += 3601
    with pytest.raises(TaskValidationError, match="zuerst"):
        run(service.complete_task(CHAT, number=1))


def test_query_resolves_unique_task() -> None:
    service, provider, _ = setup()

    run(service.complete_task(CHAT, query="waschmaschine"))

    assert provider.completed == ["t-fri"]


def test_ambiguous_query_asks_back_and_offers_numbers() -> None:
    service, provider, _ = setup()

    with pytest.raises(TaskAmbiguousError) as excinfo:
        run(service.complete_task(CHAT, query="Rechnung"))

    assert ids(excinfo.value.candidates) == ["t-allday", "t-tomorrow"]
    assert provider.completed == []
    run(service.complete_task(CHAT, number=2))  # Nummer aus der Rückfrage
    assert provider.completed == ["t-tomorrow"]


def test_exact_title_wins_over_partial_matches() -> None:
    service, provider, _ = setup(default_tasks() + [make_task("t-telekom", "Rechnung bezahlen Telekom")])

    run(service.complete_task(CHAT, query="rechnung bezahlen"))

    assert provider.completed == ["t-allday"]


def test_unknown_query_is_reported() -> None:
    service, _, _ = setup()
    with pytest.raises(TaskNotFoundError):
        run(service.complete_task(CHAT, query="Fenster putzen"))
    with pytest.raises(TaskValidationError):
        run(service.complete_task(CHAT))


# --- Anlegen --------------------------------------------------------------------


def test_create_with_time_duration_project_and_priority() -> None:
    service, provider, _ = setup()

    result = run(
        service.create_task(
            CHAT,
            content="  Waschmaschine\nreparieren ",
            due_date=date(2026, 10, 30),
            due_time=time(16, 0),
            duration_minutes=60,
            priority=1,
            project="haushalt",
            labels=["@Haus", "haus"],
        )
    )

    assert not result.duplicate
    created = provider.created[0]
    assert created.content == "Waschmaschine reparieren"
    assert created.due.at == at(date(2026, 10, 30), 16)
    assert (created.duration_minutes, created.priority, created.project_id) == (60, 1, "p-haus")
    assert created.labels == ("Haus",)


def test_create_recurring_task() -> None:
    service, provider, _ = setup()

    run(service.create_task(CHAT, content="Müll rausbringen", recurrence="jeden Sonntag"))

    assert provider.created[0].due.recurrence == "jeden Sonntag"


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"content": " "}, "leer"),
        ({"content": "x", "due_time": time(9, 0)}, "Datum"),
        ({"content": "x", "due_date": YESTERDAY}, "Vergangenheit"),
        ({"content": "x", "due_date": TODAY, "due_time": time(8, 0)}, "Vergangenheit"),
        ({"content": "x", "due_date": date(2027, 3, 28), "due_time": time(2, 30)}, "Zeitumstellung"),
        ({"content": "x", "recurrence": "jeden Tag", "due_date": TOMORROW}, "recurrence"),
        ({"content": "x", "due_date": TOMORROW, "duration_minutes": 30}, "Uhrzeit"),
        ({"content": "x", "priority": 5}, "Priorität"),
        ({"content": "x" * 501}, "zu lang"),
    ],
)
def test_create_validation(kwargs: dict, fragment: str) -> None:
    service, provider, _ = setup()

    with pytest.raises(TaskValidationError, match=fragment):
        run(service.create_task(CHAT, **kwargs))
    assert provider.created == []


def test_identical_create_within_two_minutes_is_not_duplicated() -> None:
    service, provider, clock = setup()
    kwargs = {"content": "Rechnung bezahlen", "due_date": TOMORROW}

    first = run(service.create_task(CHAT, **kwargs))
    second = run(service.create_task(CHAT, **{**kwargs, "content": "rechnung  BEZAHLEN"}))
    other_day = run(service.create_task(CHAT, content="Rechnung bezahlen", due_date=date(2026, 10, 30)))

    assert second.duplicate and second.task.task_id == first.task.task_id
    assert not other_day.duplicate
    assert len(provider.created) == 2
    clock.now += 121
    assert not run(service.create_task(CHAT, **kwargs)).duplicate
    assert len(provider.created) == 3


def test_parallel_identical_creates_only_create_once() -> None:
    service, provider, _ = setup()
    provider.create_delay = 0.01

    async def both() -> list:
        return await asyncio.gather(
            service.create_task(CHAT, content="Einkaufen"),
            service.create_task(CHAT, content="Einkaufen"),
            return_exceptions=True,
        )

    results = run(both())

    assert len(provider.created) == 1
    assert any(isinstance(r, TaskValidationError) for r in results)


def test_retry_after_timeout_is_blocked_to_avoid_duplicates() -> None:
    service, provider, _ = setup()
    provider.create_error = TaskTimeoutError("Zeitüberschreitung")

    with pytest.raises(TaskTimeoutError):
        run(service.create_task(CHAT, content="Einkaufen"))
    provider.create_error = None
    with pytest.raises(TaskValidationError, match="prüfen"):
        run(service.create_task(CHAT, content="Einkaufen"))
    assert provider.created == []


def test_failed_create_can_be_retried() -> None:
    service, provider, _ = setup()
    provider.create_error = TaskValidationError("abgelehnt")

    with pytest.raises(TaskValidationError):
        run(service.create_task(CHAT, content="Einkaufen"))
    provider.create_error = None
    assert not run(service.create_task(CHAT, content="Einkaufen")).duplicate


def test_daylight_saving_time_is_respected() -> None:
    service, provider, _ = setup(now=datetime(2026, 10, 24, 23, 30, tzinfo=BERLIN))

    run(service.create_task(CHAT, content="Sommer", due_date=date(2026, 10, 25), due_time=time(1, 30)))
    run(service.create_task(CHAT, content="Winter", due_date=date(2026, 10, 25), due_time=time(10, 0)))

    summer, winter = (t.due.at.astimezone(UTC) for t in provider.created)
    assert summer == datetime(2026, 10, 24, 23, 30, tzinfo=UTC)  # 01:30 MESZ
    assert winter == datetime(2026, 10, 25, 9, 0, tzinfo=UTC)  # 10:00 MEZ


# --- Ändern ---------------------------------------------------------------------


def test_moving_to_another_day_keeps_the_time() -> None:
    service, provider, _ = setup()

    run(service.update_task(CHAT, query="Waschmaschine", due_date=date(2026, 11, 2)))

    task_id, changes = provider.updates[0]
    assert task_id == "t-fri" and changes.due.at == at(date(2026, 11, 2), 16)


def test_changing_only_the_time_keeps_the_day() -> None:
    service, provider, _ = setup()

    run(service.update_task(CHAT, query="Waschmaschine", due_time=time(18, 30)))

    assert provider.updates[0][1].due.at == at(date(2026, 10, 30), 18, 30)


def test_recurring_task_is_protected_from_fixed_date() -> None:
    service, provider, _ = setup()

    with pytest.raises(TaskValidationError, match="wiederholt sich"):
        run(service.update_task(CHAT, query="Müll", due_date=date(2026, 11, 3)))
    assert provider.updates == []

    run(service.update_task(CHAT, query="Müll", due_date=date(2026, 11, 3), remove_recurrence=True))
    run(service.update_task(CHAT, query="Müll", recurrence="jeden Montag"))
    run(service.update_task(CHAT, query="Müll", remove_recurrence=True))

    dues = [changes.due for _, changes in provider.updates]
    assert dues[0].on == date(2026, 11, 3)
    assert dues[1].recurrence == "jeden Montag"
    assert dues[2].on == date(2026, 11, 1)  # nächster Termin bleibt als festes Datum


def test_update_validation() -> None:
    service, provider, _ = setup()

    with pytest.raises(TaskValidationError, match="keine Änderung"):
        run(service.update_task(CHAT, query="Waschmaschine"))
    with pytest.raises(TaskValidationError, match="Uhrzeit"):
        run(service.update_task(CHAT, query="Urlaub", duration_minutes=30))
    with pytest.raises(TaskValidationError, match="kein Datum"):
        run(service.update_task(CHAT, query="Buch", due_time=time(10, 0)))
    assert provider.updates == []

    run(service.update_task(CHAT, query="Waschmaschine", remove_due=True, priority=2))
    changes = provider.updates[0][1]
    assert changes.due.clear and changes.priority == 2


# --- Erledigen ------------------------------------------------------------------


def test_completing_recurring_task_reports_next_occurrence() -> None:
    service, provider, _ = setup()
    provider.next_due_after_complete = all_day(date(2026, 11, 8), True, "jeden Sonntag")

    result = run(service.complete_task(CHAT, query="Müll"))

    assert provider.completed == ["t-muell"]
    assert result.next_due is not None and result.next_due.date == date(2026, 11, 8)


# --- Löschen --------------------------------------------------------------------


def test_deletion_requires_confirmation_and_happens_once() -> None:
    service, provider, _ = setup()

    deletion = run(service.request_deletion(CHAT, query="Urlaub"))

    assert provider.deleted == []
    assert service.take_deletion_for_review(CHAT) == deletion
    assert service.take_deletion_for_review(CHAT) is None  # nur einmal anzeigen
    assert run(service.confirm_deletion(CHAT, deletion.deletion_id)).task_id == "t-later"
    with pytest.raises(DeletionAlreadyDoneError):
        run(service.confirm_deletion(CHAT, deletion.deletion_id))
    assert provider.deleted == ["t-later"]


def test_deletion_expires_and_can_be_cancelled() -> None:
    service, provider, clock = setup()

    expired = run(service.request_deletion(CHAT, query="Urlaub"))
    clock.now += 301
    with pytest.raises(DeletionExpiredError):
        run(service.confirm_deletion(CHAT, expired.deletion_id))

    pending = run(service.request_deletion(CHAT, query="Urlaub"))
    with pytest.raises(DeletionNotFoundError):
        run(service.confirm_deletion(CHAT, "deadbeef"))
    with pytest.raises(DeletionNotFoundError):
        run(service.confirm_deletion(CHAT + 1, pending.deletion_id))
    assert service.cancel_deletion(CHAT, pending.deletion_id)
    with pytest.raises(DeletionNotFoundError):
        run(service.confirm_deletion(CHAT, pending.deletion_id))
    assert provider.deleted == []
