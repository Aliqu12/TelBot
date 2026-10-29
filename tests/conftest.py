"""Gemeinsame Test-Fixtures. Es werden ausschließlich Dummy-Zugangsdaten verwendet."""

from __future__ import annotations

import pytest

# Formal gültige, aber frei erfundene Werte – niemals echte Tokens in Tests.
DUMMY_TELEGRAM_TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"
DUMMY_LLM_KEY = "sk-or-test-dummy-key-not-real"
TEST_LLM_MODEL = "test-vendor/test-model"


@pytest.fixture
def valid_env() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": DUMMY_TELEGRAM_TOKEN,
        "OPENROUTER_API_KEY": DUMMY_LLM_KEY,
        "LLM_MODEL": TEST_LLM_MODEL,
    }
