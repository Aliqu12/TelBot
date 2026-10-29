"""Tests für die Start-/Stopp-Meldungen an den Besitzer. Keine echten Telegram- oder Groq-Anfragen."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, NetworkError

from assistant_bot.assistant import Assistant
from assistant_bot.config import load_settings
from assistant_bot.conversation import ConversationService
from assistant_bot.llm import LLMRateLimitError
from assistant_bot.prompts import SHUTDOWN_INSTRUCTION, STARTUP_INSTRUCTION
from assistant_bot.telegram_bot.application import (
    MAX_STATUS_CHARS,
    SHUTDOWN_FALLBACK,
    STARTUP_FALLBACK,
    build_application,
    notify_owner,
)
from tests.test_images import RecordingModel

OWNER = 1001


def make_bot(error: Exception | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        send_message=AsyncMock(side_effect=error), set_my_commands=AsyncMock(), username="test_bot"
    )


def make_assistant(answer: Any = "Hallo!", error: Exception | None = None) -> SimpleNamespace:
    return SimpleNamespace(announce=AsyncMock(return_value=answer, side_effect=error))


def send(bot: SimpleNamespace, assistant: Any, *, owner_id: int | None = OWNER, timeout: float = 1) -> None:
    asyncio.run(notify_owner(bot, assistant, owner_id, "Anweisung", "Fester Text", timeout))  # type: ignore[arg-type]


def test_model_text_is_sent_to_owner() -> None:
    bot, assistant = make_bot(), make_assistant("  Guten Morgen Ali, ich bin startklar!  ")

    send(bot, assistant)

    assistant.announce.assert_awaited_once_with("Anweisung")
    bot.send_message.assert_awaited_once_with(chat_id=OWNER, text="Guten Morgen Ali, ich bin startklar!")


@pytest.mark.parametrize(
    "assistant",
    [
        make_assistant(error=LLMRateLimitError()),
        make_assistant(answer=""),
        make_assistant(answer="x" * (MAX_STATUS_CHARS + 1)),
    ],
    ids=["llm-error", "empty", "too-long"],
)
def test_fallback_text_is_used_when_model_fails_or_answer_is_unusable(assistant: SimpleNamespace) -> None:
    bot = make_bot()

    send(bot, assistant)

    bot.send_message.assert_awaited_once_with(chat_id=OWNER, text="Fester Text")


def test_fallback_text_is_used_on_timeout() -> None:
    async def slow(_: str) -> str:
        await asyncio.sleep(5)
        return "zu spät"

    bot, assistant = make_bot(), SimpleNamespace(announce=slow)

    send(bot, assistant, timeout=0.05)

    bot.send_message.assert_awaited_once_with(chat_id=OWNER, text="Fester Text")


def test_nothing_is_sent_without_configured_owner() -> None:
    bot, assistant = make_bot(), make_assistant()

    send(bot, assistant, owner_id=None)

    assistant.announce.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_telegram_errors_are_swallowed() -> None:
    for error in (Forbidden("chat not found"), NetworkError("down")):
        send(make_bot(error), make_assistant())


def test_announce_uses_persona_without_history_tools_or_storage(tmp_path: Path) -> None:
    conversations = ConversationService(tmp_path / "test.db", 20)
    conversations.initialize()
    asyncio.run(conversations.add_exchange(OWNER, "Frage", "Antwort"))
    model = RecordingModel("Servus Ali, bin startklar.")
    assistant = Assistant(model, conversations, "SYSTEM")  # type: ignore[arg-type]

    answer = asyncio.run(assistant.announce("Begrüße Ali"))

    assert answer == "Servus Ali, bin startklar."
    (messages,) = model.calls
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[0].content.startswith("SYSTEM") and "Heutiges Datum" in messages[0].content
    assert messages[1].content == "Begrüße Ali"
    assert len(asyncio.run(conversations.get_history(OWNER))) == 2  # unverändert


def test_instructions_name_the_owner_and_describe_the_event() -> None:
    start = STARTUP_INSTRUCTION.format(owner_name="Ali")
    stop = SHUTDOWN_INSTRUCTION.format(owner_name="Ali")

    assert "Ali" in start and "startklar" in start
    assert "Ali" in stop and "kurz" in stop and "erfinde keine" in stop.lower()


def test_application_hooks_send_model_generated_messages(valid_env: dict[str, str]) -> None:
    settings = load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": str(OWNER), "OWNER_NAME": "Ali"})
    assistant = SimpleNamespace(announce=AsyncMock(side_effect=["Hallo Ali!", "Bis gleich, Ali!"]))
    application = build_application(settings, assistant)  # type: ignore[arg-type]
    bot = make_bot()
    context = SimpleNamespace(bot=bot)

    assert application.post_init is not None and application.post_stop is not None
    asyncio.run(application.post_init(context))  # type: ignore[arg-type]
    asyncio.run(application.post_stop(context))  # type: ignore[arg-type]

    instructions = [call.args[0] for call in assistant.announce.await_args_list]
    assert instructions == [STARTUP_INSTRUCTION.format(owner_name="Ali"), SHUTDOWN_INSTRUCTION.format(owner_name="Ali")]
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == ["Hallo Ali!", "Bis gleich, Ali!"]


def test_application_hooks_use_fallbacks_when_model_is_down(valid_env: dict[str, str]) -> None:
    settings = load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": str(OWNER), "OWNER_NAME": "Ali"})
    application = build_application(settings, make_assistant(error=LLMRateLimitError()))  # type: ignore[arg-type]
    bot = make_bot()
    context = SimpleNamespace(bot=bot)

    asyncio.run(application.post_init(context))  # type: ignore[arg-type,misc]
    asyncio.run(application.post_stop(context))  # type: ignore[arg-type,misc]

    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert texts == [STARTUP_FALLBACK.format(owner="Ali"), SHUTDOWN_FALLBACK]


def test_no_status_messages_in_setup_mode(valid_env: dict[str, str]) -> None:
    settings = load_settings({**valid_env, "ALLOWED_TELEGRAM_USER_ID": ""})
    assistant = make_assistant()
    application = build_application(settings, assistant)  # type: ignore[arg-type]
    bot = make_bot()
    context = SimpleNamespace(bot=bot)

    asyncio.run(application.post_init(context))  # type: ignore[arg-type,misc]
    asyncio.run(application.post_stop(context))  # type: ignore[arg-type,misc]

    assistant.announce.assert_not_awaited()
    bot.send_message.assert_not_awaited()
