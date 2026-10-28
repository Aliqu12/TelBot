"""Tests für den YahooEmailProvider mit gefälschten IMAP-/SMTP-Verbindungen.

Es wird keine echte Verbindung aufgebaut und nichts versendet.
"""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
from datetime import date
from email.message import EmailMessage as MimeMessage

import pytest

from assistant_bot.mail.models import (
    EmailAddress,
    EmailAuthenticationError,
    EmailConnectionError,
    EmailNotFoundError,
    EmailSearchQuery,
    EmailSendError,
    EmailTimeoutError,
    OutgoingEmail,
)
from assistant_bot.mail.yahoo import YahooEmailProvider, _quote

PASSWORD = "dummy-app-password"


def raw_email(uid: int, sender: str, subject: str, body: str = "Text") -> bytes:
    msg = MimeMessage()
    msg["From"] = sender
    msg["To"] = "ich@yahoo.de"
    msg["Subject"] = subject
    msg["Date"] = f"Mon, {uid:02d} Jan 2025 10:00:00 +0100"
    msg["Message-ID"] = f"<m{uid}@example.de>"
    msg.set_content(body)
    return msg.as_bytes()


MAILBOX = {
    1: (raw_email(1, "Amazon <versand@amazon.de>", "Bestellung versendet"), True),
    2: (raw_email(2, "Jürgen Müller <j@example.de>", "Grüße"), False),
    3: (raw_email(3, "Bank <info@bank.de>", "Kontoauszug", "Ihr neuer Auszug"), False),
}


class FakeIMAP:
    def __init__(self, mailbox: dict[int, tuple[bytes, bool]], *, login_error: Exception | None = None) -> None:
        self.mailbox = mailbox
        self.login_error = login_error
        self.commands: list[tuple] = []
        self.literal: bytes | None = None
        self.search_literals: list[bytes | None] = []
        self.selected: tuple[str, bool] | None = None
        self.logged_in_with: tuple[str, str] | None = None
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if self.login_error:
            raise self.login_error
        self.logged_in_with = (user, password)
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected = (mailbox, readonly)
        return "OK", [str(len(self.mailbox)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list]:
        self.commands.append((command, *args))
        if command == "SEARCH":
            self.search_literals.append(self.literal)
            self.literal = None
            return "OK", [" ".join(str(uid) for uid in sorted(self.mailbox)).encode()]
        if command == "FETCH":
            uid_set, items = args
            data: list = []
            for seq, uid_text in enumerate(uid_set.split(","), start=1):
                uid = int(uid_text)
                if uid not in self.mailbox:
                    continue
                raw, seen = self.mailbox[uid]
                flags = b"\\Seen" if seen else b""
                data.append((b"%d (UID %d FLAGS (%s) BODY[] {%d}" % (seq, uid, flags, len(raw)), raw))
                data.append(b")")
            return "OK", data
        raise AssertionError(f"unerwarteter Befehl {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", []


class FakeSMTP:
    def __init__(self, *, login_error: Exception | None = None, send_error: Exception | None = None) -> None:
        self.login_error = login_error
        self.send_error = send_error
        self.sent: list = []
        self.closed = False

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def login(self, user: str, password: str) -> None:
        if self.login_error:
            raise self.login_error

    def send_message(self, message: object) -> dict:
        if self.send_error:
            raise self.send_error
        self.sent.append(message)
        return {}


def make_provider(imap: FakeIMAP | None = None, smtp: FakeSMTP | None = None, **kwargs: object) -> YahooEmailProvider:
    imap = imap or FakeIMAP(MAILBOX)
    smtp = smtp or FakeSMTP()
    return YahooEmailProvider(
        address="ich@yahoo.de",
        app_password=PASSWORD,
        sender_name="Ali",
        timeout_seconds=10,
        imap_factory=kwargs.get("imap_factory") or (lambda: imap),  # type: ignore[arg-type]
        smtp_factory=kwargs.get("smtp_factory") or (lambda: smtp),  # type: ignore[arg-type]
    )


# --- Lesen und Suchen ---------------------------------------------------------


def test_list_emails_newest_first_readonly_and_peek() -> None:
    imap = FakeIMAP(MAILBOX)
    summaries = asyncio.run(make_provider(imap).list_emails(limit=3))

    assert [s.email_id for s in summaries] == ["3", "2", "1"]
    assert summaries[0].subject == "Kontoauszug"
    assert summaries[1].sender.name == "Jürgen Müller"
    assert [s.unread for s in summaries] == [True, True, False]  # 1 ist \\Seen
    assert imap.selected == ("INBOX", True)  # EXAMINE: keine Flags ändern
    assert imap.logged_in_with == ("ich@yahoo.de", PASSWORD)
    assert imap.logged_out
    assert imap.commands[0] == ("SEARCH", "ALL")
    fetch = imap.commands[1]
    assert fetch[1] == "3,2,1" and "BODY.PEEK" in fetch[2]


def test_unread_list_uses_unseen() -> None:
    imap = FakeIMAP(MAILBOX)
    asyncio.run(make_provider(imap).list_emails(limit=5, unread_only=True))
    assert imap.commands[0] == ("SEARCH", "UNSEEN")


def test_ascii_search_is_done_on_server() -> None:
    imap = FakeIMAP(MAILBOX)
    query = EmailSearchQuery(sender="Amazon", subject='Bestell"ung', since=date(2025, 1, 6), limit=5)
    asyncio.run(make_provider(imap).search_emails(query))

    assert imap.commands[0] == (
        "SEARCH",
        "SINCE",
        "06-Jan-2025",
        "FROM",
        '"Amazon"',
        "SUBJECT",
        '"Bestell\\"ung"',
    )


def test_non_ascii_sender_is_filtered_locally() -> None:
    imap = FakeIMAP(MAILBOX)
    result = asyncio.run(make_provider(imap).search_emails(EmailSearchQuery(sender="müller")))

    assert [s.email_id for s in result] == ["2"]
    assert imap.commands[0] == ("SEARCH", "ALL")


def test_non_ascii_fulltext_search_uses_utf8_literal() -> None:
    imap = FakeIMAP(MAILBOX)
    asyncio.run(make_provider(imap).search_emails(EmailSearchQuery(text="Grüße")))

    assert imap.commands[0] == ("SEARCH", "CHARSET", "UTF-8", "TEXT")
    assert imap.search_literals == ["Grüße".encode()]


def test_quote_prevents_command_injection() -> None:
    assert _quote('a"b\\c\r\nX LOGOUT') == '"a\\"b\\\\c X LOGOUT"'


def test_get_email_returns_content() -> None:
    message = asyncio.run(make_provider().get_email("3"))

    assert message.summary.subject == "Kontoauszug"
    assert message.body.strip() == "Ihr neuer Auszug"
    assert message.message_id == "<m3@example.de>"


def test_get_email_rejects_invalid_id_without_connecting() -> None:
    def factory() -> FakeIMAP:
        raise AssertionError("darf nicht verbinden")

    with pytest.raises(EmailNotFoundError):
        asyncio.run(make_provider(imap_factory=factory).get_email("1:*"))


def test_get_email_unknown_uid() -> None:
    with pytest.raises(EmailNotFoundError):
        asyncio.run(make_provider().get_email("99"))


# --- Fehlerbehandlung IMAP ----------------------------------------------------


def test_login_failure_maps_to_authentication_error_without_secret() -> None:
    imap = FakeIMAP(MAILBOX, login_error=imaplib.IMAP4.error("AUTHENTICATIONFAILED"))
    with pytest.raises(EmailAuthenticationError) as exc_info:
        asyncio.run(make_provider(imap).list_emails(limit=5))
    assert PASSWORD not in str(exc_info.value)
    assert imap.logged_out


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), EmailTimeoutError),
        (ConnectionRefusedError(), EmailConnectionError),
        (imaplib.IMAP4.abort("socket error"), EmailConnectionError),
    ],
)
def test_connection_errors_are_translated(error: Exception, expected: type[Exception]) -> None:
    def factory() -> FakeIMAP:
        raise error

    with pytest.raises(expected):
        asyncio.run(make_provider(imap_factory=factory).list_emails(limit=5))


# --- Versand ------------------------------------------------------------------


OUTGOING = OutgoingEmail(
    to=(EmailAddress("max@example.de", "Max"),),
    subject="Grüße",
    body="Hallo Max,\nviele Grüße",
    in_reply_to="<m1@example.de>",
    references=("<m1@example.de>",),
)


def test_send_email_builds_correct_message() -> None:
    smtp = FakeSMTP()
    asyncio.run(make_provider(smtp=smtp).send_email(OUTGOING))

    assert len(smtp.sent) == 1 and smtp.closed
    message = smtp.sent[0]
    assert "ich@yahoo.de" in str(message["From"]) and "Ali" in str(message["From"])
    assert "max@example.de" in str(message["To"])
    assert str(message["Subject"]) == "Grüße"
    assert message["In-Reply-To"] == "<m1@example.de>"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), EmailAuthenticationError),
        (smtplib.SMTPRecipientsRefused({"max@example.de": (550, b"no")}), EmailSendError),
        (smtplib.SMTPDataError(554, b"spam"), EmailSendError),
        (smtplib.SMTPServerDisconnected("weg"), EmailConnectionError),
        (TimeoutError(), EmailTimeoutError),
        (OSError("unreachable"), EmailConnectionError),
    ],
)
def test_send_errors_are_translated(error: Exception, expected: type[Exception]) -> None:
    smtp = FakeSMTP(send_error=error) if not isinstance(error, smtplib.SMTPAuthenticationError) else FakeSMTP(
        login_error=error
    )
    with pytest.raises(expected) as exc_info:
        asyncio.run(make_provider(smtp=smtp).send_email(OUTGOING))
    assert PASSWORD not in str(exc_info.value)
    assert smtp.sent == []
