from __future__ import annotations

from pathlib import Path

import pytest

from assistant_bot.config import (
    DEFAULT_IMAGE_MAX_MB,
    LLM_API_KEY_VARIABLES,
    SUPPORTED_LLM_PROVIDERS,
    ConfigError,
    load_settings,
)
from assistant_bot.llm import PROVIDERS
from tests.conftest import DUMMY_LLM_KEY, DUMMY_TELEGRAM_TOKEN, TEST_LLM_MODEL


def test_defaults_are_applied(valid_env: dict[str, str]) -> None:
    settings = load_settings(valid_env)

    assert settings.telegram_bot_token == DUMMY_TELEGRAM_TOKEN
    assert settings.llm_provider == "openrouter"
    assert settings.llm_api_key == DUMMY_LLM_KEY
    assert settings.allowed_telegram_user_id is None
    assert settings.owner_name == "Ali"
    assert settings.llm_model == TEST_LLM_MODEL
    assert settings.llm_image_model is None
    assert settings.llm_temperature == 0.7
    assert settings.llm_max_tokens == 2048
    assert settings.history_max_messages == 20
    assert settings.max_input_chars == 4000
    assert settings.database_path == Path("data/assistant.db")
    assert settings.log_level == "INFO"


def test_custom_values_are_parsed(valid_env: dict[str, str]) -> None:
    env = {
        **valid_env,
        "ALLOWED_TELEGRAM_USER_ID": " 42 ",
        "OWNER_NAME": "Sam",
        "LLM_MODEL": "anthropic/claude-test",
        "LLM_IMAGE_MODEL": "openai/vision-test",
        "LLM_TEMPERATURE": "0.2",
        "HISTORY_MAX_MESSAGES": "10",
        "LOG_LEVEL": "debug",
        "DATABASE_PATH": "/data/test.db",
    }

    settings = load_settings(env)

    assert settings.allowed_telegram_user_id == 42
    assert settings.owner_name == "Sam"
    assert settings.llm_model == "anthropic/claude-test"
    assert settings.llm_image_model == "openai/vision-test"
    assert settings.llm_temperature == 0.2
    assert settings.history_max_messages == 10
    assert settings.log_level == "DEBUG"
    assert settings.database_path == Path("/data/test.db")


def test_missing_required_values_are_all_reported() -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_settings({})

    message = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "OPENROUTER_API_KEY" in message
    assert "LLM_MODEL" in message


def test_only_the_key_of_the_selected_provider_is_required(valid_env: dict[str, str]) -> None:
    groq_env = {k: v for k, v in valid_env.items() if k != "OPENROUTER_API_KEY"}
    groq_env["LLM_PROVIDER"] = "Groq"

    with pytest.raises(ConfigError, match="GROQ_API_KEY") as exc_info:
        load_settings(groq_env)
    settings = load_settings({**groq_env, "GROQ_API_KEY": "gsk_dummy"})

    assert "OPENROUTER_API_KEY" not in str(exc_info.value)
    assert settings.llm_provider == "groq"
    assert settings.llm_api_key == "gsk_dummy"


def test_unknown_provider_is_rejected(valid_env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load_settings({**valid_env, "LLM_PROVIDER": "altavista"})


def test_supported_providers_match_the_llm_factory() -> None:
    assert set(SUPPORTED_LLM_PROVIDERS) == set(PROVIDERS) == set(LLM_API_KEY_VARIABLES)


def test_image_max_mb_is_configurable(valid_env: dict[str, str]) -> None:
    assert load_settings(valid_env).image_max_mb == DEFAULT_IMAGE_MAX_MB == 5
    assert load_settings(valid_env).image_max_bytes == 5 * 1024 * 1024
    assert load_settings({**valid_env, "IMAGE_MAX_MB": "12"}).image_max_bytes == 12 * 1024 * 1024
    for value in ("0", "21", "viel"):
        with pytest.raises(ConfigError, match="IMAGE_MAX_MB"):
            load_settings({**valid_env, "IMAGE_MAX_MB": value})


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
        ("LLM_TEMPERATURE", "3"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_MAX_RETRIES", "-1"),
        ("LOG_LEVEL", "VERBOSE"),
    ],
)
def test_out_of_range_values_are_rejected(valid_env: dict[str, str], name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({**valid_env, name: value})


def test_secrets_are_not_in_repr(valid_env: dict[str, str]) -> None:
    settings = load_settings(valid_env)

    assert DUMMY_TELEGRAM_TOKEN not in repr(settings)
    assert DUMMY_LLM_KEY not in repr(settings)
    assert settings.secrets == (DUMMY_TELEGRAM_TOKEN, DUMMY_LLM_KEY)
