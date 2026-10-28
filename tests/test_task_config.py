"""Tests für die Konfiguration der Aufgaben-Integration (Todoist) und der Zeitzone."""

from __future__ import annotations

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
