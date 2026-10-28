"""EmailService: anwendungsbezogene E-Mail-Logik, unabhängig vom konkreten Provider.

Verantwortlich für:
- Übersichten und Suchen (inkl. Nummerierung für Rückfragen wie "zeig mir Nr. 2")
- Abruf einzelner E-Mails mit begrenzter Textlänge (Datensparsamkeit gegenüber dem LLM)
- Entwürfe und deren Freigabe-Workflow

Sicherheitsprinzip für den Versand: Das LLM kann Entwürfe nur *vorbereiten*.
Versendet wird ausschließlich über ``confirm_and_send``, das nur von der
Telegram-Schicht nach einem Button-Klick des berechtigten Nutzers aufgerufen
wird. Jede Anzeige eines Entwurfs erzeugt eine neue Revision mit Ablaufzeit;
nur die Bestätigung genau dieser Revision ist gültig. Ein Statusautomat
(PENDING -> SENDING -> SENT) verhindert Mehrfachversand.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from email.utils import parseaddr
from enum import Enum

from .models import (
    EmailAddress,
    EmailCapability,
    EmailError,
    EmailMessage,
    EmailSearchQuery,
    EmailSendError,
    EmailSummary,
    EmailValidationError,
    OutgoingEmail,
    normalize_email_address,
)
from .provider import EmailProvider

logger = logging.getLogger(__name__)

MAX_RESULTS = 20
MAX_RECIPIENTS = 10
MAX_SUBJECT_CHARS = 200
MAX_DRAFT_BODY_CHARS = 10_000
MAX_SEARCH_TERM_CHARS = 200
LISTING_TTL_SECONDS = 3600


class DraftStatus(Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class EmailDraft:
    draft_id: str
    revision: int
    email: OutgoingEmail
    status: DraftStatus
    expires_at: float  # Zeitpunkt der Uhr des Services (monoton)
    ttl_seconds: int


class DraftConfirmationError(EmailError):
    """Die Versandfreigabe ist nicht (mehr) gültig."""


class DraftNotFoundError(DraftConfirmationError):
    pass


class DraftOutdatedError(DraftConfirmationError):
    pass


class DraftExpiredError(DraftConfirmationError):
    pass


class DraftAlreadySentError(DraftConfirmationError):
    pass


@dataclass
class _DraftRecord:
    draft: EmailDraft
    needs_review: bool


@dataclass
class _Listing:
    email_ids: list[str]
    expires_at: float


class EmailService:
    def __init__(
        self,
        provider: EmailProvider,
        *,
        allowed_recipients: Sequence[str] = (),
        draft_ttl_seconds: int = 900,
        max_body_chars: int = 6000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._allowed_recipients = tuple(entry.lower() for entry in allowed_recipients)
        self._draft_ttl_seconds = draft_ttl_seconds
        self._max_body_chars = max_body_chars
        self._clock = clock
        # Bewusst nur im Arbeitsspeicher: E-Mail-Inhalte werden nicht dauerhaft gespeichert.
        self._drafts: dict[int, _DraftRecord] = {}
        self._listings: dict[int, _Listing] = {}

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def capabilities(self) -> frozenset[EmailCapability]:
        return self._provider.capabilities

    # --- Lesen ----------------------------------------------------------------

    async def list_recent(self, chat_id: int, limit: int = 5, unread_only: bool = False) -> list[EmailSummary]:
        self._require(EmailCapability.READ)
        summaries = await self._provider.list_emails(_clamp(limit), unread_only)
        self._remember_listing(chat_id, summaries)
        return summaries

    async def search(self, chat_id: int, query: EmailSearchQuery) -> list[EmailSummary]:
        self._require(EmailCapability.SEARCH)
        for term in (query.sender, query.subject, query.text):
            if term is not None and len(term) > MAX_SEARCH_TERM_CHARS:
                raise EmailValidationError("Der Suchbegriff ist zu lang.")
        query = replace(
            query,
            sender=_blank_to_none(query.sender),
            subject=_blank_to_none(query.subject),
            text=_blank_to_none(query.text),
            limit=_clamp(query.limit),
        )
        summaries = await self._provider.search_emails(query)
        self._remember_listing(chat_id, summaries)
        return summaries

    async def read(self, chat_id: int, number: int) -> EmailMessage:
        """Liest die E-Mail mit der Nummer aus der letzten Übersicht/Suche (Text gekürzt)."""
        self._require(EmailCapability.READ)
        message = await self._provider.get_email(self._resolve(chat_id, number))
        if len(message.body) > self._max_body_chars:
            message = replace(message, body=message.body[: self._max_body_chars].rstrip(), truncated=True)
        return message

    # --- Entwürfe -------------------------------------------------------------

    def create_draft(
        self,
        chat_id: int,
        *,
        to: Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] = (),
    ) -> EmailDraft:
        self._require(EmailCapability.SEND)
        to_addresses = self._validate_recipients(to)
        email = self._validated_email(
            to=to_addresses,
            cc=_without(self._validate_recipients(cc), to_addresses),
            subject=subject,
            body=body,
        )
        return self._store_new_draft(chat_id, email)

    async def create_reply_draft(self, chat_id: int, number: int, body: str) -> EmailDraft:
        self._require(EmailCapability.SEND)
        original = await self._provider.get_email(self._resolve(chat_id, number))
        targets = original.reply_to or (original.summary.sender,)
        recipients = self._validate_recipients([a.address for a in targets])
        subject = original.summary.subject
        if not subject.lower().startswith(("re:", "aw:")):
            subject = f"Re: {subject}"
        references = original.references
        if original.message_id and original.message_id not in references:
            references = references + (original.message_id,)
        email = self._validated_email(to=recipients, cc=(), subject=subject[:MAX_SUBJECT_CHARS], body=body)
        email = replace(email, in_reply_to=original.message_id, references=references)
        return self._store_new_draft(chat_id, email)

    def update_draft(
        self,
        chat_id: int,
        *,
        to: Sequence[str] | None = None,
        cc: Sequence[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
    ) -> EmailDraft:
        """Ändert den aktuellen Entwurf. Jede Änderung erfordert eine neue Freigabe."""
        record = self._pending_record(chat_id)
        current = record.draft.email
        new_to = current.to if to is None else self._validate_recipients(to)
        new_cc = current.cc if cc is None else self._validate_recipients(cc)
        updated = self._validated_email(
            to=new_to,
            cc=_without(new_cc, new_to),
            subject=current.subject if subject is None else subject,
            body=current.body if body is None else body,
        )
        updated = replace(updated, in_reply_to=current.in_reply_to, references=current.references)
        return self._issue_revision(record, updated)

    def current_draft(self, chat_id: int) -> EmailDraft | None:
        record = self._drafts.get(chat_id)
        return record.draft if record else None

    def request_review(self, chat_id: int) -> EmailDraft:
        """Zeigt den aktuellen Entwurf erneut zur Freigabe an (neue Revision, neue Frist)."""
        record = self._pending_record(chat_id)
        return self._issue_revision(record, record.draft.email)

    def take_draft_for_review(self, chat_id: int) -> EmailDraft | None:
        """Liefert einen Entwurf, der dem Nutzer jetzt zur Freigabe angezeigt werden muss."""
        record = self._drafts.get(chat_id)
        if record is None or not record.needs_review or record.draft.status is not DraftStatus.PENDING:
            return None
        record.needs_review = False
        return record.draft

    def discard_draft(self, chat_id: int, draft_id: str | None = None) -> bool:
        record = self._drafts.get(chat_id)
        if record is None or record.draft.status is DraftStatus.SENDING:
            return False
        if draft_id is not None and record.draft.draft_id != draft_id:
            return False
        if record.draft.status is DraftStatus.SENT:
            return False
        del self._drafts[chat_id]
        return True

    # --- Versand --------------------------------------------------------------

    async def confirm_and_send(self, chat_id: int, draft_id: str, revision: int) -> EmailDraft:
        """Versendet einen Entwurf – nur mit gültiger, aktueller und nicht abgelaufener Freigabe.

        Darf ausschließlich nach einer ausdrücklichen Nutzeraktion aufgerufen werden
        (Telegram-Button), niemals durch das LLM.
        """
        record = self._drafts.get(chat_id)
        if record is None or record.draft.draft_id != draft_id:
            raise DraftNotFoundError("Dieser Entwurf existiert nicht mehr.")
        draft = record.draft
        if draft.status is DraftStatus.SENT:
            raise DraftAlreadySentError("Diese E-Mail wurde bereits versendet.")
        if draft.status is DraftStatus.SENDING:
            raise DraftAlreadySentError("Diese E-Mail wird gerade versendet.")
        if draft.revision != revision:
            raise DraftOutdatedError("Der Entwurf wurde inzwischen geändert. Bitte die aktuelle Fassung bestätigen.")
        if self._clock() > draft.expires_at:
            raise DraftExpiredError("Die Freigabe ist abgelaufen.")

        # Status vor dem ersten await setzen: parallele oder wiederholte Bestätigungen scheitern.
        record.draft = replace(draft, status=DraftStatus.SENDING)
        try:
            await self._provider.send_email(draft.email)
        except Exception as exc:
            # Kein automatischer Neuversuch: Eine neue Revision erzwingt eine erneute Freigabe.
            self._issue_revision(record, draft.email)
            if isinstance(exc, EmailError):
                raise
            logger.exception("Unerwarteter Fehler beim Versand (draft_id=%s).", draft_id)
            raise EmailSendError("Der Versand ist fehlgeschlagen.") from exc

        record.draft = replace(draft, status=DraftStatus.SENT)
        logger.info(
            "E-Mail versendet (draft_id=%s, Revision=%d, Empfänger=%d).",
            draft_id,
            revision,
            len(draft.email.recipients),
        )
        return record.draft

    # --- Intern ---------------------------------------------------------------

    def _require(self, capability: EmailCapability) -> None:
        if capability not in self._provider.capabilities:
            raise EmailError(f"Der E-Mail-Provider unterstützt diese Funktion nicht ({capability.value}).")

    def _remember_listing(self, chat_id: int, summaries: list[EmailSummary]) -> None:
        self._listings[chat_id] = _Listing(
            email_ids=[s.email_id for s in summaries],
            expires_at=self._clock() + LISTING_TTL_SECONDS,
        )

    def _resolve(self, chat_id: int, number: int) -> str:
        listing = self._listings.get(chat_id)
        if listing is None or self._clock() > listing.expires_at:
            raise EmailValidationError("Bitte zuerst E-Mails auflisten oder suchen.")
        if not 1 <= number <= len(listing.email_ids):
            raise EmailValidationError(
                f"Nummer {number} gibt es in der letzten Übersicht nicht (1 bis {len(listing.email_ids)})."
            )
        return listing.email_ids[number - 1]

    def _validate_recipients(self, values: Sequence[str]) -> tuple[EmailAddress, ...]:
        values = _dedupe(values)
        if len(values) > MAX_RECIPIENTS:
            raise EmailValidationError(f"Höchstens {MAX_RECIPIENTS} Empfänger sind erlaubt.")
        result: list[EmailAddress] = []
        for value in values:
            if any(sep in value for sep in ",;"):
                raise EmailValidationError("Bitte jede Empfängeradresse einzeln angeben.")
            if not value.isprintable():  # Zeilenumbrüche o. Ä. -> mögliche Header-Injection
                raise EmailValidationError("Die Empfängeradresse enthält unzulässige Zeichen.")
            name, raw_address = parseaddr(value)
            address = normalize_email_address(raw_address or value)
            if not self._is_recipient_allowed(address):
                raise EmailValidationError(
                    f"Der Empfänger {address} ist nicht freigegeben (siehe EMAIL_ALLOWED_RECIPIENTS)."
                )
            if any(existing.address.lower() == address.lower() for existing in result):
                continue
            clean_name = " ".join(name.split())
            if len(clean_name) > 100:
                clean_name = ""
            result.append(EmailAddress(address=address, name=clean_name))
        return tuple(result)

    def _is_recipient_allowed(self, address: str) -> bool:
        if not self._allowed_recipients:
            return True
        address = address.lower()
        domain = "@" + address.rpartition("@")[2]
        return any(entry == address or entry == domain for entry in self._allowed_recipients)

    @staticmethod
    def _validated_email(
        *, to: tuple[EmailAddress, ...], cc: tuple[EmailAddress, ...], subject: str, body: str
    ) -> OutgoingEmail:
        if not to:
            raise EmailValidationError("Mindestens ein Empfänger ist erforderlich.")
        if len(to) + len(cc) > MAX_RECIPIENTS:
            raise EmailValidationError(f"Höchstens {MAX_RECIPIENTS} Empfänger sind erlaubt.")
        clean_subject = " ".join(subject.split())  # keine Zeilenumbrüche im Header
        if not clean_subject:
            raise EmailValidationError("Der Betreff darf nicht leer sein.")
        if len(clean_subject) > MAX_SUBJECT_CHARS:
            raise EmailValidationError(f"Der Betreff darf höchstens {MAX_SUBJECT_CHARS} Zeichen lang sein.")
        clean_body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not clean_body:
            raise EmailValidationError("Der Text der E-Mail darf nicht leer sein.")
        if len(clean_body) > MAX_DRAFT_BODY_CHARS:
            raise EmailValidationError(f"Der Text darf höchstens {MAX_DRAFT_BODY_CHARS} Zeichen lang sein.")
        return OutgoingEmail(to=to, cc=cc, subject=clean_subject, body=clean_body)

    def _store_new_draft(self, chat_id: int, email: OutgoingEmail) -> EmailDraft:
        existing = self._drafts.get(chat_id)
        if existing is not None and existing.draft.status is DraftStatus.SENDING:
            raise EmailValidationError("Bitte warten, der vorherige Entwurf wird gerade versendet.")
        draft = EmailDraft(
            draft_id=secrets.token_hex(6),
            revision=1,
            email=email,
            status=DraftStatus.PENDING,
            expires_at=self._clock() + self._draft_ttl_seconds,
            ttl_seconds=self._draft_ttl_seconds,
        )
        self._drafts[chat_id] = _DraftRecord(draft=draft, needs_review=True)
        logger.info("E-Mail-Entwurf erstellt (draft_id=%s, Empfänger=%d).", draft.draft_id, len(email.recipients))
        return draft

    def _issue_revision(self, record: _DraftRecord, email: OutgoingEmail) -> EmailDraft:
        record.draft = replace(
            record.draft,
            email=email,
            revision=record.draft.revision + 1,
            status=DraftStatus.PENDING,
            expires_at=self._clock() + self._draft_ttl_seconds,
        )
        record.needs_review = True
        return record.draft

    def _pending_record(self, chat_id: int) -> _DraftRecord:
        record = self._drafts.get(chat_id)
        if record is None or record.draft.status is not DraftStatus.PENDING:
            raise DraftNotFoundError("Es gibt keinen offenen E-Mail-Entwurf.")
        return record


def _clamp(limit: int) -> int:
    return max(1, min(MAX_RESULTS, limit))


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _without(addresses: tuple[EmailAddress, ...], exclude: tuple[EmailAddress, ...]) -> tuple[EmailAddress, ...]:
    excluded = {a.address.lower() for a in exclude}
    return tuple(a for a in addresses if a.address.lower() not in excluded)


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(value.strip())
    return result
