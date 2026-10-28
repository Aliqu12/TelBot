"""Tests für die Telegram-Freigabe von E-Mail-Entwürfen (Schaltflächen)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram import InlineKeyboardMarkup

from assistant_bot.mail.models import EmailTimeoutError
from assistant_bot.mail.service import EmailService
from assistant_bot.telegram_bot.access import AccessPolicy
from assistant_bot.telegram_bot.email_confirmation import EmailConfirmationHandlers
from assistant_bot.telegram_bot.handlers import BotHandlers
from tests.test_email_service import Clock, FakeProvider

OWNER = 111
STRANGER = 666


def setup(ttl: int = 900) -> tuple[EmailConfirmationHandlers, EmailService, FakeProvider, Clock]:
    provider = FakeProvider()
    clock = Clock()
    service = EmailService(provider, draft_ttl_seconds=ttl, clock=clock)
    return EmailConfirmationHandlers(email=service, access=AccessPolicy(OWNER)), service, provider, clock


def make_context() -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock()))


def callback_update(data: str, user_id: int = OWNER, chat_type: str = "private") -> SimpleNamespace:
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
    )


def new_draft(service: EmailService) -> object:
    return service.create_draft(OWNER, to=["max@example.de"], subject="Grüße", body="Hallo Max")


def sent_texts(context: SimpleNamespace) -> list[str]:
    return [c.kwargs["text"] for c in context.bot.send_message.await_args_list]


def test_pending_draft_is_presented_with_buttons() -> None:
    handlers, service, _, _ = setup()
    created = new_draft(service)
    context = make_context()

    asyncio.run(handlers.present_pending_draft(context.bot, OWNER))
    asyncio.run(handlers.present_pending_draft(context.bot, OWNER))  # nur einmal anzeigen

    context.bot.send_message.assert_awaited_once()
    kwargs = context.bot.send_message.await_args.kwargs
    assert "An: max@example.de" in kwargs["text"] and "Betreff: Grüße" in kwargs["text"]
    assert "Hallo Max" in kwargs["text"] and "15 Minuten" in kwargs["text"]
    markup: InlineKeyboardMarkup = kwargs["reply_markup"]
    buttons = markup.inline_keyboard[0]
    assert buttons[0].callback_data == f"email:send:{created.draft_id}:{created.revision}"
    assert buttons[1].callback_data == f"email:discard:{created.draft_id}:{created.revision}"
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)


def test_authorized_click_sends_once_even_if_repeated() -> None:
    handlers, service, provider, _ = setup()
    created = new_draft(service)
    service.take_draft_for_review(OWNER)
    context = make_context()
    update = callback_update(f"email:send:{created.draft_id}:{created.revision}")

    asyncio.run(handlers.callback(update, context))  # type: ignore[arg-type]
    asyncio.run(handlers.callback(update, context))  # doppelter Klick / doppeltes Update

    assert len(provider.sent) == 1
    texts = sent_texts(context)
    assert texts[0] == "Die E-Mail an max@example.de wurde versendet."
    assert "bereits versendet" in texts[1]
    update.callback_query.edit_message_reply_markup.assert_awaited()


def test_unauthorized_click_does_not_send() -> None:
    handlers, service, provider, _ = setup()
    created = new_draft(service)
    context = make_context()
    update = callback_update(f"email:send:{created.draft_id}:{created.revision}", user_id=STRANGER)

    asyncio.run(handlers.callback(update, context))  # type: ignore[arg-type]

    assert provider.sent == []
    context.bot.send_message.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()


def test_click_in_group_or_with_garbage_data_is_ignored() -> None:
    handlers, service, provider, _ = setup()
    created = new_draft(service)
    context = make_context()

    for update in (
        callback_update(f"email:send:{created.draft_id}:{created.revision}", chat_type="group"),
        callback_update("email:send:xyz;rm:1"),
        callback_update("email:send"),
    ):
        asyncio.run(handlers.callback(update, context))  # type: ignore[arg-type]

    assert provider.sent == []
    context.bot.send_message.assert_not_awaited()


def test_outdated_button_after_change_does_not_send() -> None:
    handlers, service, provider, _ = setup()
    created = new_draft(service)
    service.update_draft(OWNER, body="Geändert")
    service.take_draft_for_review(OWNER)
    context = make_context()

    asyncio.run(handlers.callback(callback_update(f"email:send:{created.draft_id}:{created.revision}"), context))  # type: ignore[arg-type]

    assert provider.sent == []
    assert "geändert" in sent_texts(context)[0]


def test_expired_button_shows_draft_again_for_new_confirmation() -> None:
    handlers, service, provider, clock = setup(ttl=60)
    created = new_draft(service)
    service.take_draft_for_review(OWNER)
    clock.now += 120
    context = make_context()

    asyncio.run(handlers.callback(callback_update(f"email:send:{created.draft_id}:{created.revision}"), context))  # type: ignore[arg-type]

    assert provider.sent == []
    texts = sent_texts(context)
    assert "abgelaufen" in texts[0]
    markup = context.bot.send_message.await_args_list[1].kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"email:send:{created.draft_id}:{created.revision + 1}"


def test_timeout_warns_about_possible_delivery_and_requires_new_confirmation() -> None:
    handlers, service, provider, _ = setup()
    created = new_draft(service)
    service.take_draft_for_review(OWNER)
    provider.send_error = EmailTimeoutError("timeout")
    context = make_context()

    asyncio.run(handlers.callback(callback_update(f"email:send:{created.draft_id}:{created.revision}"), context))  # type: ignore[arg-type]

    texts = sent_texts(context)
    assert "Gesendet" in texts[0]
    assert len(texts) == 2  # Entwurf mit neuer Revision erneut angezeigt


def test_discard_button() -> None:
    handlers, service, provider, _ = setup()
    created = new_draft(service)
    context = make_context()

    asyncio.run(handlers.callback(callback_update(f"email:discard:{created.draft_id}:{created.revision}"), context))  # type: ignore[arg-type]
    asyncio.run(handlers.callback(callback_update(f"email:send:{created.draft_id}:{created.revision}"), context))  # type: ignore[arg-type]

    assert service.current_draft(OWNER) is None
    assert provider.sent == []
    assert sent_texts(context)[0] == "Der Entwurf wurde verworfen."


def test_text_handler_presents_draft_created_by_assistant() -> None:
    handlers, service, _, _ = setup()

    async def reply(chat_id: int, text: str) -> str:
        new_draft(service)
        return "Ich habe einen Entwurf vorbereitet."

    assistant = MagicMock()
    assistant.reply = AsyncMock(side_effect=reply)
    bot_handlers = BotHandlers(
        assistant=assistant,
        access=AccessPolicy(OWNER),
        owner_name="Ali",
        max_input_chars=4000,
        email_confirmation=handlers,
    )
    message = SimpleNamespace(text="Schreib Max eine Mail", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(id=OWNER),
    )
    context = make_context()

    asyncio.run(bot_handlers.text_message(update, context))  # type: ignore[arg-type]

    message.reply_text.assert_awaited_once_with("Ich habe einen Entwurf vorbereitet.")
    assert context.bot.send_message.await_args.kwargs["reply_markup"] is not None


def test_unauthorized_text_does_not_touch_email() -> None:
    handlers, service, _, _ = setup()
    assistant = MagicMock()
    assistant.reply = AsyncMock()
    bot_handlers = BotHandlers(
        assistant=assistant,
        access=AccessPolicy(OWNER),
        owner_name="Ali",
        max_input_chars=4000,
        email_confirmation=handlers,
    )
    message = SimpleNamespace(text="Zeig meine Mails", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=STRANGER),
        effective_chat=SimpleNamespace(id=STRANGER),
    )
    context = make_context()

    asyncio.run(bot_handlers.text_message(update, context))  # type: ignore[arg-type]

    assistant.reply.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
