"""Aufteilen langer Antworten in Telegram-taugliche Nachrichten."""

from __future__ import annotations

# Telegram erlaubt 4096 Zeichen pro Nachricht; etwas Reserve schadet nicht.
TELEGRAM_SAFE_MESSAGE_LENGTH = 4000

_SEPARATORS = ("\n\n", "\n", " ")


def split_message(text: str, limit: int = TELEGRAM_SAFE_MESSAGE_LENGTH) -> list[str]:
    """Teilt Text in Stücke von höchstens ``limit`` Zeichen.

    Bevorzugt wird an Absätzen, dann an Zeilenumbrüchen, dann an Leerzeichen
    getrennt. Nur wenn nichts davon passt, wird hart geschnitten.
    """
    if limit < 1:
        raise ValueError("limit muss mindestens 1 sein.")

    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = _find_cut(remaining, limit)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _find_cut(text: str, limit: int) -> int:
    window = text[:limit]
    # Nicht zu früh trennen, sonst entstehen viele sehr kurze Nachrichten.
    min_cut = max(1, limit // 2)
    for separator in _SEPARATORS:
        index = window.rfind(separator)
        if index >= min_cut:
            return index
    return limit
