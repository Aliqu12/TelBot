"""Begrenzt, wie oft Statusmeldungen (Start/Stopp) das Sprachmodell nutzen dürfen.

Der Zähler liegt in der SQLite-Datenbank und überlebt deshalb Neustarts. Nur so greift die Grenze auch bei
einer Neustart-Schleife des Containers, bei der jeder Start einen neuen Prozess mit leerem Speicher bedeutet.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = "CREATE TABLE IF NOT EXISTS status_llm_calls (called_at REAL NOT NULL)"


class StatusRateLimiter:
    """Erlaubt höchstens ``max_calls`` Modellanfragen innerhalb von ``window_seconds`` (gleitendes Fenster)."""

    def __init__(
        self,
        database_path: Path,
        max_calls: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database_path = database_path
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._clock = clock

    async def try_acquire(self) -> bool:
        """Reserviert eine Modellanfrage. ``False`` heißt: Limit erreicht, bitte festen Text verwenden.

        Datenbankfehler zählen als "nicht erlaubt", damit im Zweifel keine LLM-Anfrage gesendet wird.
        """
        if self._max_calls <= 0:
            return False
        try:
            return await asyncio.to_thread(self._try_acquire)
        except sqlite3.Error as exc:
            logger.warning("Zähler für Statusmeldungen nicht nutzbar (%s), nutze festen Text.", type(exc).__name__)
            return False

    def _try_acquire(self) -> bool:
        now = self._clock()
        # isolation_level=None: Transaktion explizit steuern. BEGIN IMMEDIATE sperrt Schreiber, damit
        # Zählen und Eintragen atomar sind.
        connection = sqlite3.connect(self._database_path, timeout=5, isolation_level=None)
        try:
            connection.execute(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            # Alte Einträge entfernen, ebenso solche aus der "Zukunft" (z. B. nach einer Uhrzeitkorrektur).
            connection.execute(
                "DELETE FROM status_llm_calls WHERE called_at <= ? OR called_at > ?",
                (now - self._window_seconds, now + self._window_seconds),
            )
            (count,) = connection.execute("SELECT COUNT(*) FROM status_llm_calls").fetchone()
            allowed = count < self._max_calls
            if allowed:
                connection.execute("INSERT INTO status_llm_calls (called_at) VALUES (?)", (now,))
            connection.execute("COMMIT")
            return allowed
        finally:
            connection.close()  # ohne COMMIT wird eine offene Transaktion zurückgerollt
