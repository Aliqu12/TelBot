"""Telegram-Bestätigung für das Löschen von Aufgaben.

Gelöscht wird ausschließlich hier – nach einem Klick des berechtigten Nutzers
auf "Löschen". Die Schaltfläche enthält die ID der Löschanfrage; der
``TaskService`` prüft Ablaufzeit und verhindert doppeltes Löschen.
"""

from __future__ import annotations

import contextlib
import logging
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..tasks.models import TaskError, TaskNotFoundError, TaskTimeoutError
from ..tasks.service import DELETION_TTL_SECONDS, DeletionError, PendingDeletion, TaskService
from ..tasks.tools import format_due
from .access import AccessPolicy

logger = logging.getLogger(__name__)

CALLBACK_PATTERN = r"^task:"
_CALLBACK_RE = re.compile(r"^task:(del|keep):([0-9a-f]{1,32})$")


class TaskConfirmationHandlers:
    def __init__(self, *, tasks: TaskService, access: AccessPolicy) -> None:
        self._tasks = tasks
        self._access = access

    async def present_pending_deletion(self, bot: Bot, chat_id: int) -> None:
        """Zeigt eine neu angeforderte Löschung mit Bestätigungs-Schaltflächen an."""
        deletion = self._tasks.take_deletion_for_review(chat_id)
        if deletion is None:
            return
        await bot.send_message(
            chat_id=chat_id,
            text=format_deletion(deletion, self._tasks),
            reply_markup=deletion_keyboard(deletion),
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        if query is None:
            return
        if user is None or not self._access.is_authorized(user.id):
            logger.warning("Zugriff verweigert: user_id=%s, Aktion=Aufgaben-Schaltfläche", user.id if user else None)
            with contextlib.suppress(TelegramError):
                await query.answer()
            return

        with contextlib.suppress(TelegramError):
            await query.answer()
        match = _CALLBACK_RE.match(query.data or "")
        if chat is None or chat.type != ChatType.PRIVATE or match is None:
            return
        action, deletion_id = match.group(1), match.group(2)

        # Schaltflächen sofort entfernen, damit nicht mehrfach getippt wird.
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)

        if action == "keep":
            cancelled = self._tasks.cancel_deletion(chat.id, deletion_id)
            text = "Alles klar, die Aufgabe bleibt bestehen." if cancelled else "Diese Löschanfrage ist nicht mehr offen."
            await context.bot.send_message(chat_id=chat.id, text=text)
            return

        try:
            task = await self._tasks.confirm_deletion(chat.id, deletion_id)
        except DeletionError as exc:
            text = str(exc)
        except TaskNotFoundError:
            text = "Die Aufgabe existiert in Todoist nicht mehr (bereits erledigt oder gelöscht)."
        except TaskTimeoutError:
            text = (
                "Todoist hat nicht rechtzeitig geantwortet. Die Aufgabe wurde möglicherweise trotzdem gelöscht. "
                "Bitte in Todoist prüfen."
            )
        except TaskError as exc:
            text = f"Das Löschen ist fehlgeschlagen: {exc}"
        else:
            text = f"Die Aufgabe „{task.content}“ wurde gelöscht."
        await context.bot.send_message(chat_id=chat.id, text=text)


def format_deletion(deletion: PendingDeletion, tasks: TaskService) -> str:
    task = deletion.task
    lines = ["Aufgabe endgültig löschen?", "", task.content]
    if task.due is not None:
        lines.append(f"Fällig: {format_due(task.due, tasks.now())}")
    minutes = max(1, DELETION_TTL_SECONDS // 60)
    lines += [
        "",
        f"Gelöscht wird erst, wenn du auf \"Löschen\" tippst (gültig {minutes} Minuten). "
        "Zum Abhaken stattdessen \"erledigt\" sagen.",
    ]
    return "\n".join(lines)


def deletion_keyboard(deletion: PendingDeletion) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Löschen", callback_data=f"task:del:{deletion.deletion_id}"),
                InlineKeyboardButton("Behalten", callback_data=f"task:keep:{deletion.deletion_id}"),
            ]
        ]
    )
