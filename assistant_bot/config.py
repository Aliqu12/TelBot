"""Konfiguration: Umgebungsvariablen laden und frühzeitig validieren."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .mail.models import EmailValidationError, normalize_email_address

DEFAULT_LLM_PROVIDER = "openrouter"
DEFAULT_DATABASE_PATH = "data/assistant.db"
DEFAULT_TIMEZONE = "Europe/Berlin"
# Textdatei mit der Persönlichkeit des Assistenten (relativ zum Arbeitsverzeichnis, im Container /app).
DEFAULT_PERSONALITY_FILE = "personality.md"
MAX_PERSONALITY_CHARS = 20000
# Bilder kosten viele Tokens; bei Bildanfragen wird deshalb weniger Verlauf mitgesendet.
DEFAULT_IMAGE_HISTORY_MESSAGES = 4
# Größte erlaubte Bilddatei in MB (Telegram liefert Dateien für Bots nur bis 20 MB aus).
DEFAULT_IMAGE_MAX_MB = 5
MAX_IMAGE_MB_LIMIT = 20
# Wie oft Start-/Stoppmeldungen pro Minute das Sprachmodell nutzen dürfen; danach gibt es nur feste Texte.
DEFAULT_STATUS_LLM_MAX_PER_MINUTE = 2
_TRUE_VALUES = ("true", "1", "yes", "ja", "on")
_FALSE_VALUES = ("false", "0", "no", "nein", "off")
# Muss zu den Providern in ``mail.factory`` passen.
SUPPORTED_EMAIL_PROVIDERS = ("yahoo",)
# Muss zu den Providern in ``tasks.factory`` passen.
SUPPORTED_TASK_PROVIDERS = ("todoist",)
# Muss zu den Providern in ``web.factory`` passen.
SUPPORTED_WEB_PROVIDERS = ("tavily",)

# Anbieter für das Sprachmodell; muss zu ``llm.factory.PROVIDERS`` passen.
SUPPORTED_LLM_PROVIDERS = ("openrouter", "groq")
# Umgebungsvariable mit dem API-Key je Anbieter (nur der Key des gewählten Anbieters ist Pflicht).
LLM_API_KEY_VARIABLES = {"openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY"}

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
class ReminderSettings:
    """Konfiguration der Aufgaben-Erinnerungen (benötigt die Aufgaben-Integration)."""

    poll_seconds: int = 300
    default_time: time = time(12, 0)  # Uhrzeit für Aufgaben, die nur ein Datum haben
    early_hours: int = 24  # erste Erinnerung
    urgent_hours: int = 2  # zweite, dringlichere Erinnerung


@dataclass(frozen=True)
class WebSettings:
    """Konfiguration der optionalen Websuche (z. B. Tavily)."""

    provider: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 20.0
    max_results: int = 5  # Treffer pro Suche
    max_chars: int = 4000  # Zeichen pro Treffer bzw. gelesener Seite
    max_calls_per_day: int = 30  # 0 = unbegrenzt


@dataclass(frozen=True)
class Settings:
    """Validierte, unveränderliche Anwendungskonfiguration."""

    # repr=False verhindert, dass Geheimnisse versehentlich in Logs landen.
    telegram_bot_token: str = field(repr=False)
    llm_provider: str
    llm_api_key: str = field(repr=False)
    allowed_telegram_user_id: int | None
    owner_name: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout_seconds: float
    llm_max_retries: int
    database_path: Path
    history_max_messages: int
    max_input_chars: int
    log_level: str
    email: EmailSettings | None = None  # None = E-Mail-Integration deaktiviert
    tasks: TaskSettings | None = None  # None = Aufgaben-Integration deaktiviert
    reminders: ReminderSettings | None = None  # None = keine Erinnerungen
    web: WebSettings | None = None  # None = Websuche deaktiviert
    timezone: str = DEFAULT_TIMEZONE  # IANA-Name, z. B. "Europe/Berlin"
    llm_image_model: str | None = None  # None = Bilder gehen an das Textmodell (llm_model)
    image_max_mb: int = DEFAULT_IMAGE_MAX_MB
    image_history_max_messages: int = DEFAULT_IMAGE_HISTORY_MESSAGES  # Verlauf bei Bildanfragen
    status_messages_enabled: bool = True  # Begrüßung beim Start und Abschied beim Beenden
    status_llm_max_per_minute: int = DEFAULT_STATUS_LLM_MAX_PER_MINUTE  # 0 = immer feste Texte
    personality_file: Path = Path(DEFAULT_PERSONALITY_FILE)  # Persönlichkeit des Assistenten (Textdatei)
    llm_reasoning: bool = False  # True = Modell darf denken, die Gedanken werden aber nie mitgeliefert

    @property
    def image_max_bytes(self) -> int:
        return self.image_max_mb * 1024 * 1024

    @property
    def secrets(self) -> tuple[str, ...]:
        """Werte, die niemals im Klartext geloggt werden dürfen."""
        secrets = (self.telegram_bot_token, self.llm_api_key)
        if self.email is not None:
            secrets += (self.email.app_password,)
        if self.tasks is not None:
            secrets += (self.tasks.api_token,)
        if self.web is not None:
            secrets += (self.web.api_key,)
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

    def required_text(self, name: str) -> str:
        value = self.raw(name)
        if not value:
            self.errors.append(f"{name} fehlt (Pflichtwert, z. B. 'openai/gpt-oss-120b').")
        return value

    def boolean(self, name: str, default: bool) -> bool:
        raw = self.raw(name).lower()
        if not raw:
            return default
        if raw in _TRUE_VALUES:
            return True
        if raw in _FALSE_VALUES:
            return False
        self.errors.append(f"{name} muss true oder false sein (erhalten: {raw!r}).")
        return default

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
    llm_provider = reader.text("LLM_PROVIDER", DEFAULT_LLM_PROVIDER).lower()
    if llm_provider not in SUPPORTED_LLM_PROVIDERS:
        reader.errors.append(
            f"LLM_PROVIDER {llm_provider!r} wird nicht unterstützt (möglich: {', '.join(SUPPORTED_LLM_PROVIDERS)})."
        )
        llm_api_key = ""
    else:
        llm_api_key = reader.required_secret(LLM_API_KEY_VARIABLES[llm_provider])
    llm_model = reader.required_text("LLM_MODEL")

    log_level = reader.text("LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        reader.errors.append(f"LOG_LEVEL muss einer der Werte {', '.join(_LOG_LEVELS)} sein.")

    owner_name = reader.text("OWNER_NAME", "Ali")
    if len(owner_name) > 100:
        reader.errors.append("OWNER_NAME darf höchstens 100 Zeichen lang sein.")

    allowed_user_id = reader.optional_user_id("ALLOWED_TELEGRAM_USER_ID")
    tasks = _load_task_settings(reader)
    settings = Settings(
        telegram_bot_token=telegram_bot_token,
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
        allowed_telegram_user_id=allowed_user_id,
        owner_name=owner_name,
        llm_model=llm_model,
        llm_temperature=reader.number("LLM_TEMPERATURE", 0.7, 0.0, 2.0),
        llm_max_tokens=reader.integer("LLM_MAX_TOKENS", 2048, 64, 32768),
        llm_timeout_seconds=reader.number("LLM_TIMEOUT_SECONDS", 60.0, 1.0, 600.0),
        llm_max_retries=reader.integer("LLM_MAX_RETRIES", 2, 0, 10),
        database_path=Path(reader.text("DATABASE_PATH", DEFAULT_DATABASE_PATH)),
        history_max_messages=reader.integer("HISTORY_MAX_MESSAGES", 20, 1, 200),
        max_input_chars=reader.integer("MAX_INPUT_CHARS", 4000, 1, 4096),
        log_level=log_level,
        email=_load_email_settings(reader, owner_name),
        tasks=tasks,
        reminders=_load_reminder_settings(
            reader, tasks_enabled=tasks is not None, owner_known=allowed_user_id is not None
        ),
        web=_load_web_settings(reader),
        timezone=_load_timezone(reader),
        llm_image_model=reader.raw("LLM_IMAGE_MODEL") or None,
        image_max_mb=reader.integer("IMAGE_MAX_MB", DEFAULT_IMAGE_MAX_MB, 1, MAX_IMAGE_MB_LIMIT),
        image_history_max_messages=reader.integer(
            "IMAGE_HISTORY_MAX_MESSAGES", DEFAULT_IMAGE_HISTORY_MESSAGES, 0, 200
        ),
        status_messages_enabled=reader.boolean("STATUS_MESSAGES_ENABLED", True),
        status_llm_max_per_minute=reader.integer(
            "STATUS_LLM_MAX_PER_MINUTE", DEFAULT_STATUS_LLM_MAX_PER_MINUTE, 0, 60
        ),
        personality_file=Path(reader.text("PERSONALITY_FILE", DEFAULT_PERSONALITY_FILE)),
        llm_reasoning=reader.boolean("LLM_REASONING", False),
    )

    if reader.errors:
        details = "\n".join(f"  - {error}" for error in reader.errors)
        raise ConfigError(f"Ungültige Konfiguration:\n{details}")
    return settings


def load_personality(path: Path) -> str:
    """Liest die Persönlichkeitsdatei (UTF-8).

    Raises:
        ConfigError: Wenn die Datei fehlt, nicht lesbar, leer oder unsinnig groß ist.
    """
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        raise ConfigError(
            f"PERSONALITY_FILE {str(path)!r} wurde nicht gefunden. Die Datei mit der Persönlichkeit des "
            "Assistenten ist Pflicht (Standard: personality.md)."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"PERSONALITY_FILE {str(path)!r} konnte nicht gelesen werden ({type(exc).__name__}); "
            "erwartet wird eine UTF-8-Textdatei."
        ) from None
    if not text:
        raise ConfigError(f"PERSONALITY_FILE {str(path)!r} ist leer.")
    if len(text) > MAX_PERSONALITY_CHARS:
        raise ConfigError(
            f"PERSONALITY_FILE {str(path)!r} ist zu lang ({len(text)} Zeichen, höchstens {MAX_PERSONALITY_CHARS})."
        )
    return text


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


def _load_reminder_settings(reader: _EnvReader, *, tasks_enabled: bool, owner_known: bool) -> ReminderSettings | None:
    # Ohne Aufgaben-Integration oder bekannte User-ID gibt es nichts abzufragen bzw. niemanden zu erinnern.
    if not reader.boolean("REMINDERS_ENABLED", True) or not tasks_enabled or not owner_known:
        return None
    default_time = ReminderSettings.default_time
    raw_time = reader.raw("REMINDER_DEFAULT_TIME")
    if raw_time:
        try:
            default_time = time.fromisoformat(raw_time)
        except ValueError:
            reader.errors.append(f"REMINDER_DEFAULT_TIME muss eine Uhrzeit wie 12:00 sein (erhalten: {raw_time!r}).")
    early_hours = reader.integer("REMINDER_EARLY_HOURS", 24, 1, 168)
    urgent_hours = reader.integer("REMINDER_URGENT_HOURS", 2, 1, 168)
    if urgent_hours >= early_hours:
        reader.errors.append("REMINDER_URGENT_HOURS muss kleiner sein als REMINDER_EARLY_HOURS.")
    return ReminderSettings(
        poll_seconds=reader.integer("REMINDER_POLL_SECONDS", 300, 60, 3600),
        default_time=default_time,
        early_hours=early_hours,
        urgent_hours=urgent_hours,
    )


def _load_web_settings(reader: _EnvReader) -> WebSettings | None:
    provider = reader.raw("WEB_SEARCH_PROVIDER").lower()
    if not provider:
        return None
    if provider not in SUPPORTED_WEB_PROVIDERS:
        reader.errors.append(
            f"WEB_SEARCH_PROVIDER {provider!r} wird nicht unterstützt (möglich: {', '.join(SUPPORTED_WEB_PROVIDERS)})."
        )

    api_key = reader.raw("TAVILY_API_KEY")
    if not api_key:
        reader.errors.append("TAVILY_API_KEY fehlt (Pflichtwert, wenn WEB_SEARCH_PROVIDER=tavily gesetzt ist).")
    elif any(char.isspace() for char in api_key) or not api_key.isprintable():
        reader.errors.append("TAVILY_API_KEY darf keine Leer- oder Steuerzeichen enthalten.")

    return WebSettings(
        provider=provider,
        api_key=api_key,
        timeout_seconds=reader.number("WEB_TIMEOUT_SECONDS", 20.0, 3.0, 120.0),
        max_results=reader.integer("WEB_MAX_RESULTS", 5, 1, 20),
        max_chars=reader.integer("WEB_MAX_CHARS", 4000, 500, 50000),
        max_calls_per_day=reader.integer("WEB_MAX_CALLS_PER_DAY", 30, 0, 10000),
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
