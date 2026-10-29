"""Konfiguration: Umgebungsvariablen laden und frühzeitig validieren."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .mail.models import EmailValidationError, normalize_email_address

DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_DATABASE_PATH = "data/assistant.db"
DEFAULT_TIMEZONE = "Europe/Berlin"
# Muss zu den Providern in ``mail.factory`` passen.
SUPPORTED_EMAIL_PROVIDERS = ("yahoo",)
# Muss zu den Providern in ``tasks.factory`` passen.
SUPPORTED_TASK_PROVIDERS = ("todoist",)

# Werte für Groqs ``reasoning_effort`` bei Bildanfragen ("none" = Instruct-Modus ohne Denkphase).
IMAGE_REASONING_EFFORTS = ("none", "default", "low", "medium", "high")

_TELEGRAM_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """Die Konfiguration ist unvollständig oder ungültig."""


@dataclass(frozen=True)
class EmailSettings:
    """Konfiguration der optionalen E-Mail-Integration."""

    provider: str
    address: str
    app_password: str = field(repr=False)
    sender_name: str
    # Leer = jede gültige Adresse (nach Bestätigung). Sonst nur diese Adressen bzw. "@domain".
    allowed_recipients: tuple[str, ...]
    draft_ttl_seconds: int
    timeout_seconds: float
    max_body_chars: int


@dataclass(frozen=True)
class TaskSettings:
    """Konfiguration der optionalen Aufgaben-Integration (Todoist)."""

    provider: str
    api_token: str = field(repr=False)
    timeout_seconds: float = 15.0


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
    email: EmailSettings | None = None  # None = E-Mail-Integration deaktiviert
    tasks: TaskSettings | None = None  # None = Aufgaben-Integration deaktiviert
    timezone: str = DEFAULT_TIMEZONE  # IANA-Name, z. B. "Europe/Berlin"
    groq_image_model: str | None = None  # None = Bildverarbeitung deaktiviert
    groq_image_reasoning_effort: str | None = None  # None = Parameter wird nicht gesendet

    @property
    def secrets(self) -> tuple[str, ...]:
        """Werte, die niemals im Klartext geloggt werden dürfen."""
        secrets = (self.telegram_bot_token, self.groq_api_key)
        if self.email is not None:
            secrets += (self.email.app_password,)
        if self.tasks is not None:
            secrets += (self.tasks.api_token,)
        return secrets


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

    image_model, image_reasoning_effort = _load_image_model(reader)

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
        email=_load_email_settings(reader, owner_name),
        tasks=_load_task_settings(reader),
        timezone=_load_timezone(reader),
        groq_image_model=image_model,
        groq_image_reasoning_effort=image_reasoning_effort,
    )

    if reader.errors:
        details = "\n".join(f"  - {error}" for error in reader.errors)
        raise ConfigError(f"Ungültige Konfiguration:\n{details}")
    return settings


def _load_email_settings(reader: _EnvReader, owner_name: str) -> EmailSettings | None:
    provider = reader.raw("EMAIL_PROVIDER").lower()
    if not provider:
        return None
    if provider not in SUPPORTED_EMAIL_PROVIDERS:
        reader.errors.append(
            f"EMAIL_PROVIDER {provider!r} wird nicht unterstützt (möglich: {', '.join(SUPPORTED_EMAIL_PROVIDERS)})."
        )

    address = reader.raw("EMAIL_ADDRESS")
    if not address:
        reader.errors.append("EMAIL_ADDRESS fehlt (Pflichtwert, wenn EMAIL_PROVIDER gesetzt ist).")
    else:
        try:
            address = normalize_email_address(address)
        except EmailValidationError:
            reader.errors.append("EMAIL_ADDRESS ist keine gültige E-Mail-Adresse.")

    # App-Passwörter werden teils mit Leerzeichen-Gruppen angezeigt; diese werden entfernt.
    app_password = "".join(reader.raw("EMAIL_APP_PASSWORD").split())
    if not app_password:
        reader.errors.append("EMAIL_APP_PASSWORD fehlt (Pflichtwert, wenn EMAIL_PROVIDER gesetzt ist).")

    sender_name = reader.text("EMAIL_SENDER_NAME", owner_name)
    if len(sender_name) > 100 or not sender_name.isprintable():
        reader.errors.append("EMAIL_SENDER_NAME darf höchstens 100 druckbare Zeichen enthalten.")

    return EmailSettings(
        provider=provider,
        address=address,
        app_password=app_password,
        sender_name=sender_name,
        allowed_recipients=_parse_recipient_allowlist(reader),
        draft_ttl_seconds=reader.integer("EMAIL_DRAFT_TTL_SECONDS", 900, 60, 86400),
        timeout_seconds=reader.number("EMAIL_TIMEOUT_SECONDS", 30.0, 5.0, 300.0),
        max_body_chars=reader.integer("EMAIL_MAX_BODY_CHARS", 6000, 500, 50000),
    )


def _load_image_model(reader: _EnvReader) -> tuple[str | None, str | None]:
    model = reader.raw("GROQ_IMAGE_MODEL") or None
    effort = reader.raw("GROQ_IMAGE_REASONING_EFFORT").lower() or None
    if effort is not None and effort not in IMAGE_REASONING_EFFORTS:
        reader.errors.append(
            f"GROQ_IMAGE_REASONING_EFFORT muss einer der Werte {', '.join(IMAGE_REASONING_EFFORTS)} sein "
            "(oder leer)."
        )
    if model is None:
        return None, None
    if effort is None and model.lower().startswith("qwen/"):
        # Qwen denkt sonst vor jeder Antwort ausführlich nach (langsamer, teurer, kann die Antwort abschneiden).
        effort = "none"
    return model, effort


def _load_timezone(reader: _EnvReader) -> str:
    name = reader.text("TIMEZONE", DEFAULT_TIMEZONE)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        reader.errors.append(
            f"TIMEZONE {name!r} ist keine bekannte Zeitzone (erwartet z. B. 'Europe/Berlin'; "
            "unter Windows muss das Paket 'tzdata' installiert sein)."
        )
    return name


def _load_task_settings(reader: _EnvReader) -> TaskSettings | None:
    provider = reader.raw("TASK_PROVIDER").lower()
    if not provider:
        return None
    if provider not in SUPPORTED_TASK_PROVIDERS:
        reader.errors.append(
            f"TASK_PROVIDER {provider!r} wird nicht unterstützt (möglich: {', '.join(SUPPORTED_TASK_PROVIDERS)})."
        )

    api_token = reader.raw("TODOIST_API_TOKEN")
    if not api_token:
        reader.errors.append("TODOIST_API_TOKEN fehlt (Pflichtwert, wenn TASK_PROVIDER=todoist gesetzt ist).")
    elif any(char.isspace() for char in api_token) or not api_token.isprintable():
        reader.errors.append("TODOIST_API_TOKEN darf keine Leer- oder Steuerzeichen enthalten.")

    return TaskSettings(
        provider=provider,
        api_token=api_token,
        timeout_seconds=reader.number("TODOIST_TIMEOUT_SECONDS", 15.0, 3.0, 120.0),
    )


def _parse_recipient_allowlist(reader: _EnvReader) -> tuple[str, ...]:
    entries = []
    for raw_entry in reader.raw("EMAIL_ALLOWED_RECIPIENTS").split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            if entry.startswith("@"):
                entries.append("@" + normalize_email_address("x" + entry).rpartition("@")[2])
            else:
                entries.append(normalize_email_address(entry).lower())
        except EmailValidationError:
            reader.errors.append(
                f"EMAIL_ALLOWED_RECIPIENTS enthält einen ungültigen Eintrag: {entry!r} "
                "(erwartet: name@domain.de oder @domain.de)."
            )
    return tuple(entries)
