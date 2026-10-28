"""Tests für EmailService: Nummerierung, Validierung, Entwürfe und Versandfreigabe."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

import pytest

from assistant_bot.config import EmailSettings
from assistant_bot.mail.factory import create_email_provider
from assistant_bot.mail.models import (
    EmailAddress,
    EmailCapability,
    EmailMessage,
    EmailNotFoundError,
    EmailSearchQuery,
    EmailSendError,
    EmailSummary,
    EmailTimeoutError,
    EmailValidationError,
    OutgoingEmail,
)
from assistant_bot.mail.service import (
    DraftAlreadySentError,
    DraftExpiredError,
    DraftNotFoundError,
    DraftOutdatedError,
    DraftStatus,
    EmailService,
)
from assistant_bot.mail.yahoo import YahooEmailProvider

CHAT = 42


def summary(email_id: str, subject: str = "Betreff", sender: str = "info@amazon.de") -> EmailSummary:
    return EmailSummary(
        email_id=email_id,
        sender=EmailAddress(sender, "Absender"),
        subject=subject,
        date=datetime(2025, 1, 6, 10, 0),
        unread=True,
    )


class FakeProvider:
    name = "fake"

    def __init__(self, capabilities: frozenset[EmailCapability] = frozenset(EmailCapability)) -> None:
        self.capabilities = capabilities
        self.summaries = [summary("30", "Neueste"), summary("20", "Mittel"), summary("10", "Alt")]
        self.messages: dict[str, EmailMessage] = {}
        self.sent: list[OutgoingEmail] = []
        self.send_error: Exception | None = None
        self.send_delay = 0.0
        self.queries: list[EmailSearchQuery] = []

    async def list_emails(self, limit: int, unread_only: bool = False) -> list[EmailSummary]:
        return self.summaries[:limit]

    async def search_emails(self, query: EmailSearchQuery) -> list[EmailSummary]:
        self.queries.append(query)
        return self.summaries[: query.limit]

    async def get_email(self, email_id: str) -> EmailMessage:
        if email_id not in self.messages:
            raise EmailNotFoundError("nicht gefunden")
        return self.messages[email_id]

    async def send_email(self, email: OutgoingEmail) -> None:
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.send_error:
            raise self.send_error
        self.sent.append(email)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_service(**kwargs: object) -> tuple[EmailService, FakeProvider, Clock]:
    provider = FakeProvider()
    clock = Clock()
    service = EmailService(provider, clock=clock, **kwargs)  # type: ignore[arg-type]
    return service, provider, clock


def message(email_id: str, body: str = "Hallo", reply_to: tuple[EmailAddress, ...] = ()) -> EmailMessage:
    return EmailMessage(
        summary=summary(email_id, "Rechnung"),
        to=(EmailAddress("ich@yahoo.de"),),
        cc=(),
        reply_to=reply_to,
        body=body,
        attachments=(),
        message_id=f"<{email_id}@amazon.de>",
        references=("<alt@amazon.de>",),
    )


def draft(service: EmailService, to: list[str] | None = None) -> object:
    return service.create_draft(CHAT, to=to or ["max@example.de"], subject="Hallo", body="Text")


# --- Lesen --------------------------------------------------------------------


def test_read_resolves_number_from_last_listing() -> None:
    service, provider, _ = make_service()
    provider.messages["20"] = message("20")

    asyncio.run(service.list_recent(CHAT, limit=3))
    result = asyncio.run(service.read(CHAT, 2))

    assert result.summary.email_id == "20"


@pytest.mark.parametrize("number", [0, 4])
def test_read_rejects_unknown_number(number: int) -> None:
    service, _, _ = make_service()
    asyncio.run(service.list_recent(CHAT, limit=3))
    with pytest.raises(EmailValidationError):
        asyncio.run(service.read(CHAT, number))


def test_read_requires_listing_and_listings_are_per_chat() -> None:
    service, _, _ = make_service()
    asyncio.run(service.list_recent(CHAT))
    with pytest.raises(EmailValidationError):
        asyncio.run(service.read(999, 1))


def test_read_truncates_long_body() -> None:
    service, provider, _ = make_service(max_body_chars=500)
    provider.messages["30"] = message("30", body="x" * 2000)
    asyncio.run(service.list_recent(CHAT))

    result = asyncio.run(service.read(CHAT, 1))

    assert len(result.body) == 500 and result.truncated


def test_search_cleans_query_and_clamps_limit() -> None:
    service, provider, _ = make_service()
    asyncio.run(service.search(CHAT, EmailSearchQuery(sender=" Amazon ", subject="  ", limit=500)))

    query = provider.queries[0]
    assert query.sender == "Amazon" and query.subject is None and query.limit == 20


def test_search_rejects_overlong_terms() -> None:
    service, _, _ = make_service()
    with pytest.raises(EmailValidationError):
        asyncio.run(service.search(CHAT, EmailSearchQuery(text="x" * 500)))


# --- Entwürfe und Validierung -------------------------------------------------


@pytest.mark.parametrize(
    "to",
    [
        [],
        ["keine-adresse"],
        ["max@example.de\r\nBcc: boese@evil.de"],
        ["max@example.de, eva@example.de"],
        [f"user{i}@example.de" for i in range(11)],
    ],
)
def test_invalid_recipients_are_rejected(to: list[str]) -> None:
    service, _, _ = make_service()
    with pytest.raises(EmailValidationError):
        service.create_draft(CHAT, to=to, subject="Hallo", body="Text")


@pytest.mark.parametrize(("subject", "body"), [("", "Text"), ("Hallo", "   "), ("x" * 201, "Text")])
def test_invalid_subject_or_body_is_rejected(subject: str, body: str) -> None:
    service, _, _ = make_service()
    with pytest.raises(EmailValidationError):
        service.create_draft(CHAT, to=["max@example.de"], subject=subject, body=body)


def test_subject_newlines_are_collapsed_and_display_names_parsed() -> None:
    service, _, _ = make_service()
    created = service.create_draft(
        CHAT, to=["Max Müller <MAX@Example.de>", "max@example.de"], subject="Hallo\nWelt", body="Text"
    )
    assert created.email.subject == "Hallo Welt"
    assert created.email.to == (EmailAddress("MAX@example.de", "Max Müller"),)


def test_allowlist_restricts_recipients() -> None:
    service, _, _ = make_service(allowed_recipients=("chef@firma.de", "@example.de"))

    service.create_draft(CHAT, to=["chef@firma.de", "max@example.de"], subject="Hallo", body="Text")
    with pytest.raises(EmailValidationError, match="nicht freigegeben"):
        service.create_draft(CHAT, to=["fremd@evil.de"], subject="Hallo", body="Text")
    with pytest.raises(EmailValidationError):
        service.create_draft(CHAT, to=["max@example.de"], cc=["fremd@evil.de"], subject="Hallo", body="Text")


def test_new_draft_is_offered_for_review_exactly_once() -> None:
    service, _, _ = make_service()
    created = draft(service)

    assert service.take_draft_for_review(CHAT) == created
    assert service.take_draft_for_review(CHAT) is None


# --- Versandfreigabe ----------------------------------------------------------


def test_confirm_sends_exactly_once() -> None:
    service, provider, _ = make_service()
    created = draft(service)

    sent = asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))
    with pytest.raises(DraftAlreadySentError):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))

    assert sent.status is DraftStatus.SENT
    assert len(provider.sent) == 1
    assert provider.sent[0].to[0].address == "max@example.de"


def test_parallel_confirmations_send_only_once() -> None:
    service, provider, _ = make_service()
    provider.send_delay = 0.01
    created = draft(service)

    async def run() -> list[object]:
        return await asyncio.gather(
            service.confirm_and_send(CHAT, created.draft_id, created.revision),
            service.confirm_and_send(CHAT, created.draft_id, created.revision),
            return_exceptions=True,
        )

    results = asyncio.run(run())

    assert len(provider.sent) == 1
    assert sum(isinstance(r, DraftAlreadySentError) for r in results) == 1


def test_changed_draft_requires_new_confirmation() -> None:
    service, provider, _ = make_service()
    created = draft(service)
    updated = service.update_draft(CHAT, body="Neuer Text")

    with pytest.raises(DraftOutdatedError):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))
    assert provider.sent == []

    asyncio.run(service.confirm_and_send(CHAT, updated.draft_id, updated.revision))
    assert provider.sent[0].body == "Neuer Text"


def test_review_request_invalidates_old_buttons() -> None:
    service, _, _ = make_service()
    created = draft(service)
    service.request_review(CHAT)

    with pytest.raises(DraftOutdatedError):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))


def test_expired_confirmation_is_rejected() -> None:
    service, provider, clock = make_service(draft_ttl_seconds=60)
    created = draft(service)
    clock.now += 61

    with pytest.raises(DraftExpiredError):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))
    assert provider.sent == []


@pytest.mark.parametrize(("draft_id_ok", "chat"), [(False, CHAT), (True, 999)])
def test_unknown_draft_or_other_chat_is_rejected(draft_id_ok: bool, chat: int) -> None:
    service, provider, _ = make_service()
    created = draft(service)
    draft_id = created.draft_id if draft_id_ok else "ffffffffffff"

    with pytest.raises(DraftNotFoundError):
        asyncio.run(service.confirm_and_send(chat, draft_id, created.revision))
    assert provider.sent == []


@pytest.mark.parametrize("error", [EmailSendError("abgelehnt"), EmailTimeoutError("timeout"), RuntimeError("bug")])
def test_failed_send_requires_new_confirmation(error: Exception) -> None:
    service, provider, _ = make_service()
    created = draft(service)
    service.take_draft_for_review(CHAT)
    provider.send_error = error

    with pytest.raises((EmailSendError, EmailTimeoutError)):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))

    again = service.take_draft_for_review(CHAT)
    assert again is not None and again.revision == created.revision + 1
    with pytest.raises(DraftOutdatedError):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))

    provider.send_error = None
    asyncio.run(service.confirm_and_send(CHAT, again.draft_id, again.revision))
    assert len(provider.sent) == 1


def test_discarded_draft_cannot_be_sent() -> None:
    service, provider, _ = make_service()
    created = draft(service)

    assert service.discard_draft(CHAT, created.draft_id)
    with pytest.raises(DraftNotFoundError):
        asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))
    assert provider.sent == []


def test_sent_draft_cannot_be_updated() -> None:
    service, _, _ = make_service()
    created = draft(service)
    asyncio.run(service.confirm_and_send(CHAT, created.draft_id, created.revision))

    with pytest.raises(DraftNotFoundError):
        service.update_draft(CHAT, body="Nachtrag")


def test_reply_draft_uses_reply_to_and_threading_headers() -> None:
    service, provider, _ = make_service()
    provider.messages["30"] = message("30", reply_to=(EmailAddress("support@amazon.de"),))
    asyncio.run(service.list_recent(CHAT))

    reply = asyncio.run(service.create_reply_draft(CHAT, 1, "Danke!"))

    assert [a.address for a in reply.email.to] == ["support@amazon.de"]
    assert reply.email.subject == "Re: Rechnung"
    assert reply.email.in_reply_to == "<30@amazon.de>"
    assert reply.email.references == ("<alt@amazon.de>", "<30@amazon.de>")


def test_reply_respects_allowlist() -> None:
    service, provider, _ = make_service(allowed_recipients=("@example.de",))
    provider.messages["30"] = message("30")
    asyncio.run(service.list_recent(CHAT))

    with pytest.raises(EmailValidationError):
        asyncio.run(service.create_reply_draft(CHAT, 1, "Danke!"))


def test_missing_capability_is_rejected() -> None:
    provider = FakeProvider(frozenset({EmailCapability.READ}))
    service = EmailService(provider)
    with pytest.raises(Exception, match="unterstützt"):
        service.create_draft(CHAT, to=["max@example.de"], subject="Hallo", body="Text")


# --- Provider-Auswahl ---------------------------------------------------------

SETTINGS = EmailSettings(
    provider="yahoo",
    address="ich@yahoo.de",
    app_password="dummy",
    sender_name="Ali",
    allowed_recipients=(),
    draft_ttl_seconds=900,
    timeout_seconds=30,
    max_body_chars=6000,
)


def test_factory_creates_yahoo_provider_without_connecting() -> None:
    provider = create_email_provider(SETTINGS)
    assert isinstance(provider, YahooEmailProvider)
    assert provider.capabilities == frozenset(EmailCapability)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError):
        create_email_provider(replace(SETTINGS, provider="gmail"))
