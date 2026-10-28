"""Telegram-Freigabe für E-Mail-Entwürfe.

Versendet wird ausschließlich hier – nach einem Klick des berechtigten Nutzers
auf "Senden". Die Schaltfläche enthält Entwurfs-ID und Revision; der
``EmailService`` prüft Aktualität, Ablaufzeit und verhindert Mehrfachversand.
"""

from __future__ import annotations

import contextlib
import logging
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..mail.models import EmailError, EmailTimeoutError
from ..mail.service import DraftConfirmationError, DraftExpiredError, EmailDraft, EmailService
from .access import AccessPolicy
from .message_split import split_message

logger = logging.getLogger(__name__)

CALLBACK_PATTERN = r"^email:"
_CALLBACK_RE = re.compile(r"^email:(send|discard):([0-9a-f]{1,32}):(\d{1,6})$")


class EmailConfirmationHandlers:
    def __init__(self, *, email: EmailService, access: AccessPolicy) -> None:
        self._email = email
        self._access = access

    async def present_pending_draft(self, bot: Bot, chat_id: int) -> None:
        """Zeigt einen neu erstellten/geänderten Entwurf mit Freigabe-Schaltflächen an."""
        draft = self._email.take_draft_for_review(chat_id)
        if draft is None:
            return
        chunks = split_message(format_draft(draft))
        for index, chunk in enumerate(chunks):
            markup = draft_keyboard(draft) if index == len(chunks) - 1 else None
            await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=markup)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        if query is None:
            return
        if user is None or not self._access.is_authorized(user.id):
            logger.warning("Zugriff verweigert: user_id=%s, Aktion=E-Mail-Schaltfläche", user.id if user else None)
            with contextlib.suppress(TelegramError):
                await query.answer()
            return

        with contextlib.suppress(TelegramError):
            await query.answer()
        match = _CALLBACK_RE.match(query.data or "")
        if chat is None or chat.type != ChatType.PRIVATE or match is None:
            return
        action, draft_id, revision = match.group(1), match.group(2), int(match.group(3))

        # Schaltflächen sofort entfernen, damit nicht mehrfach getippt wird.
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)

        if action == "discard":
            discarded = self._email.discard_draft(chat.id, draft_id)
            text = "Der Entwurf wurde verworfen." if discarded else "Dieser Entwurf ist nicht mehr offen."
            await context.bot.send_message(chat_id=chat.id, text=text)
            return

        try:
            draft = await self._email.confirm_and_send(chat.id, draft_id, revision)
        except DraftExpiredError:
            self._email.request_review(chat.id)
            text = "Die Freigabe ist abgelaufen. Bitte prüfe den Entwurf und tippe erneut auf \"Senden\"."
        except DraftConfirmationError as exc:
            text = str(exc)
        except EmailTimeoutError:
            text = (
                "Der E-Mail-Server hat nicht rechtzeitig geantwortet. Die E-Mail wurde möglicherweise "
                "trotzdem versendet. Bitte prüfe den Ordner \"Gesendet\", bevor du erneut sendest."
            )
        except EmailError as exc:
            text = f"Der Versand ist fehlgeschlagen: {exc}"
        else:
            recipients = ", ".join(a.address for a in draft.email.recipients)
            text = f"Die E-Mail an {recipients} wurde versendet."
        await context.bot.send_message(chat_id=chat.id, text=text)
        # Nach Fehlern/Ablauf liegt eine neue Revision vor, die erneut bestätigt werden muss.
        await self.present_pending_draft(context.bot, chat.id)


def format_draft(draft: EmailDraft) -> str:
    email = draft.email
    lines = ["E-Mail-Entwurf zur Freigabe", "", f"An: {', '.join(a.display() for a in email.to)}"]
    if email.cc:
        lines.append(f"Cc: {', '.join(a.display() for a in email.cc)}")
    minutes = max(1, draft.ttl_seconds // 60)
    lines += [
        f"Betreff: {email.subject}",
        "",
        email.body,
        "",
        f"Die E-Mail wird erst versendet, wenn du auf \"Senden\" tippst. Die Freigabe gilt {minutes} Minuten.",
    ]
    return "\n".join(lines)


def draft_keyboard(draft: EmailDraft) -> InlineKeyboardMarkup:
    suffix = f"{draft.draft_id}:{draft.revision}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Senden", callback_data=f"email:send:{suffix}"),
                InlineKeyboardButton("Verwerfen", callback_data=f"email:discard:{suffix}"),
            ]
        ]
    )
