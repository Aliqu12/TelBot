"""Tests für die Konfiguration der Websuche."""

from __future__ import annotations

import pytest

from assistant_bot.config import ConfigError, load_settings

KEY = "tvly-test-dummy-key-not-real"


@pytest.fixture
def web_env(valid_env: dict[str, str]) -> dict[str, str]:
    return {**valid_env, "WEB_SEARCH_PROVIDER": "Tavily", "TAVILY_API_KEY": KEY}


def test_web_search_is_disabled_by_default(valid_env: dict[str, str]) -> None:
    assert load_settings(valid_env).web is None
    assert load_settings({**valid_env, "WEB_SEARCH_PROVIDER": " "}).web is None


def test_web_settings_are_parsed(web_env: dict[str, str]) -> None:
    settings = load_settings(
        {
            **web_env,
            "WEB_TIMEOUT_SECONDS": "30",
            "WEB_MAX_RESULTS": "3",
            "WEB_MAX_CHARS": "2000",
            "WEB_MAX_CALLS_PER_DAY": "0",
        }
    )

    assert settings.web is not None
    assert settings.web.provider == "tavily"
    assert settings.web.api_key == KEY
    assert settings.web.timeout_seconds == 30.0
    assert settings.web.max_results == 3
    assert settings.web.max_chars == 2000
    assert settings.web.max_calls_per_day == 0


def test_web_defaults(web_env: dict[str, str]) -> None:
    web = load_settings(web_env).web

    assert web is not None
    assert (web.max_results, web.max_chars, web.max_calls_per_day) == (5, 4000, 30)


def test_tavily_key_is_secret_and_not_in_repr(web_env: dict[str, str]) -> None:
    settings = load_settings(web_env)

    assert KEY in settings.secrets
    assert KEY not in repr(settings)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"WEB_SEARCH_PROVIDER": "google"}, "WEB_SEARCH_PROVIDER"),
        ({"TAVILY_API_KEY": ""}, "TAVILY_API_KEY"),
        ({"TAVILY_API_KEY": "abc def"}, "TAVILY_API_KEY"),
        ({"WEB_MAX_RESULTS": "50"}, "WEB_MAX_RESULTS"),
        ({"WEB_MAX_CHARS": "10"}, "WEB_MAX_CHARS"),
        ({"WEB_MAX_CALLS_PER_DAY": "-1"}, "WEB_MAX_CALLS_PER_DAY"),
        ({"WEB_TIMEOUT_SECONDS": "0"}, "WEB_TIMEOUT_SECONDS"),
    ],
)
def test_invalid_web_settings_are_reported(web_env: dict[str, str], override: dict[str, str], fragment: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_settings({**web_env, **override})

    assert fragment in str(excinfo.value)
    assert KEY not in str(excinfo.value)
