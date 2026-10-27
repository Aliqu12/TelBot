"""Konfiguration: Umgebungsvariablen laden und frühzeitig validieren."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_DATABASE_PATH = "data/assistant.db"

_TELEGRAM_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """Die Konfiguration ist unvollständig oder ungültig."""


@dataclass(frozen=True)
class Settings:
    """Validierte, unveränderliche Anwendungskonfiguration."""

    # repr=False verhindert, dass Geheimnisse versehentlich in Logs landen.
    telegram_bot_token: str = field(repr=False)
    groq_api_key: str = field(repr=False)
    allowed_telegram_user_id: int | None
    owner_name: str
    groq_model: str
    groq_temperature: float
    groq_max_completion_tokens: int
    groq_timeout_seconds: float
    groq_max_retries: int
    database_path: Path
    history_max_messages: int
    max_input_chars: int
    log_level: str

    @property
    def secrets(self) -> tuple[str, ...]:
        """Werte, die niemals im Klartext geloggt werden dürfen."""
        return (self.telegram_bot_token, self.groq_api_key)


class _EnvReader:
    """Liest Umgebungsvariablen und sammelt alle Fehler, statt beim ersten abzubrechen."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def raw(self, name: str) -> str:
        return (self._env.get(name) or "").strip()

    def required_secret(self, name: str) -> str:
        value = self.raw(name)
        if not value:
            self.errors.append(f"{name} fehlt (Pflichtwert).")
        elif any(char.isspace() for char in value):
            self.errors.append(f"{name} darf keine Leerzeichen enthalten.")
        return value

    def text(self, name: str, default: str) -> str:
        return self.raw(name) or default

    def integer(self, name: str, default: int, minimum: int, maximum: int) -> int:
        raw = self.raw(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name} muss eine ganze Zahl sein (erhalten: {raw!r}).")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(f"{name} muss zwischen {minimum} und {maximum} liegen (erhalten: {value}).")
        return value

    def number(self, name: str, default: float, minimum: float, maximum: float) -> float:
        raw = self.raw(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{name} muss eine Zahl sein (erhalten: {raw!r}).")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(f"{name} muss zwischen {minimum} und {maximum} liegen (erhalten: {raw}).")
        return value

    def optional_user_id(self, name: str) -> int | None:
        raw = self.raw(name)
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name} muss eine numerische Telegram User-ID sein (erhalten: {raw!r}).")
            return None
        if value <= 0:
            self.errors.append(f"{name} muss eine positive Zahl sein (erhalten: {value}).")
            return None
        return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Lädt die Konfiguration aus den Umgebungsvariablen.

    Raises:
        ConfigError: Wenn Pflichtwerte fehlen oder Werte ungültig sind.
            Die Meldung enthält alle gefundenen Probleme, aber keine Geheimnisse.
    """
    reader = _EnvReader(os.environ if environ is None else environ)

    telegram_bot_token = reader.required_secret("TELEGRAM_BOT_TOKEN")
    if telegram_bot_token and not _TELEGRAM_TOKEN_PATTERN.fullmatch(telegram_bot_token):
        reader.errors.append(
            "TELEGRAM_BOT_TOKEN hat kein gültiges Format (erwartet: '<Zahl>:<Geheimnis>' von @BotFather)."
        )
    groq_api_key = reader.required_secret("GROQ_API_KEY")

    log_level = reader.text("LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        reader.errors.append(f"LOG_LEVEL muss einer der Werte {', '.join(_LOG_LEVELS)} sein.")

    owner_name = reader.text("OWNER_NAME", "Ali")
    if len(owner_name) > 100:
        reader.errors.append("OWNER_NAME darf höchstens 100 Zeichen lang sein.")

    settings = Settings(
        telegram_bot_token=telegram_bot_token,
        groq_api_key=groq_api_key,
        allowed_telegram_user_id=reader.optional_user_id("ALLOWED_TELEGRAM_USER_ID"),
        owner_name=owner_name,
        groq_model=reader.text("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        groq_temperature=reader.number("GROQ_TEMPERATURE", 0.7, 0.0, 2.0),
        groq_max_completion_tokens=reader.integer("GROQ_MAX_COMPLETION_TOKENS", 2048, 64, 32768),
        groq_timeout_seconds=reader.number("GROQ_TIMEOUT_SECONDS", 60.0, 1.0, 600.0),
        groq_max_retries=reader.integer("GROQ_MAX_RETRIES", 2, 0, 10),
        database_path=Path(reader.text("DATABASE_PATH", DEFAULT_DATABASE_PATH)),
        history_max_messages=reader.integer("HISTORY_MAX_MESSAGES", 20, 1, 200),
        max_input_chars=reader.integer("MAX_INPUT_CHARS", 4000, 1, 4096),
        log_level=log_level,
    )

    if reader.errors:
        details = "\n".join(f"  - {error}" for error in reader.errors)
        raise ConfigError(f"Ungültige Konfiguration:\n{details}")
    return settings
