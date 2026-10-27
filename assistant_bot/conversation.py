"""Conversation-Service: Gesprächsverläufe pro Chat in SQLite verwalten."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from .messages import ChatMessage, Role

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id);
"""


class ConversationService:
    """Speichert User- und Assistant-Nachrichten und liefert einen begrenzten Kontext.

    SQLite-Zugriffe sind synchron und werden per ``asyncio.to_thread`` ausgelagert,
    damit der Event-Loop des Bots nicht blockiert. Jede Operation nutzt eine
    eigene, kurzlebige Verbindung – einfach und threadsicher.
    """

    def __init__(self, database_path: Path, max_history_messages: int) -> None:
        if max_history_messages < 1:
            raise ValueError("max_history_messages muss mindestens 1 sein.")
        self._database_path = database_path
        self._max_history_messages = max_history_messages

    def initialize(self) -> None:
        """Legt Verzeichnis, Tabelle und Index an (idempotent)."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        logger.info("Gesprächsdatenbank bereit: %s", self._database_path)

    async def get_history(self, chat_id: int) -> list[ChatMessage]:
        """Liefert die letzten ``max_history_messages`` Nachrichten in chronologischer Reihenfolge."""
        return await asyncio.to_thread(self._get_history, chat_id)

    async def add_exchange(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        """Speichert Frage und Antwort atomar in einer Transaktion."""
        await asyncio.to_thread(self._add_exchange, chat_id, user_text, assistant_text)

    async def reset(self, chat_id: int) -> int:
        """Löscht den Gesprächsverlauf eines Chats und gibt die Anzahl gelöschter Nachrichten zurück."""
        return await asyncio.to_thread(self._reset, chat_id)

    def _get_history(self, chat_id: int) -> list[ChatMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM messages
                    WHERE chat_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (chat_id, self._max_history_messages),
            ).fetchall()
        history = [ChatMessage(role=cast(Role, role), content=content) for role, content in rows]
        # Durch das Abschneiden kann der Kontext mit einer Antwort beginnen –
        # ein sauberer Kontext startet immer mit einer Nutzernachricht.
        while history and history[0].role != "user":
            history.pop(0)
        return history

    def _add_exchange(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
                [(chat_id, "user", user_text), (chat_id, "assistant", assistant_text)],
            )

    def _reset(self, chat_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            return cursor.rowcount

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10)
        try:
            with connection:  # commit bei Erfolg, rollback bei Exception
                yield connection
        finally:
            connection.close()
