"""Tests für die E-Mail-Konfiguration aus Umgebungsvariablen."""

from __future__ import annotations

import pytest

from assistant_bot.config import ConfigError, load_settings

APP_PASSWORD = "abcd efgh ijkl mnop"


@pytest.fixture
def email_env(valid_env: dict[str, str]) -> dict[str, str]:
    return {
        **valid_env,
        "EMAIL_PROVIDER": "Yahoo",
        "EMAIL_ADDRESS": "Ich@Yahoo.de",
        "EMAIL_APP_PASSWORD": APP_PASSWORD,
    }


def test_email_is_disabled_by_default(valid_env: dict[str, str]) -> None:
    assert load_settings(valid_env).email is None
    assert load_settings({**valid_env, "EMAIL_PROVIDER": "  "}).email is None


def test_yahoo_settings_are_parsed(email_env: dict[str, str]) -> None:
    settings = load_settings(
        {**email_env, "OWNER_NAME": "Ali", "EMAIL_ALLOWED_RECIPIENTS": "Chef@Firma.de, @Example.ORG ,"}
    )
    email = settings.email

    assert email is not None
    assert email.provider == "yahoo"
    assert email.address == "Ich@yahoo.de"
    assert email.app_password == "abcdefghijklmnop"
    assert email.sender_name == "Ali"
    assert email.allowed_recipients == ("chef@firma.de", "@example.org")
    assert (email.draft_ttl_seconds, email.timeout_seconds, email.max_body_chars) == (900, 30.0, 6000)


def test_app_password_is_secret_and_not_in_repr(email_env: dict[str, str]) -> None:
    settings = load_settings(email_env)

    assert "abcdefghijklmnop" in settings.secrets
    assert "abcdefghijklmnop" not in repr(settings)


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"EMAIL_PROVIDER": "gmail"}, "EMAIL_PROVIDER"),
        ({"EMAIL_ADDRESS": ""}, "EMAIL_ADDRESS"),
        ({"EMAIL_ADDRESS": "kein-at"}, "EMAIL_ADDRESS"),
        ({"EMAIL_APP_PASSWORD": ""}, "EMAIL_APP_PASSWORD"),
        ({"EMAIL_ALLOWED_RECIPIENTS": "ok@example.de,kaputt"}, "EMAIL_ALLOWED_RECIPIENTS"),
        ({"EMAIL_DRAFT_TTL_SECONDS": "5"}, "EMAIL_DRAFT_TTL_SECONDS"),
        ({"EMAIL_TIMEOUT_SECONDS": "abc"}, "EMAIL_TIMEOUT_SECONDS"),
    ],
)
def test_invalid_email_settings_fail_fast(email_env: dict[str, str], override: dict[str, str], fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment) as exc_info:
        load_settings({**email_env, **override})
    assert "abcdefghijklmnop" not in str(exc_info.value)
