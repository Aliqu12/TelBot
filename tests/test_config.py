from __future__ import annotations

from pathlib import Path

import pytest

from assistant_bot.config import DEFAULT_GROQ_MODEL, ConfigError, load_settings
from tests.conftest import DUMMY_GROQ_KEY, DUMMY_TELEGRAM_TOKEN


def test_defaults_are_applied(valid_env: dict[str, str]) -> None:
    settings = load_settings(valid_env)

    assert settings.telegram_bot_token == DUMMY_TELEGRAM_TOKEN
    assert settings.groq_api_key == DUMMY_GROQ_KEY
    assert settings.allowed_telegram_user_id is None
    assert settings.owner_name == "Ali"
    assert settings.groq_model == DEFAULT_GROQ_MODEL
    assert settings.history_max_messages == 20
    assert settings.max_input_chars == 4000
    assert settings.database_path == Path("data/assistant.db")
    assert settings.log_level == "INFO"


def test_custom_values_are_parsed(valid_env: dict[str, str]) -> None:
    env = {
        **valid_env,
        "ALLOWED_TELEGRAM_USER_ID": " 42 ",
        "OWNER_NAME": "Sam",
        "GROQ_MODEL": "llama-3.3-70b-versatile",
        "GROQ_TEMPERATURE": "0.2",
        "HISTORY_MAX_MESSAGES": "10",
        "LOG_LEVEL": "debug",
        "DATABASE_PATH": "/data/test.db",
    }

    settings = load_settings(env)

    assert settings.allowed_telegram_user_id == 42
    assert settings.owner_name == "Sam"
    assert settings.groq_model == "llama-3.3-70b-versatile"
    assert settings.groq_temperature == 0.2
    assert settings.history_max_messages == 10
    assert settings.log_level == "DEBUG"
    assert settings.database_path == Path("/data/test.db")


def test_missing_required_values_are_all_reported() -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_settings({})

    message = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "GROQ_API_KEY" in message


def test_invalid_token_format_is_rejected_without_leaking_it(valid_env: dict[str, str]) -> None:
    secret = "not-a-valid-token-but-secret"

    with pytest.raises(ConfigError) as exc_info:
        load_settings({**valid_env, "TELEGRAM_BOT_TOKEN": secret})

    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize("value", ["abc", "-5", "0", "12.5"])
def test_invalid_allowed_user_id_is_rejected(valid_env: dict[str, str], value: str) -> None:
    with pytest.raises(ConfigError, match="ALLOWED_TELEGRAM_USER_ID"):
        load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HISTORY_MAX_MESSAGES", "0"),
        ("HISTORY_MAX_MESSAGES", "zwanzig"),
        ("MAX_INPUT_CHARS", "100000"),
        ("GROQ_TEMPERATURE", "3"),
        ("GROQ_TIMEOUT_SECONDS", "0"),
        ("GROQ_MAX_RETRIES", "-1"),
        ("LOG_LEVEL", "VERBOSE"),
    ],
)
def test_out_of_range_values_are_rejected(valid_env: dict[str, str], name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({**valid_env, name: value})


def test_secrets_are_not_in_repr(valid_env: dict[str, str]) -> None:
    settings = load_settings(valid_env)

    assert DUMMY_TELEGRAM_TOKEN not in repr(settings)
    assert DUMMY_GROQ_KEY not in repr(settings)
    assert settings.secrets == (DUMMY_TELEGRAM_TOKEN, DUMMY_GROQ_KEY)
