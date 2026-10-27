"""Logging nach stdout (für Docker/Portainer) mit Schwärzung von Geheimnissen."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
REDACTED = "***"


class RedactingFormatter(logging.Formatter):
    """Ersetzt bekannte Geheimnisse im fertig formatierten Logeintrag (inkl. Tracebacks)."""

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # httpx loggt auf INFO jede Request-URL – bei Telegram enthält diese den Bot-Token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
