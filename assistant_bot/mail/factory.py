"""Auswahl des E-Mail-Providers anhand der Konfiguration (EMAIL_PROVIDER)."""

from __future__ import annotations

from ..config import EmailSettings
from .provider import EmailProvider
from .yahoo import YahooEmailProvider


def create_email_provider(settings: EmailSettings) -> EmailProvider:
    """Erzeugt den konfigurierten Provider.

    Neue Provider (z. B. "gmail", "outlook") werden hier ergänzt und in
    ``config.SUPPORTED_EMAIL_PROVIDERS`` eingetragen – der ``EmailService``
    bleibt unverändert.
    """
    if settings.provider == "yahoo":
        return YahooEmailProvider(
            address=settings.address,
            app_password=settings.app_password,
            sender_name=settings.sender_name,
            timeout_seconds=settings.timeout_seconds,
        )
    raise ValueError(f"Unbekannter E-Mail-Provider: {settings.provider!r}")
