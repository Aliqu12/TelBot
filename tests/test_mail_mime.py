"""Tests für Adressprüfung, MIME-Verarbeitung und HTML-zu-Text."""

from __future__ import annotations

from email import message_from_bytes, policy
from email.message import EmailMessage as MimeMessage

import pytest

from assistant_bot.mail.mime import build_mime_message, html_to_text, parse_message, parse_summary
from assistant_bot.mail.models import EmailAddress, EmailValidationError, OutgoingEmail, normalize_email_address


def raw_email(
    *,
    sender: str = "Amazon <versand@amazon.de>",
    subject: str = "Ihre Bestellung",
    body: str = "Hallo Ali",
    html: str | None = None,
    attachment: bytes | None = None,
) -> bytes:
    msg = MimeMessage()
    msg["From"] = sender
    msg["To"] = "ich@yahoo.de"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 06 Jan 2025 10:00:00 +0100"
    msg["Message-ID"] = "<abc123@amazon.de>"
    if html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body)
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename="Rechnung.pdf")
    return msg.as_bytes()


# --- Adressen -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Max@Example.DE", "Max@example.de"),
        ("  a.b+c@sub.example.org ", "a.b+c@sub.example.org"),
        ("info@bücher.de", "info@xn--bcher-kva.de"),
    ],
)
def test_valid_addresses_are_normalized(value: str, expected: str) -> None:
    assert normalize_email_address(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "keine-adresse", "a@b", "a b@example.de", "a@example.de\r\nBcc: x@evil.de", "@example.de", "a@-x.de"],
)
def test_invalid_addresses_are_rejected(value: str) -> None:
    with pytest.raises(EmailValidationError):
        normalize_email_address(value)


# --- Parsen -------------------------------------------------------------------


def test_summary_decodes_encoded_umlaut_subject() -> None:
    raw = (
        b"From: =?utf-8?q?J=C3=BCrgen_M=C3=BCller?= <juergen@example.de>\r\n"
        b"Subject: =?utf-8?q?Gr=C3=BC=C3=9Fe_aus_K=C3=B6ln?=\r\n"
        b"Date: Tue, 07 Jan 2025 08:30:00 +0000\r\n\r\n"
    )
    summary = parse_summary("42", raw, unread=True)

    assert summary.email_id == "42"
    assert summary.subject == "Grüße aus Köln"
    assert summary.sender.name == "Jürgen Müller"
    assert summary.sender.address == "juergen@example.de"
    assert summary.date is not None and summary.date.day == 7
    assert summary.unread is True


def test_header_control_characters_are_removed() -> None:
    raw = b"From: a@example.de\r\nSubject: Hallo\x07 \xe2\x80\x8bWelt\r\n\r\n"
    assert parse_summary("1", raw, unread=False).subject == "Hallo Welt"


def test_unencoded_utf8_headers_are_decoded() -> None:
    raw = "From: Jürgen <j@example.de>\r\nSubject: Grüße\r\n\r\n".encode()
    summary = parse_summary("1", raw, unread=False)
    assert summary.subject == "Grüße" and summary.sender.name == "Jürgen"


def test_message_with_plain_text_and_attachment() -> None:
    message = parse_message("7", raw_email(body="Ihre Bestellung über 12,99 € ist unterwegs.", attachment=b"%PDF"), unread=False)

    assert "12,99 €" in message.body
    assert message.message_id == "<abc123@amazon.de>"
    assert [a.address for a in message.to] == ["ich@yahoo.de"]
    assert len(message.attachments) == 1
    assert message.attachments[0].filename == "Rechnung.pdf"
    assert message.attachments[0].content_type == "application/pdf"


def test_html_only_message_is_converted_to_safe_text() -> None:
    html = (
        "<html><head><style>p{color:red}</style><title>X</title></head><body>"
        "<script>alert('x')</script><p>Hallo&nbsp;Ali,</p><p>Ihr Paket &amp; mehr</p>"
        "<ul><li>Eins</li><li>Zwei</li></ul></body></html>"
    )
    message = parse_message("3", raw_email(html=html), unread=True)

    assert "alert" not in message.body
    assert "color" not in message.body
    assert "<p>" not in message.body
    assert "Hallo Ali," in message.body
    assert "Ihr Paket & mehr" in message.body
    assert "- Eins" in message.body


def test_html_to_text_keeps_paragraphs() -> None:
    text = html_to_text("<div>Zeile 1</div><div>Zeile 2<br>Zeile 3</div>")
    assert [line for line in text.splitlines() if line] == ["Zeile 1", "Zeile 2", "Zeile 3"]


def test_broken_mime_does_not_crash() -> None:
    message = parse_message("9", b"Subject: kaputt\r\nContent-Type: multipart/mixed\r\n\r\nohne boundary", unread=False)
    assert message.summary.subject == "kaputt"


# --- Erzeugen -----------------------------------------------------------------


def test_build_mime_message_roundtrip_with_umlauts() -> None:
    outgoing = OutgoingEmail(
        to=(EmailAddress("max@example.de", "Max Müller"),),
        cc=(EmailAddress("eva@example.de"),),
        subject="Grüße & Termin",
        body="Hallo Max,\n\nschöne Grüße!\nÄÖÜ ß",
        in_reply_to="<orig@example.de>",
        references=("<first@example.de>", "<orig@example.de>"),
    )
    built = build_mime_message(outgoing, EmailAddress("ich@yahoo.de", "Ali Khan"))
    reparsed = message_from_bytes(built.as_bytes(), policy=policy.default)

    assert str(reparsed["Subject"]) == "Grüße & Termin"
    assert "ich@yahoo.de" in str(reparsed["From"])
    assert "Max Müller" in str(reparsed["To"])
    assert "eva@example.de" in str(reparsed["Cc"])
    assert reparsed["In-Reply-To"] == "<orig@example.de>"
    assert "<first@example.de>" in reparsed["References"]
    assert reparsed["Message-ID"].endswith("@yahoo.de>")
    assert reparsed.get_content().replace("\r\n", "\n").strip() == "Hallo Max,\n\nschöne Grüße!\nÄÖÜ ß"
    assert "Bcc" not in reparsed
