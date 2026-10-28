"""Providerunabhängige Datenmodelle und Fehler der E-Mail-Integration.

Diese Typen sind der gemeinsame Vertrag zwischen ``EmailService`` und allen
Providern. Provider-spezifische Strukturen (IMAP-Antworten, API-Objekte)
verlassen die jeweilige Provider-Implementierung nicht.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

MAX_ADDRESS_LENGTH = 254

# Bewusst pragmatisch statt vollständiger RFC-5322-Grammatik: ASCII-Local-Part,
# Domain mit mindestens einem Punkt. Internationale Domains werden vorher per IDNA kodiert.
_LOCAL_PART = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ADDRESS_PATTERN = re.compile(rf"^{_LOCAL_PART}@{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+$")


class EmailError(Exception):
    """Basisklasse aller E-Mail-Fehler. Die Meldung ist für Nutzer verständlich formuliert."""


class EmailValidationError(EmailError):
    """Ungültige Eingabe, z. B. eine fehlerhafte Adresse oder ein nicht freigegebener Empfänger."""


class EmailAuthenticationError(EmailError):
    """Anmeldung beim Mailserver fehlgeschlagen (Adresse oder App-Passwort falsch)."""


class EmailConnectionError(EmailError):
    """Mailserver nicht erreichbar oder Verbindung abgebrochen."""


class EmailTimeoutError(EmailError):
    """Der Mailserver hat nicht rechtzeitig geantwortet."""


class EmailNotFoundError(EmailError):
    """Die angeforderte E-Mail existiert nicht (mehr)."""


class EmailSendError(EmailError):
    """Der Mailserver hat den Versand abgelehnt."""


class EmailCapability(Enum):
    """Fähigkeiten eines Providers. Nicht jeder künftige Provider muss alles können."""

    READ = "read"
    SEARCH = "search"
    SEND = "send"


def normalize_email_address(value: str) -> str:
    """Prüft eine E-Mail-Adresse und gibt sie normalisiert (Domain klein, IDNA) zurück.

    Raises:
        EmailValidationError: Bei ungültigem Format oder Steuerzeichen (Header-Injection).
    """
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_ADDRESS_LENGTH or any(ch.isspace() for ch in candidate):
        raise EmailValidationError(f"Ungültige E-Mail-Adresse: {_printable(value)!r}")
    local, _, domain = candidate.rpartition("@")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise EmailValidationError(f"Ungültige E-Mail-Adresse: {_printable(value)!r}") from None
    address = f"{local}@{ascii_domain}"
    if len(address) > MAX_ADDRESS_LENGTH or not _ADDRESS_PATTERN.fullmatch(address):
        raise EmailValidationError(f"Ungültige E-Mail-Adresse: {_printable(value)!r}")
    return address


def _printable(value: str) -> str:
    return "".join(ch for ch in value if ch.isprintable())[:100]


@dataclass(frozen=True, slots=True)
class EmailAddress:
    address: str
    name: str = ""

    def display(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Nur Metadaten – Anhänge werden weder gespeichert noch an das LLM übergeben."""

    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class EmailSummary:
    """Kopfdaten einer E-Mail für Übersichten und Suchergebnisse."""

    email_id: str  # Provider-interne, opake ID
    sender: EmailAddress
    subject: str
    date: datetime | None
    unread: bool


@dataclass(frozen=True, slots=True)
class EmailMessage:
    summary: EmailSummary
    to: tuple[EmailAddress, ...]
    cc: tuple[EmailAddress, ...]
    reply_to: tuple[EmailAddress, ...]
    body: str  # bereits als lesbarer Klartext aufbereitet
    attachments: tuple[AttachmentInfo, ...]
    message_id: str | None  # RFC-5322 Message-ID, für Antworten (In-Reply-To)
    references: tuple[str, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class EmailSearchQuery:
    sender: str | None = None
    subject: str | None = None
    text: str | None = None
    since: date | None = None
    before: date | None = None
    unread_only: bool = False
    limit: int = 10


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: tuple[EmailAddress, ...]
    subject: str
    body: str
    cc: tuple[EmailAddress, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

    @property
    def recipients(self) -> tuple[EmailAddress, ...]:
        return self.to + self.cc
