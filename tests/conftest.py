"""Gemeinsame Test-Fixtures. Es werden ausschließlich Dummy-Zugangsdaten verwendet."""

from __future__ import annotations

import pytest

# Formal gültige, aber frei erfundene Werte – niemals echte Tokens in Tests.
DUMMY_TELEGRAM_TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"
DUMMY_GROQ_KEY = "gsk_test_dummy_key_not_real"


@pytest.fixture
def valid_env() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": DUMMY_TELEGRAM_TOKEN,
        "GROQ_API_KEY": DUMMY_GROQ_KEY,
    }
