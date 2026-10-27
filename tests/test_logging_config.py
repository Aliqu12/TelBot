from __future__ import annotations

import logging
import sys

from assistant_bot.logging_config import RedactingFormatter


def make_record(msg: str, *args: object, exc_info: object = None) -> logging.LogRecord:
    return logging.LogRecord("test", logging.ERROR, __file__, 1, msg, args, exc_info)  # type: ignore[arg-type]


def test_secrets_are_redacted_in_message() -> None:
    formatter = RedactingFormatter("%(message)s", secrets=["geheim123", ""])

    output = formatter.format(make_record("URL: https://api.telegram.org/botgeheim123/getMe"))

    assert "geheim123" not in output
    assert "bot***/getMe" in output


def test_secrets_are_redacted_in_tracebacks() -> None:
    formatter = RedactingFormatter("%(message)s", secrets=["geheim123"])
    try:
        raise RuntimeError("Fehler mit geheim123")
    except RuntimeError:
        record = make_record("Absturz", exc_info=sys.exc_info())

    output = formatter.format(record)

    assert "geheim123" not in output
    assert "Fehler mit ***" in output
