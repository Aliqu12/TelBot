from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant_bot.llm import LLMRateLimitError
from assistant_bot.telegram_bot.access import AccessPolicy
from assistant_bot.telegram_bot.handlers import SETUP_HINT, BotHandlers

OWNER_ID = 1001
STRANGER_ID = 2002


# --- AccessPolicy -------------------------------------------------------------


def test_policy_with_owner_only_authorizes_owner() -> None:
    policy = AccessPolicy(OWNER_ID)

    assert policy.is_configured
    assert policy.is_authorized(OWNER_ID)
    assert not policy.is_authorized(STRANGER_ID)
    assert not policy.is_authorized(None)
    assert policy.may_use_whoami(OWNER_ID)
    assert not policy.may_use_whoami(STRANGER_ID)


def test_policy_in_setup_mode_authorizes_nobody_but_allows_whoami() -> None:
    policy = AccessPolicy(None)

    assert not policy.is_configured
    assert not policy.is_authorized(OWNER_ID)
    assert policy.may_use_whoami(STRANGER_ID)


# --- Handler ------------------------------------------------------------------


def make_update(user_id: int, text: str = "Hallo") -> SimpleNamespace:
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )


def make_context() -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


def make_handlers(allowed_user_id: int | None = OWNER_ID, max_input_chars: int = 100) -> tuple[BotHandlers, MagicMock]:
    assistant = MagicMock()
    assistant.reply = AsyncMock(return_value="KI-Antwort")
    assistant.reset = AsyncMock()
    handlers = BotHandlers(
        assistant=assistant,
        access=AccessPolicy(allowed_user_id),
        owner_name="Ali",
        max_input_chars=max_input_chars,
    )
    return handlers, assistant


def replies(update: SimpleNamespace) -> list[str]:
    return [call.args[0] for call in update.effective_message.reply_text.await_args_list]


def test_authorized_user_gets_ai_reply() -> None:
    handlers, assistant = make_handlers()
    update = make_update(OWNER_ID, "Wie spät ist es?")

    asyncio.run(handlers.text_message(update, make_context()))

    assistant.reply.assert_awaited_once_with(OWNER_ID, "Wie spät ist es?")
    assert replies(update) == ["KI-Antwort"]


def test_stranger_gets_no_reply_and_causes_no_llm_request() -> None:
    handlers, assistant = make_handlers()
    update = make_update(STRANGER_ID)

    asyncio.run(handlers.text_message(update, make_context()))

    assistant.reply.assert_not_awaited()
    assert replies(update) == []


def test_setup_mode_answers_with_hint_and_causes_no_llm_request() -> None:
    handlers, assistant = make_handlers(allowed_user_id=None)
    update = make_update(OWNER_ID)

    asyncio.run(handlers.text_message(update, make_context()))

    assistant.reply.assert_not_awaited()
    assert replies(update) == [SETUP_HINT]


def test_whoami_in_setup_mode_returns_user_id() -> None:
    handlers, _ = make_handlers(allowed_user_id=None)
    update = make_update(STRANGER_ID, "/whoami")

    asyncio.run(handlers.whoami_command(update, make_context()))

    assert str(STRANGER_ID) in replies(update)[0]


def test_whoami_is_ignored_for_strangers_after_setup() -> None:
    handlers, _ = make_handlers()
    update = make_update(STRANGER_ID, "/whoami")

    asyncio.run(handlers.whoami_command(update, make_context()))

    assert replies(update) == []


@pytest.mark.parametrize("command", ["start_command", "help_command", "reset_command", "unknown_command"])
def test_commands_are_ignored_for_strangers(command: str) -> None:
    handlers, assistant = make_handlers()
    update = make_update(STRANGER_ID, "/start")

    asyncio.run(getattr(handlers, command)(update, make_context()))

    assert replies(update) == []
    assistant.reset.assert_not_awaited()


def test_too_long_input_is_rejected_without_llm_request() -> None:
    handlers, assistant = make_handlers(max_input_chars=10)
    update = make_update(OWNER_ID, "x" * 11)

    asyncio.run(handlers.text_message(update, make_context()))

    assistant.reply.assert_not_awaited()
    assert "zu lang" in replies(update)[0]


def test_llm_error_is_translated_into_friendly_message() -> None:
    handlers, assistant = make_handlers()
    assistant.reply.side_effect = LLMRateLimitError()
    update = make_update(OWNER_ID)

    asyncio.run(handlers.text_message(update, make_context()))

    assert "Anfragelimit" in replies(update)[0]


def test_reset_command_clears_history_for_owner() -> None:
    handlers, assistant = make_handlers()
    update = make_update(OWNER_ID, "/reset")

    asyncio.run(handlers.reset_command(update, make_context()))

    assistant.reset.assert_awaited_once_with(OWNER_ID)
    assert "gelöscht" in replies(update)[0]


def test_long_answers_are_split_into_multiple_messages() -> None:
    handlers, assistant = make_handlers()
    assistant.reply.return_value = ("Absatz " * 400 + "\n\n") * 3
    update = make_update(OWNER_ID)

    asyncio.run(handlers.text_message(update, make_context()))

    sent = replies(update)
    assert len(sent) > 1
    assert all(len(chunk) <= 4000 for chunk in sent)
