"""MIME-Verarbeitung nach RFC 5322: E-Mails parsen und erstellen.

Unabhängig vom Provider nutzbar, sobald dieser Roh-Nachrichten liefert bzw.
annimmt (IMAP/SMTP). Unterstützt Unicode, RFC-2047-kodierte Header und
HTML-Mails, die sicher in reinen Text umgewandelt werden.
"""

from __future__ import annotations

import email.policy
import re
from datetime import datetime
from email.errors import HeaderParseError
from email.header import Header, decode_header, make_header
from email.headerregistry import Address
from email.message import EmailMessage as MimeMessage
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser

from .models import AttachmentInfo, EmailAddress, EmailMessage, EmailSummary, OutgoingEmail

_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


# --- Parsen -------------------------------------------------------------------


def parse_summary(email_id: str, raw_headers: bytes, *, unread: bool) -> EmailSummary:
    headers = BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw_headers)
    return _summary_from_headers(email_id, headers, unread=unread)


def parse_message(email_id: str, raw: bytes, *, unread: bool, truncated: bool = False) -> EmailMessage:
    # Header robust mit compat32 dekodieren, Body komfortabel mit der modernen Policy.
    headers = BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    assert isinstance(message, MimeMessage)
    return EmailMessage(
        summary=_summary_from_headers(email_id, headers, unread=unread),
        to=_addresses(headers, "To"),
        cc=_addresses(headers, "Cc"),
        reply_to=_addresses(headers, "Reply-To"),
        body=_extract_body(message),
        attachments=_extract_attachments(message),
        message_id=_message_id(headers.get("Message-ID")),
        references=tuple(_message_ids(headers.get("References"))),
        truncated=truncated,
    )


def _summary_from_headers(email_id: str, headers: Message, *, unread: bool) -> EmailSummary:
    senders = _addresses(headers, "From")
    return EmailSummary(
        email_id=email_id,
        sender=senders[0] if senders else EmailAddress(address="", name="(unbekannt)"),
        subject=_decode(headers.get("Subject")) or "(kein Betreff)",
        date=_parse_date(headers.get("Date")),
        unread=unread,
    )


def _decode(value: object) -> str:
    if value is None:
        return ""
    raw = _raw_header(value)
    try:
        text = str(make_header(decode_header(raw)))
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        text = raw
    return _clean_inline(text)


def _raw_header(value: object) -> str:
    """Header als Text. Unkodierte 8-Bit-Header (häufig UTF-8) werden lesbar dekodiert."""
    if not isinstance(value, Header):
        return str(value)
    pieces = []
    for data, charset in decode_header(value):
        if isinstance(data, str):
            pieces.append(data)
            continue
        for candidate in (charset, "utf-8", "latin-1"):
            if candidate and candidate != "unknown-8bit":
                try:
                    pieces.append(data.decode(candidate))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
    return "".join(pieces)


def _clean_inline(text: str) -> str:
    """Einzeilig, ohne Steuerzeichen – verhindert u. a. vorgetäuschte Zeilen in Übersichten."""
    text = text.translate(_INVISIBLE_CHARS)
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def _addresses(headers: Message, name: str) -> tuple[EmailAddress, ...]:
    values = [_raw_header(value) for value in headers.get_all(name, [])]
    result = []
    for display_name, address in getaddresses(values):
        address = _clean_inline(address)
        if address:
            result.append(EmailAddress(address=address, name=_decode(display_name)))
    return tuple(result)


def _parse_date(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None


def _message_ids(value: object) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value)) if value else []


def _message_id(value: object) -> str | None:
    ids = _message_ids(value)
    return ids[0] if ids else None


def _extract_body(message: MimeMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, ValueError, KeyError):
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return normalize_text(content)


def _extract_attachments(message: MimeMessage) -> tuple[AttachmentInfo, ...]:
    attachments = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True)
        attachments.append(
            AttachmentInfo(
                filename=_clean_inline(part.get_filename() or "") or "(ohne Namen)",
                content_type=part.get_content_type(),
                size_bytes=len(payload) if isinstance(payload, bytes) else 0,
            )
        )
    return tuple(attachments)


def normalize_text(text: str) -> str:
    """Einheitliche Zeilenumbrüche, keine Steuerzeichen, höchstens eine Leerzeile am Stück."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").translate(_INVISIBLE_CHARS)
    lines = []
    for line in text.split("\n"):
        cleaned = "".join(ch for ch in line if ch.isprintable() or ch == "\t").rstrip()
        if cleaned or (lines and lines[-1]):
            lines.append(cleaned)
    return "\n".join(lines).strip()


class _HtmlTextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "head", "title", "noscript", "template", "svg", "iframe", "object"})
    _BLOCK = frozenset(
        {"p", "div", "br", "tr", "table", "ul", "ol", "li", "blockquote", "section", "article",
         "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._BLOCK:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Wandelt HTML in lesbaren Text um. Skripte, Styles und eingebettete Inhalte entfallen,
    externe Ressourcen werden nie geladen."""
    parser = _HtmlTextExtractor()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return normalize_text("\n".join(lines))


# --- Erstellen ----------------------------------------------------------------


def build_mime_message(outgoing: OutgoingEmail, sender: EmailAddress) -> MimeMessage:
    """Erstellt eine standardkonforme UTF-8-Nachricht (Header nach RFC 2047 kodiert)."""
    message = MimeMessage(policy=email.policy.SMTP)
    message["From"] = _to_header_address(sender)
    message["To"] = tuple(_to_header_address(a) for a in outgoing.to)
    if outgoing.cc:
        message["Cc"] = tuple(_to_header_address(a) for a in outgoing.cc)
    message["Subject"] = outgoing.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.address.rpartition("@")[2])
    if outgoing.in_reply_to:
        message["In-Reply-To"] = outgoing.in_reply_to
        message["References"] = " ".join(outgoing.references or (outgoing.in_reply_to,))
    message.set_content(outgoing.body.replace("\r\n", "\n"), charset="utf-8")
    return message


def _to_header_address(address: EmailAddress) -> Address:
    return Address(display_name=address.name, addr_spec=address.address)
