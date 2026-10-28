"""Vertrag, den jeder E-Mail-Provider (Yahoo, später z. B. Gmail/Outlook) erfüllen muss."""

from __future__ import annotations

from typing import Protocol

from .models import EmailCapability, EmailMessage, EmailSearchQuery, EmailSummary, OutgoingEmail


class EmailProvider(Protocol):
    """Zugriff auf genau ein Postfach.

    Implementierungen übersetzen ihre technischen Fehler in die Fehlerklassen aus
    ``models`` (``EmailAuthenticationError``, ``EmailTimeoutError``, ...). Ergebnisse
    sind immer nach Datum absteigend sortiert (neueste zuerst).
    """

    name: str
    capabilities: frozenset[EmailCapability]

    async def list_emails(self, limit: int, unread_only: bool = False) -> list[EmailSummary]: ...

    async def search_emails(self, query: EmailSearchQuery) -> list[EmailSummary]: ...

    async def get_email(self, email_id: str) -> EmailMessage:
        """Raises: EmailNotFoundError, wenn die ID unbekannt ist."""
        ...

    async def send_email(self, email: OutgoingEmail) -> None: ...
