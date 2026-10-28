"""Yahoo Mail über IMAP (lesen/suchen) und SMTP (senden).

Offizielle Yahoo-Einstellungen (help.yahoo.com, SLN4075):
- IMAP: imap.mail.yahoo.com, Port 993, SSL
- SMTP: smtp.mail.yahoo.com, Port 465 (SSL) oder 587 (STARTTLS)
- Anmeldung: vollständige E-Mail-Adresse + App-Passwort (SLN15241)

Die Standardbibliotheken ``imaplib``/``smtplib`` arbeiten blockierend und
laufen deshalb in einem Worker-Thread. Für jede Operation wird eine eigene,
kurze Verbindung aufgebaut – einfach, robust und ohne Verbindungs-Pooling.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import smtplib
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from typing import TypeVar

from .mime import build_mime_message, parse_message, parse_summary
from .models import (
    EmailAddress,
    EmailAuthenticationError,
    EmailCapability,
    EmailConnectionError,
    EmailError,
    EmailMessage,
    EmailNotFoundError,
    EmailSearchQuery,
    EmailSendError,
    EmailSummary,
    EmailTimeoutError,
    OutgoingEmail,
)

logger = logging.getLogger(__name__)

YAHOO_IMAP_HOST = "imap.mail.yahoo.com"
YAHOO_IMAP_PORT = 993
YAHOO_SMTP_HOST = "smtp.mail.yahoo.com"
YAHOO_SMTP_PORT = 465

MAILBOX = "INBOX"
# Obergrenze pro abgerufener Nachricht: schützt den Speicher bei großen Anhängen.
MAX_FETCH_BYTES = 5 * 1024 * 1024
# Für Suchbegriffe mit Umlauten wird lokal in den neuesten N Nachrichten gefiltert.
CLIENT_SIDE_SCAN_LIMIT = 300

_SUMMARY_FETCH_ITEMS = "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

ImapFactory = Callable[[], imaplib.IMAP4]
SmtpFactory = Callable[[], smtplib.SMTP]
T = TypeVar("T")


class YahooEmailProvider:
    name = "yahoo"
    capabilities = frozenset({EmailCapability.READ, EmailCapability.SEARCH, EmailCapability.SEND})

    def __init__(
        self,
        *,
        address: str,
        app_password: str,
        sender_name: str,
        timeout_seconds: float,
        imap_factory: ImapFactory | None = None,
        smtp_factory: SmtpFactory | None = None,
    ) -> None:
        self._address = address
        self._app_password = app_password
        self._sender = EmailAddress(address=address, name=sender_name)
        # Zertifikats- und Hostnamenprüfung sind im Default-Kontext aktiv.
        tls = ssl.create_default_context()
        self._imap_factory = imap_factory or (
            lambda: imaplib.IMAP4_SSL(YAHOO_IMAP_HOST, YAHOO_IMAP_PORT, ssl_context=tls, timeout=timeout_seconds)
        )
        self._smtp_factory = smtp_factory or (
            lambda: smtplib.SMTP_SSL(YAHOO_SMTP_HOST, YAHOO_SMTP_PORT, context=tls, timeout=timeout_seconds)
        )

    # --- Öffentliche API (EmailProvider) --------------------------------------

    async def list_emails(self, limit: int, unread_only: bool = False) -> list[EmailSummary]:
        return await self.search_emails(EmailSearchQuery(unread_only=unread_only, limit=limit))

    async def search_emails(self, query: EmailSearchQuery) -> list[EmailSummary]:
        return await asyncio.to_thread(self._imap_call, lambda conn: self._search(conn, query))

    async def get_email(self, email_id: str) -> EmailMessage:
        if not email_id.isdigit():
            raise EmailNotFoundError("Diese E-Mail wurde nicht gefunden.")
        return await asyncio.to_thread(self._imap_call, lambda conn: self._fetch_message(conn, email_id))

    async def send_email(self, email: OutgoingEmail) -> None:
        await asyncio.to_thread(self._send, email)

    # --- IMAP -----------------------------------------------------------------

    def _imap_call(self, operation: Callable[[imaplib.IMAP4], T]) -> T:
        with _translate_imap_errors(), self._imap_session() as conn:
            return operation(conn)

    @contextmanager
    def _imap_session(self) -> Iterator[imaplib.IMAP4]:
        conn = self._imap_factory()
        try:
            try:
                conn.login(self._address, self._app_password)
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error:
                raise EmailAuthenticationError(
                    "Anmeldung bei Yahoo fehlgeschlagen. Bitte EMAIL_ADDRESS und EMAIL_APP_PASSWORD prüfen."
                ) from None
            # readonly (EXAMINE): Lesen verändert keine Flags, Mails bleiben z. B. ungelesen.
            _check(conn.select(MAILBOX, readonly=True), "Postfach konnte nicht geöffnet werden.")
            yield conn
        finally:
            try:
                conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def _search(self, conn: imaplib.IMAP4, query: EmailSearchQuery) -> list[EmailSummary]:
        criteria: list[str] = []
        local_filters: list[tuple[str, str]] = []
        if query.unread_only:
            criteria.append("UNSEEN")
        if query.since:
            criteria += ["SINCE", _imap_date(query.since)]
        if query.before:
            criteria += ["BEFORE", _imap_date(query.before)]
        for key, value in (("FROM", query.sender), ("SUBJECT", query.subject)):
            if not value:
                continue
            if value.isascii():
                criteria += [key, _quote(value)]
            else:
                local_filters.append((key, value))

        literal: bytes | None = None
        if query.text:
            if query.text.isascii():
                criteria += ["TEXT", _quote(query.text)]
            else:
                literal = query.text.encode("utf-8")

        if literal is not None:
            # Nicht-ASCII-Volltextsuche: Suchbegriff als IMAP-Literal mit CHARSET UTF-8.
            conn.literal = literal  # type: ignore[attr-defined]
            response = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria, "TEXT")
        else:
            response = conn.uid("SEARCH", *(criteria or ["ALL"]))
        data = _check(response, "Suche fehlgeschlagen.")
        uids = sorted((int(uid) for uid in (data[0] or b"").split()), reverse=True)

        if not local_filters:
            return self._fetch_summaries(conn, uids[: query.limit])

        candidates = self._fetch_summaries(conn, uids[:CLIENT_SIDE_SCAN_LIMIT])
        matches = [s for s in candidates if all(_matches(s, key, value) for key, value in local_filters)]
        return matches[: query.limit]

    def _fetch_summaries(self, conn: imaplib.IMAP4, uids: list[int]) -> list[EmailSummary]:
        if not uids:
            return []
        uid_set = ",".join(str(uid) for uid in uids)
        data = _check(conn.uid("FETCH", uid_set, _SUMMARY_FETCH_ITEMS), "Abruf fehlgeschlagen.")
        summaries = [
            parse_summary(uid, payload, unread=unread) for uid, unread, payload in _parse_fetch_response(data)
        ]
        return sorted(summaries, key=lambda s: int(s.email_id), reverse=True)

    def _fetch_message(self, conn: imaplib.IMAP4, email_id: str) -> EmailMessage:
        data = _check(
            conn.uid("FETCH", email_id, f"(UID FLAGS BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)"),
            "Abruf fehlgeschlagen.",
        )
        for uid, unread, payload in _parse_fetch_response(data):
            if uid == email_id:
                return parse_message(uid, payload, unread=unread, truncated=len(payload) >= MAX_FETCH_BYTES)
        raise EmailNotFoundError("Diese E-Mail wurde nicht gefunden.")

    # --- SMTP -----------------------------------------------------------------

    def _send(self, email: OutgoingEmail) -> None:
        message = build_mime_message(email, self._sender)
        try:
            with self._smtp_factory() as smtp:
                smtp.login(self._address, self._app_password)
                refused = smtp.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise EmailAuthenticationError(
                "Anmeldung beim Yahoo-Postausgang fehlgeschlagen. Bitte EMAIL_APP_PASSWORD prüfen."
            ) from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            logger.warning("SMTP-Server hat den Versand abgelehnt: %s", type(exc).__name__)
            raise EmailSendError("Der Mailserver hat den Versand abgelehnt.") from None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            logger.warning("SMTP-Verbindung fehlgeschlagen: %s", type(exc).__name__)
            raise EmailConnectionError("Verbindung zum Yahoo-Postausgang fehlgeschlagen.") from None
        except smtplib.SMTPException as exc:
            logger.warning("SMTP-Fehler: %s", type(exc).__name__)
            raise EmailSendError("Der Versand ist fehlgeschlagen.") from None
        except TimeoutError:
            raise EmailTimeoutError("Der Yahoo-Postausgang hat nicht rechtzeitig geantwortet.") from None
        except OSError as exc:
            logger.warning("Netzwerkfehler beim Versand: %s", type(exc).__name__)
            raise EmailConnectionError("Der Yahoo-Postausgang ist nicht erreichbar.") from None
        if refused:
            logger.warning("%d Empfänger wurden vom SMTP-Server abgelehnt.", len(refused))


@contextmanager
def _translate_imap_errors() -> Iterator[None]:
    try:
        yield
    except EmailError:
        raise
    except TimeoutError:
        raise EmailTimeoutError("Yahoo Mail hat nicht rechtzeitig geantwortet.") from None
    except imaplib.IMAP4.abort as exc:
        logger.warning("IMAP-Verbindung abgebrochen: %s", type(exc).__name__)
        raise EmailConnectionError("Die Verbindung zu Yahoo Mail wurde unterbrochen.") from None
    except imaplib.IMAP4.error as exc:
        logger.warning("IMAP-Fehler: %s", type(exc).__name__)
        raise EmailError("Yahoo Mail hat die Anfrage abgelehnt.") from None
    except OSError as exc:  # inkl. ssl.SSLError, DNS- und Verbindungsfehler
        logger.warning("Netzwerkfehler bei IMAP: %s", type(exc).__name__)
        raise EmailConnectionError("Yahoo Mail ist nicht erreichbar.") from None


def _check(response: tuple[str, list], error_message: str) -> list:  # type: ignore[type-arg]
    status, data = response
    if status != "OK":
        logger.warning("IMAP-Antwort %s: %s", status, error_message)
        raise EmailError(error_message)
    return data


def _parse_fetch_response(data: list) -> list[tuple[str, bool, bytes]]:  # type: ignore[type-arg]
    """Zerlegt eine imaplib-FETCH-Antwort in (UID, ungelesen, Nutzdaten).

    imaplib liefert Tupel (Metadaten, Literal); manche Server senden FLAGS erst
    nach dem Literal als separaten bytes-Eintrag – beide Varianten werden erkannt.
    """
    records: list[list[bytes]] = []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            records.append([bytes(item[0]), bytes(item[1])])
        elif isinstance(item, bytes) and records:
            records[-1][0] += b" " + item
    result = []
    for meta, payload in records:
        uid_match = re.search(rb"UID (\d+)", meta)
        if not uid_match:
            continue
        flags_match = re.search(rb"FLAGS \(([^)]*)\)", meta)
        unread = not (flags_match and b"\\Seen" in flags_match.group(1))
        result.append((uid_match.group(1).decode("ascii"), unread, payload))
    return result


def _matches(summary: EmailSummary, key: str, value: str) -> bool:
    needle = value.casefold()
    if key == "FROM":
        haystack = f"{summary.sender.name} {summary.sender.address}"
    else:
        haystack = summary.subject
    return needle in haystack.casefold()


def _imap_date(value: date) -> str:
    # Monatsnamen fest auf Englisch (IMAP-Vorgabe), unabhängig von der System-Locale.
    return f"{value.day:02d}-{_MONTHS[value.month - 1]}-{value.year}"


def _quote(value: str) -> str:
    """IMAP quoted string; Zeilenumbrüche werden entfernt (keine Befehlsinjektion)."""
    cleaned = " ".join(value.split())
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'
