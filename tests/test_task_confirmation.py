"""Tests für die Telegram-Bestätigung beim Löschen von Aufgaben (Schaltflächen)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram import InlineKeyboardMarkup

from assistant_bot.tasks.models import TaskTimeoutError
from assistant_bot.tasks.service import TaskService
from assistant_bot.telegram_bot.access import AccessPolicy
from assistant_bot.telegram_bot.handlers import BotHandlers
from assistant_bot.telegram_bot.task_confirmation import TaskConfirmationHandlers
from tests.test_email_confirmation import callback_update, make_context, sent_texts
from tests.test_task_service import BERLIN, NOW, Clock, FakeTaskProvider, default_tasks

OWNER = 111
STRANGER = 666


def setup() -> tuple[TaskConfirmationHandlers, TaskService, FakeTaskProvider, Clock]:
    provider = FakeTaskProvider(default_tasks())
    clock = Clock()
    service = TaskService(provider, timezone=BERLIN, clock=clock, now=lambda: NOW)
    return TaskConfirmationHandlers(tasks=service, access=AccessPolicy(OWNER)), service, provider, clock


def request(service: TaskService, query: str = "Waschmaschine") -> str:
    return asyncio.run(service.request_deletion(OWNER, query=query)).deletion_id


def test_pending_deletion_is_presented_with_buttons_once() -> None:
    handlers, service, _, _ = setup()
    deletion_id = request(service)
    context = make_context()

    asyncio.run(handlers.present_pending_deletion(context.bot, OWNER))
    asyncio.run(handlers.present_pending_deletion(context.bot, OWNER))

    context.bot.send_message.assert_awaited_once()
    kwargs = context.bot.send_message.await_args.kwargs
    assert "Waschmaschine reparieren" in kwargs["text"] and "Fr, 30.10.2026 16:00" in kwargs["text"]
    assert "5 Minuten" in kwargs["text"]
    markup: InlineKeyboardMarkup = kwargs["reply_markup"]
    buttons = markup.inline_keyboard[0]
    assert [b.text for b in buttons] == ["Löschen", "Behalten"]
    assert buttons[0].callback_data == f"task:del:{deletion_id}"
    assert buttons[1].callback_data == f"task:keep:{deletion_id}"
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_confirmed_deletion_happens_once_even_on_double_click() -> None:
    handlers, service, provider, _ = setup()
    deletion_id = request(service)
    context = make_context()
    update = callback_update(f"task:del:{deletion_id}", user_id=OWNER)

    asyncio.run(handlers.callback(update, context))  # type: ignore[arg-type]
    asyncio.run(handlers.callback(update, context))  # type: ignore[arg-type]

    assert provider.deleted == ["t-fri"]
    texts = sent_texts(context)
    assert texts[0] == "Die Aufgabe „Waschmaschine reparieren“ wurde gelöscht."
    assert "bereits gelöscht" in texts[1]
    update.callback_query.edit_message_reply_markup.assert_awaited()


def test_stranger_group_and_garbage_cannot_delete() -> None:
    handlers, service, provider, _ = setup()
    deletion_id = request(service)
    context = make_context()

    for update in (
        callback_update(f"task:del:{deletion_id}", user_id=STRANGER),
        callback_update(f"task:del:{deletion_id}", user_id=OWNER, chat_type="group"),
        callback_update("task:del:xyz;drop", user_id=OWNER),
        callback_update("task:del", user_id=OWNER),
    ):
        asyncio.run(handlers.callback(update, context))  # type: ignore[arg-type]

    assert provider.deleted == []
    context.bot.send_message.assert_not_awaited()


def test_keep_button_cancels() -> None:
    handlers, service, provider, _ = setup()
    deletion_id = request(service)
    context = make_context()

    asyncio.run(handlers.callback(callback_update(f"task:keep:{deletion_id}", user_id=OWNER), context))  # type: ignore[arg-type]
    asyncio.run(handlers.callback(callback_update(f"task:del:{deletion_id}", user_id=OWNER), context))  # type: ignore[arg-type]

    assert provider.deleted == []
    texts = sent_texts(context)
    assert texts[0] == "Alles klar, die Aufgabe bleibt bestehen."
    assert "nicht mehr gültig" in texts[1]


def test_expired_and_failed_deletions() -> None:
    handlers, service, provider, clock = setup()
    context = make_context()

    expired_id = request(service)
    clock.now += 301
    asyncio.run(handlers.callback(callback_update(f"task:del:{expired_id}", user_id=OWNER), context))  # type: ignore[arg-type]

    timeout_id = request(service)

    async def slow_delete(task_id: str) -> None:
        raise TaskTimeoutError("timeout")

    provider.delete_task = slow_delete  # type: ignore[method-assign]
    asyncio.run(handlers.callback(callback_update(f"task:del:{timeout_id}", user_id=OWNER), context))  # type: ignore[arg-type]

    texts = sent_texts(context)
    assert "abgelaufen" in texts[0]
    assert "möglicherweise trotzdem gelöscht" in texts[1]
    assert provider.deleted == []


def test_text_handler_presents_deletion_prepared_by_assistant() -> None:
    handlers, service, _, _ = setup()

    async def reply(chat_id: int, text: str) -> str:
        await service.request_deletion(chat_id, query="Urlaub")
        return "Bitte bestätige das Löschen."

    assistant = MagicMock()
    assistant.reply = AsyncMock(side_effect=reply)
    bot_handlers = BotHandlers(
        assistant=assistant,
        access=AccessPolicy(OWNER),
        owner_name="Ali",
        max_input_chars=4000,
        task_confirmation=handlers,
    )
    message = SimpleNamespace(text="Lösche die Aufgabe Urlaub planen", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(id=OWNER),
    )
    context = make_context()

    asyncio.run(bot_handlers.text_message(update, context))  # type: ignore[arg-type]

    message.reply_text.assert_awaited_once_with("Bitte bestätige das Löschen.")
    kwargs = context.bot.send_message.await_args.kwargs
    assert "Urlaub planen" in kwargs["text"] and kwargs["reply_markup"] is not None
