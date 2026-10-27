from __future__ import annotations

import pytest

from assistant_bot.telegram_bot.message_split import split_message


def test_short_text_is_single_chunk() -> None:
    assert split_message("  Hallo  ", limit=10) == ["Hallo"]


def test_empty_text_returns_no_chunks() -> None:
    assert split_message("   \n ", limit=10) == []


def test_prefers_paragraph_boundaries() -> None:
    text = "Erster Absatz.\n\nZweiter Absatz."
    assert split_message(text, limit=20) == ["Erster Absatz.", "Zweiter Absatz."]


def test_falls_back_to_spaces() -> None:
    chunks = split_message("eins zwei drei vier fünf", limit=10)
    assert chunks == ["eins zwei", "drei vier", "fünf"]


def test_hard_cut_without_whitespace() -> None:
    assert split_message("a" * 25, limit=10) == ["a" * 10, "a" * 10, "a" * 5]


def test_all_chunks_respect_limit_and_preserve_content() -> None:
    text = " ".join(f"Wort{i}" for i in range(2000))
    chunks = split_message(text, limit=100)

    assert all(0 < len(chunk) <= 100 for chunk in chunks)
    assert " ".join(chunks) == text


def test_invalid_limit_raises() -> None:
    with pytest.raises(ValueError):
        split_message("x", limit=0)
