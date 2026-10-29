"""Tests für das Limit der KI-Statusmeldungen. Keine echten Telegram- oder Groq-Anfragen."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_bot.config import ConfigError, load_settings
from assistant_bot.status_limiter import StatusRateLimiter
from assistant_bot.telegram_bot.application import SHUTDOWN_FALLBACK, STARTUP_FALLBACK, build_application, notify_owner

OWNER = 1001


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def acquire(limiter: StatusRateLimiter) -> bool:
    return asyncio.run(limiter.try_acquire())


# --- Zähler -----------------------------------------------------------------------


def test_only_two_requests_per_minute_are_allowed(tmp_path: Path) -> None:
    clock = Clock()
    limiter = StatusRateLimiter(tmp_path / "t.db", 2, clock=clock)

    assert [acquire(limiter) for _ in range(4)] == [True, True, False, False]


def test_window_slides_after_one_minute(tmp_path: Path) -> None:
    clock = Clock()
    limiter = StatusRateLimiter(tmp_path / "t.db", 2, clock=clock)
    assert acquire(limiter) and acquire(limiter)

    clock.now += 59
    assert not acquire(limiter)
    clock.now += 2  # 61 s nach den ersten beiden
    assert acquire(limiter)


def test_limit_survives_restarts(tmp_path: Path) -> None:
    clock = Clock()
    database = tmp_path / "t.db"
    assert acquire(StatusRateLimiter(database, 2, clock=clock))
    assert acquire(StatusRateLimiter(database, 2, clock=clock))

    # Neuer Prozess = neues Objekt, der Zähler liegt in der Datenbank.
    assert not acquire(StatusRateLimiter(database, 2, clock=clock))


def test_zero_means_never_ask_the_model(tmp_path: Path) -> None:
    limiter = StatusRateLimiter(tmp_path / "t.db", 0, clock=Clock())

    assert not acquire(limiter)
    assert not (tmp_path / "t.db").exists()


def test_entries_from_the_future_do_not_block_forever(tmp_path: Path) -> None:
    clock = Clock(10_000.0)
    limiter = StatusRateLimiter(tmp_path / "t.db", 1, clock=clock)
    assert acquire(limiter)

    clock.now = 100.0  # Uhr wurde zurückgestellt
    assert acquire(limiter)


def test_database_errors_mean_fallback(tmp_path: Path) -> None:
    limiter = StatusRateLimiter(tmp_path / "missing-dir" / "t.db", 2, clock=Clock())

    assert not acquire(limiter)


# --- Zusammenspiel mit den Statusmeldungen -------------------------------------------


def make_bot() -> SimpleNamespace:
    return SimpleNamespace(send_message=AsyncMock(), set_my_commands=AsyncMock(), username="test_bot")


def test_third_message_in_a_minute_uses_fallback_without_asking_the_model(tmp_path: Path) -> None:
    limiter = StatusRateLimiter(tmp_path / "t.db", 2, clock=Clock())
    assistant = SimpleNamespace(announce=AsyncMock(return_value="Hallo vom Modell"))
    bot = make_bot()

    for _ in range(3):
        asyncio.run(notify_owner(bot, assistant, OWNER, "Anweisung", "Fester Text", 1.0, limiter))  # type: ignore[arg-type]

    assert assistant.announce.await_count == 2
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == ["Hallo vom Modell", "Hallo vom Modell", "Fester Text"]


def test_failed_model_request_still_counts(tmp_path: Path) -> None:
    from assistant_bot.llm import LLMRateLimitError

    limiter = StatusRateLimiter(tmp_path / "t.db", 1, clock=Clock())
    assistant = SimpleNamespace(announce=AsyncMock(side_effect=LLMRateLimitError()))
    bot = make_bot()

    for _ in range(2):
        asyncio.run(notify_owner(bot, assistant, OWNER, "Anweisung", "Fester Text", 1.0, limiter))  # type: ignore[arg-type]

    assert assistant.announce.await_count == 1
    assert bot.send_message.await_count == 2


def test_application_hooks_share_the_limit(tmp_path: Path, valid_env: dict[str, str]) -> None:
    settings = load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": str(OWNER), "OWNER_NAME": "Ali"})
    limiter = StatusRateLimiter(tmp_path / "t.db", 2, clock=Clock())
    assistant = SimpleNamespace(announce=AsyncMock(return_value="KI-Text"))
    application = build_application(settings, assistant, status_limiter=limiter)  # type: ignore[arg-type]
    bot = make_bot()
    context = SimpleNamespace(bot=bot)

    # Zwei Neustarts hintereinander: Start, Stopp, Start, Stopp.
    for _ in range(2):
        asyncio.run(application.post_init(context))  # type: ignore[arg-type,misc]
        asyncio.run(application.post_stop(context))  # type: ignore[arg-type,misc]

    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == ["KI-Text", "KI-Text", STARTUP_FALLBACK.format(owner="Ali"), SHUTDOWN_FALLBACK]
    assert assistant.announce.await_count == 2


# --- Schalter und Konfiguration -------------------------------------------------------


def test_switch_disables_all_status_messages(valid_env: dict[str, str]) -> None:
    settings = load_settings(
        {**valid_env, "ALLOWED_TELEGRAM_USER_ID": str(OWNER), "STATUS_MESSAGES_ENABLED": "false"}
    )
    assistant = SimpleNamespace(announce=AsyncMock(return_value="KI-Text"))
    application = build_application(settings, assistant)  # type: ignore[arg-type]
    bot = make_bot()
    context = SimpleNamespace(bot=bot)

    asyncio.run(application.post_init(context))  # type: ignore[arg-type,misc]
    asyncio.run(application.post_stop(context))  # type: ignore[arg-type,misc]

    assistant.announce.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.set_my_commands.assert_awaited_once()  # der Rest des Starts läuft normal


def test_status_settings_defaults_and_validation(valid_env: dict[str, str]) -> None:
    defaults = load_settings(valid_env)
    assert defaults.status_messages_enabled is True
    assert defaults.status_llm_max_per_minute == 2

    custom = load_settings({**valid_env, "STATUS_MESSAGES_ENABLED": "NEIN", "STATUS_LLM_MAX_PER_MINUTE": "0"})
    assert custom.status_messages_enabled is False
    assert custom.status_llm_max_per_minute == 0

    with pytest.raises(ConfigError, match="STATUS_MESSAGES_ENABLED"):
        load_settings({**valid_env, "STATUS_MESSAGES_ENABLED": "vielleicht"})
    with pytest.raises(ConfigError, match="STATUS_LLM_MAX_PER_MINUTE"):
        load_settings({**valid_env, "STATUS_LLM_MAX_PER_MINUTE": "-1"})
