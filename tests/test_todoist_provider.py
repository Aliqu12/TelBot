"""Tests für TodoistTaskProvider – ausschließlich gegen einen gefälschten HTTP-Transport (kein echter Token)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from assistant_bot.config import TaskSettings
from assistant_bot.tasks.factory import create_task_provider
from assistant_bot.tasks.models import (
    DueInput,
    NewTask,
    TaskAuthenticationError,
    TaskChanges,
    TaskConnectionError,
    TaskError,
    TaskNotFoundError,
    TaskRateLimitError,
    TaskTimeoutError,
    TaskValidationError,
)
from assistant_bot.tasks.todoist import TodoistTaskProvider

BERLIN = ZoneInfo("Europe/Berlin")
TOKEN = "test-token-not-real"


def task_json(task_id: str = "6X7rM8997g3RQmvh", **overrides: object) -> dict:
    data = {
        "id": task_id,
        "content": "Waschmaschine reparieren",
        "description": "",
        "project_id": "6Jf8VQXxpwv56VQ7",
        "priority": 1,
        "labels": [],
        "due": None,
        "duration": None,
    }
    data.update(overrides)
    return data


class Recorder:
    """Gefälschte Todoist-API: liefert vorbereitete Antworten und merkt sich alle Anfragen."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def make_provider(recorder: Recorder) -> tuple[TodoistTaskProvider, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = TodoistTaskProvider(
        TOKEN, timezone=BERLIN, timeout_seconds=5, transport=httpx.MockTransport(recorder), sleep=fake_sleep
    )
    return provider, sleeps


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


# --- Anlegen und Ändern -------------------------------------------------------


def test_create_task_sends_utc_time_priority_and_duration() -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json=task_json(
                priority=4,
                due={"date": "2026-10-30T15:00:00Z", "timezone": "Europe/Berlin", "string": "30 Okt 16:00",
                     "is_recurring": False},
                duration={"amount": 90, "unit": "minute"},
                labels=["Haushalt"],
            ),
        )
    )
    provider, _ = make_provider(recorder)
    new = NewTask(
        content="Waschmaschine reparieren",
        priority=1,
        due=DueInput(at=datetime(2026, 10, 30, 16, 0, tzinfo=BERLIN)),
        duration_minutes=90,
        labels=("Haushalt",),
    )

    task = run(provider.create_task(new))

    request = recorder.requests[0]
    assert request.method == "POST" and request.url.path == "/api/v1/tasks"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    # 30.10.2026 ist nach Ende der Sommerzeit: 16:00 MEZ = 15:00 UTC.
    assert recorder.body() == {
        "content": "Waschmaschine reparieren",
        "priority": 4,
        "labels": ["Haushalt"],
        "due_datetime": "2026-10-30T15:00:00Z",
        "duration": 90,
        "duration_unit": "minute",
    }
    assert task.priority == 1
    assert task.due is not None and task.due.time == datetime(2026, 10, 30, 16, 0, tzinfo=BERLIN)
    assert task.due.date == date(2026, 10, 30)
    assert task.duration_minutes == 90
    assert task.url == "https://app.todoist.com/app/task/6X7rM8997g3RQmvh"


def test_summer_time_is_converted_correctly() -> None:
    recorder = Recorder(httpx.Response(200, json=task_json()))
    provider, _ = make_provider(recorder)

    run(provider.create_task(NewTask(content="x", due=DueInput(at=datetime(2026, 10, 24, 16, 0, tzinfo=BERLIN)))))

    assert recorder.body()["due_datetime"] == "2026-10-24T14:00:00Z"  # MESZ = UTC+2


def test_create_recurring_and_all_day_tasks() -> None:
    recorder = Recorder(httpx.Response(200, json=task_json()), httpx.Response(200, json=task_json()))
    provider, _ = make_provider(recorder)

    run(provider.create_task(NewTask(content="Müll rausbringen", due=DueInput(recurrence="jeden Sonntag"))))
    run(provider.create_task(NewTask(content="Rechnung bezahlen", due=DueInput(on=date(2026, 10, 29)))))

    assert recorder.body(0) == {"content": "Müll rausbringen", "due_string": "jeden Sonntag", "due_lang": "de"}
    assert recorder.body(1) == {"content": "Rechnung bezahlen", "due_date": "2026-10-29"}


def test_update_can_clear_due_and_duration() -> None:
    recorder = Recorder(httpx.Response(200, json=task_json()))
    provider, _ = make_provider(recorder)

    run(provider.update_task("abc123", TaskChanges(due=DueInput(clear=True), clear_duration=True, content="Neu")))

    assert recorder.requests[0].url.path == "/api/v1/tasks/abc123"
    assert recorder.body() == {
        "content": "Neu",
        "due_string": "no date",
        "due_lang": "en",
        "duration": None,
        "duration_unit": None,
    }


def test_complete_and_delete_use_the_right_endpoints() -> None:
    recorder = Recorder(httpx.Response(204), httpx.Response(204))
    provider, _ = make_provider(recorder)

    run(provider.complete_task("abc123"))
    run(provider.delete_task("abc123"))

    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("POST", "/api/v1/tasks/abc123/close"),
        ("DELETE", "/api/v1/tasks/abc123"),
    ]


def test_invalid_ids_are_rejected_before_any_request() -> None:
    recorder = Recorder()
    provider, _ = make_provider(recorder)

    with pytest.raises(TaskValidationError):
        run(provider.delete_task("../projects/1"))
    assert recorder.requests == []


# --- Lesen ----------------------------------------------------------------------


def test_list_tasks_follows_pagination_and_parses_due_variants() -> None:
    floating_task = task_json("a2", due={"date": "2026-10-28T09:30:00", "timezone": None, "is_recurring": False})
    page1 = {
        "results": [
            task_json("a1", due={"date": "2026-10-28", "is_recurring": False, "string": "heute"}),
            floating_task,
        ],
        "next_cursor": "CURSOR2",
    }
    page2 = {
        "results": [
            floating_task,  # Duplikat über Seitengrenzen
            task_json(
                "a3",
                due={"date": "2026-11-01T09:00:00Z", "timezone": "Europe/Berlin", "is_recurring": True,
                     "string": "jeden Sonntag um 10 Uhr"},
            ),
        ],
        "next_cursor": None,
    }
    recorder = Recorder(httpx.Response(200, json=page1), httpx.Response(200, json=page2))
    provider, _ = make_provider(recorder)

    tasks = run(provider.list_tasks())

    assert [t.task_id for t in tasks] == ["a1", "a2", "a3"]
    assert recorder.requests[0].url.params["limit"] == "200"
    assert recorder.requests[1].url.params["cursor"] == "CURSOR2"
    all_day, floating, fixed = tasks[0].due, tasks[1].due, tasks[2].due
    assert all_day.date == date(2026, 10, 28) and all_day.time is None
    assert floating.time == datetime(2026, 10, 28, 9, 30, tzinfo=BERLIN)
    assert fixed.time == datetime(2026, 11, 1, 10, 0, tzinfo=BERLIN) and fixed.is_recurring
    assert fixed.text == "jeden Sonntag um 10 Uhr"


def test_task_created_at_is_read_from_added_at() -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "results": [task_json("a1", added_at="2026-10-30T08:15:00.000000Z"), task_json("a2")],
                "next_cursor": None,
            },
        )
    )
    provider, _ = make_provider(recorder)

    first, second = run(provider.list_tasks())

    assert first.created_at == datetime(2026, 10, 30, 8, 15, tzinfo=UTC)
    assert second.created_at is None


def test_list_projects() -> None:
    recorder = Recorder(
        httpx.Response(
            200,
            json={
                "results": [
                    {"id": "p1", "name": "Inbox", "inbox_project": True},
                    {"id": "p2", "name": "Haushalt"},
                    {"id": "p3", "name": "Alt", "is_archived": True},
                ],
                "next_cursor": None,
            },
        )
    )
    provider, _ = make_provider(recorder)

    projects = run(provider.list_projects())

    assert [(p.name, p.is_inbox) for p in projects] == [("Inbox", True), ("Haushalt", False)]


# --- Fehler und Wiederholungen ------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, TaskAuthenticationError), (404, TaskNotFoundError), (503, TaskConnectionError)],
)
def test_http_errors_are_mapped(status: int, error: type[Exception]) -> None:
    recorder = Recorder(httpx.Response(status, json={"error": "x"}))
    provider, _ = make_provider(recorder)

    with pytest.raises(error) as excinfo:
        run(provider.complete_task("abc"))
    assert TOKEN not in str(excinfo.value)
    assert len(recorder.requests) == 1  # Schreibzugriffe werden nicht wiederholt


def test_validation_error_contains_sanitized_detail() -> None:
    recorder = Recorder(httpx.Response(400, json={"error": "Invalid date format\x07", "error_code": 42}))
    provider, _ = make_provider(recorder)

    with pytest.raises(TaskValidationError, match="Invalid date format$"):
        run(provider.create_task(NewTask(content="x")))


def test_get_is_retried_after_short_rate_limit_and_server_error() -> None:
    recorder = Recorder(
        httpx.Response(429, json={"error": "Too many", "error_extra": {"retry_after": 2}}),
        httpx.Response(200, json=task_json()),
        httpx.Response(502),
        httpx.Response(200, json=task_json()),
    )
    provider, sleeps = make_provider(recorder)

    run(provider.get_task("abc"))
    run(provider.get_task("abc"))

    assert len(recorder.requests) == 4
    assert sleeps == [2]


def test_long_rate_limit_is_reported_with_retry_after() -> None:
    recorder = Recorder(httpx.Response(429, json={"error_extra": {"retry_after": 40}}))
    provider, sleeps = make_provider(recorder)

    with pytest.raises(TaskRateLimitError) as excinfo:
        run(provider.create_task(NewTask(content="x")))

    assert excinfo.value.retry_after == 40 and "40 Sekunden" in str(excinfo.value)
    assert sleeps == [] and len(recorder.requests) == 1


def test_write_timeout_is_not_retried_and_warns_about_uncertainty() -> None:
    recorder = Recorder(httpx.ReadTimeout("timeout"))
    provider, _ = make_provider(recorder)

    with pytest.raises(TaskTimeoutError, match="möglicherweise"):
        run(provider.create_task(NewTask(content="x")))
    assert len(recorder.requests) == 1


def test_connect_error_is_retried_even_for_writes() -> None:
    recorder = Recorder(httpx.ConnectError("refused"), httpx.Response(200, json=task_json()))
    provider, _ = make_provider(recorder)

    run(provider.create_task(NewTask(content="x")))

    assert len(recorder.requests) == 2


def test_unexpected_payload_is_reported() -> None:
    recorder = Recorder(httpx.Response(200, json={"unexpected": True}))
    provider, _ = make_provider(recorder)

    with pytest.raises(TaskConnectionError):
        run(provider.list_tasks())


def test_factory_creates_todoist_provider() -> None:
    provider = create_task_provider(TaskSettings(provider="todoist", api_token=TOKEN), BERLIN)
    assert isinstance(provider, TodoistTaskProvider)
    run(provider.aclose())


# --- Projekte, Abschnitte, Labels, Verschieben, erledigte Aufgaben ------------------


def test_project_calls() -> None:
    recorder = Recorder(
        httpx.Response(200, json={"id": "p1", "name": "Garten", "parent_id": "p0", "inbox_project": False}),
        httpx.Response(200, json={"id": "p1", "name": "Beet"}),
        httpx.Response(200, json={"id": "p1", "name": "Beet", "is_archived": True}),
        httpx.Response(200, json={"id": "p1", "name": "Beet"}),
        httpx.Response(204),
    )
    provider, _ = make_provider(recorder)

    created = run(provider.create_project("Garten", "p0"))
    renamed = run(provider.rename_project("p1", "Beet"))
    run(provider.archive_project("p1"))
    run(provider.unarchive_project("p1"))
    run(provider.delete_project("p1"))

    assert (created.project_id, created.parent_id, renamed.name) == ("p1", "p0", "Beet")
    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("POST", "/api/v1/projects"),
        ("POST", "/api/v1/projects/p1"),
        ("POST", "/api/v1/projects/p1/archive"),
        ("POST", "/api/v1/projects/p1/unarchive"),
        ("DELETE", "/api/v1/projects/p1"),
    ]
    assert json.loads(recorder.requests[0].content) == {"name": "Garten", "parent_id": "p0"}
    assert json.loads(recorder.requests[1].content) == {"name": "Beet"}


def test_archived_projects_are_marked() -> None:
    recorder = Recorder(httpx.Response(200, json={"results": [{"id": "p9", "name": "Alt"}], "next_cursor": None}))
    provider, _ = make_provider(recorder)

    projects = run(provider.list_archived_projects())

    assert recorder.requests[0].url.path == "/api/v1/projects/archived"
    assert [(p.project_id, p.is_archived) for p in projects] == [("p9", True)]


def test_sections_and_labels() -> None:
    recorder = Recorder(
        httpx.Response(200, json={"results": [{"id": "s1", "name": "Küche", "project_id": "p1"}], "next_cursor": None}),
        httpx.Response(200, json={"id": "s2", "name": "Bad", "project_id": "p1"}),
        httpx.Response(200, json={"results": [{"id": "l1", "name": "privat"}], "next_cursor": None}),
        httpx.Response(200, json={"id": "l2", "name": "Neu"}),
        httpx.Response(200, json={"id": "l2", "name": "Alt"}),
        httpx.Response(204),
    )
    provider, _ = make_provider(recorder)

    sections = run(provider.list_sections("p1"))
    created = run(provider.create_section("p1", "Bad"))
    labels = run(provider.list_labels())
    run(provider.create_label("Neu"))
    renamed = run(provider.rename_label("l2", "Alt"))
    run(provider.delete_label("l2"))

    assert [(s.section_id, s.project_id, s.name) for s in sections] == [("s1", "p1", "Küche")]
    assert recorder.requests[0].url.params["project_id"] == "p1"
    assert created.section_id == "s2" and json.loads(recorder.requests[1].content) == {"name": "Bad", "project_id": "p1"}
    assert [label.name for label in labels] == ["privat"] and renamed.name == "Alt"
    assert [(r.method, r.url.path) for r in recorder.requests[3:]] == [
        ("POST", "/api/v1/labels"),
        ("POST", "/api/v1/labels/l2"),
        ("DELETE", "/api/v1/labels/l2"),
    ]


def test_move_task_sends_exactly_one_target() -> None:
    recorder = Recorder(
        httpx.Response(200, json=task_json(project_id="p2")),
        httpx.Response(200, json=task_json(project_id="p2", section_id="s1")),
    )
    provider, _ = make_provider(recorder)

    to_project = run(provider.move_task("t1", project_id="p2"))
    to_section = run(provider.move_task("t1", section_id="s1"))

    assert recorder.requests[0].url.path == "/api/v1/tasks/t1/move"
    assert json.loads(recorder.requests[0].content) == {"project_id": "p2"}
    assert json.loads(recorder.requests[1].content) == {"section_id": "s1"}
    assert to_project.section_id is None and to_section.section_id == "s1"
    with pytest.raises(TaskValidationError):
        run(provider.move_task("t1"))
    with pytest.raises(TaskValidationError):
        run(provider.move_task("t1", project_id="p2", section_id="s1"))
    with pytest.raises(TaskValidationError):
        run(provider.move_task("../x", project_id="p2"))
    assert len(recorder.requests) == 2


def test_completed_tasks_use_items_key_utc_range_and_keep_recurring_repeats() -> None:
    page1 = {
        "items": [
            task_json("a", completed_at="2026-10-26T13:30:00Z"),
            task_json("r", completed_at="2026-10-24T08:00:00Z"),
        ],
        "next_cursor": "abc",
    }
    page2 = {"items": [task_json("r", completed_at="2026-10-25T08:00:00Z")], "next_cursor": None}
    recorder = Recorder(httpx.Response(200, json=page1), httpx.Response(200, json=page2))
    provider, _ = make_provider(recorder)

    tasks = run(
        provider.list_completed_tasks(
            datetime(2026, 10, 23, 13, 0, tzinfo=BERLIN), datetime(2026, 10, 30, 13, 0, tzinfo=BERLIN), "p1"
        )
    )

    params = recorder.requests[0].url.params
    assert recorder.requests[0].url.path == "/api/v1/tasks/completed/by_completion_date"
    assert (params["since"], params["until"], params["project_id"]) == (
        "2026-10-23T11:00:00Z",
        "2026-10-30T12:00:00Z",
        "p1",
    )
    assert recorder.requests[1].url.params["cursor"] == "abc"
    assert [(t.task_id, t.completed_at) for t in tasks] == [
        ("a", datetime(2026, 10, 26, 14, 30, tzinfo=BERLIN)),
        ("r", datetime(2026, 10, 24, 10, 0, tzinfo=BERLIN)),
        ("r", datetime(2026, 10, 25, 9, 0, tzinfo=BERLIN)),
    ]


def test_plan_limit_and_missing_entry_messages() -> None:
    recorder = Recorder(httpx.Response(403, json={"error": "limit"}), httpx.Response(404, json={}))
    provider, _ = make_provider(recorder)

    with pytest.raises(TaskError, match="Limit"):
        run(provider.create_label("X"))
    with pytest.raises(TaskNotFoundError):
        run(provider.delete_project("p1"))
