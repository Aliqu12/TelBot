"""Tests für die Konfiguration der Aufgaben-Integration (Todoist) und der Zeitzone."""

from __future__ import annotations

from datetime import time

import pytest

from assistant_bot.config import ConfigError, load_settings

TOKEN = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def task_env(valid_env: dict[str, str]) -> dict[str, str]:
    return {**valid_env, "TASK_PROVIDER": "Todoist", "TODOIST_API_TOKEN": TOKEN}


def test_tasks_are_disabled_by_default(valid_env: dict[str, str]) -> None:
    settings = load_settings(valid_env)
    assert settings.tasks is None
    assert settings.timezone == "Europe/Berlin"
    assert load_settings({**valid_env, "TASK_PROVIDER": "  "}).tasks is None


def test_todoist_settings_are_parsed(task_env: dict[str, str]) -> None:
    settings = load_settings({**task_env, "TODOIST_TIMEOUT_SECONDS": "20", "TIMEZONE": "Europe/Vienna"})

    assert settings.tasks is not None
    assert settings.tasks.provider == "todoist"
    assert settings.tasks.api_token == TOKEN
    assert settings.tasks.timeout_seconds == 20.0
    assert settings.timezone == "Europe/Vienna"


def test_todoist_token_is_secret_and_not_in_repr(task_env: dict[str, str]) -> None:
    settings = load_settings(task_env)

    assert TOKEN in settings.secrets
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"TASK_PROVIDER": "trello"}, "TASK_PROVIDER"),
        ({"TODOIST_API_TOKEN": ""}, "TODOIST_API_TOKEN"),
        ({"TODOIST_API_TOKEN": "abc def"}, "TODOIST_API_TOKEN"),
        ({"TODOIST_TIMEOUT_SECONDS": "0"}, "TODOIST_TIMEOUT_SECONDS"),
        ({"TIMEZONE": "Mars/Olympus"}, "TIMEZONE"),
    ],
)
def test_invalid_task_settings_are_reported(task_env: dict[str, str], override: dict[str, str], fragment: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({**task_env, **override})

    assert fragment in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)


# --- Erinnerungen ---------------------------------------------------------------


@pytest.fixture
def reminder_env(task_env: dict[str, str]) -> dict[str, str]:
    return {**task_env, "ALLOWED_TELEGRAM_USER_ID": "42"}


def test_reminders_use_defaults_when_tasks_and_owner_are_configured(reminder_env: dict[str, str]) -> None:
    reminders = load_settings(reminder_env).reminders

    assert reminders is not None
    assert reminders.poll_seconds == 300
    assert reminders.default_time == time(12, 0)
    assert (reminders.early_hours, reminders.urgent_hours) == (24, 2)


def test_reminders_are_parsed(reminder_env: dict[str, str]) -> None:
    reminders = load_settings(
        {
            **reminder_env,
            "REMINDER_POLL_SECONDS": "120",
            "REMINDER_DEFAULT_TIME": "09:30",
            "REMINDER_EARLY_HOURS": "48",
            "REMINDER_URGENT_HOURS": "3",
        }
    ).reminders

    assert reminders is not None
    assert (reminders.poll_seconds, reminders.default_time) == (120, time(9, 30))
    assert (reminders.early_hours, reminders.urgent_hours) == (48, 3)


def test_reminders_are_off_without_tasks_owner_or_when_disabled(
    valid_env: dict[str, str], reminder_env: dict[str, str]
) -> None:
    assert load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": "42"}).reminders is None  # keine Aufgaben
    assert load_settings({**reminder_env, "ALLOWED_TELEGRAM_USER_ID": ""}).reminders is None  # kein Besitzer
    assert load_settings({**reminder_env, "REMINDERS_ENABLED": "false"}).reminders is None


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"REMINDER_POLL_SECONDS": "10"}, "REMINDER_POLL_SECONDS"),
        ({"REMINDER_DEFAULT_TIME": "mittags"}, "REMINDER_DEFAULT_TIME"),
        ({"REMINDER_URGENT_HOURS": "24"}, "REMINDER_URGENT_HOURS"),
        ({"REMINDER_EARLY_HOURS": "abc"}, "REMINDER_EARLY_HOURS"),
    ],
)
def test_invalid_reminder_settings_are_reported(
    reminder_env: dict[str, str], override: dict[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({**reminder_env, **override})

    assert fragment in str(excinfo.value)


# --- Wochenrückblick ------------------------------------------------------------


def test_weekly_review_defaults_to_friday_13_00_when_tasks_and_owner_are_configured(reminder_env: dict[str, str]) -> None:
    review = load_settings(reminder_env).weekly_review

    assert review is not None
    assert (review.weekday, review.send_time) == (4, time(13, 0))


@pytest.mark.parametrize(("name", "weekday"), [("Sonntag", 6), ("monday", 0), (" Fr ", 4), ("DONNERSTAG", 3)])
def test_weekly_review_day_and_time_are_parsed(reminder_env: dict[str, str], name: str, weekday: int) -> None:
    review = load_settings({**reminder_env, "WEEKLY_REVIEW_DAY": name, "WEEKLY_REVIEW_TIME": "18:30"}).weekly_review

    assert review is not None
    assert (review.weekday, review.send_time) == (weekday, time(18, 30))


def test_weekly_review_is_off_without_tasks_owner_or_when_disabled(
    valid_env: dict[str, str], reminder_env: dict[str, str]
) -> None:
    assert load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": "42"}).weekly_review is None
    assert load_settings({**reminder_env, "ALLOWED_TELEGRAM_USER_ID": ""}).weekly_review is None
    assert load_settings({**reminder_env, "WEEKLY_REVIEW_ENABLED": "false"}).weekly_review is None
    # unabhängig von den Erinnerungen
    only_review = load_settings({**reminder_env, "REMINDERS_ENABLED": "false"})
    assert only_review.reminders is None and only_review.weekly_review is not None


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"WEEKLY_REVIEW_DAY": "werktag"}, "WEEKLY_REVIEW_DAY"),
        ({"WEEKLY_REVIEW_TIME": "nachmittags"}, "WEEKLY_REVIEW_TIME"),
    ],
)
def test_invalid_weekly_review_settings_are_reported(
    reminder_env: dict[str, str], override: dict[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({**reminder_env, **override})

    assert fragment in str(excinfo.value)
