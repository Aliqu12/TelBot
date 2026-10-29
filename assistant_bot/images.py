"""Prüfung eingehender Bilder, bevor sie an das Vision-Modell gehen.

Der Dateityp wird anhand der ersten Bytes (Signatur) bestimmt, nicht anhand von
Dateiname oder vom Absender angegebenem MIME-Typ.
"""

from __future__ import annotations

from .messages import ImageInput

# Groq begrenzt Anfragen mit Base64-Bildern auf ca. 4 MB; Base64 vergrößert um ein Drittel.
MAX_IMAGE_BYTES = 3 * 1024 * 1024

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class ImageRejectedError(Exception):
    """Das Bild ist zu groß oder hat ein nicht unterstütztes Format (Meldung für den Nutzer)."""


def too_large_message(size: int) -> str:
    return (
        f"Das Bild ist mit {size / 1024 / 1024:.1f} MB zu groß (höchstens "
        f"{MAX_IMAGE_BYTES // 1024 // 1024} MB). Schick es bitte als normales Foto statt als Datei, "
        "dann verkleinert Telegram es automatisch."
    )


def detect_mime_type(data: bytes) -> str | None:
    for signature, mime_type in _SIGNATURES:
        if data.startswith(signature):
            return mime_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_image(data: bytes) -> ImageInput:
    """Prüft Größe und Format und liefert ein ``ImageInput``.

    Raises:
        ImageRejectedError: Mit einer verständlichen deutschen Meldung.
    """
    if not data:
        raise ImageRejectedError("Das Bild ist leer.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejectedError(too_large_message(len(data)))
    mime_type = detect_mime_type(data)
    if mime_type is None:
        raise ImageRejectedError(
            "Dieses Bildformat kann ich nicht verarbeiten. Unterstützt sind JPEG, PNG und WebP – "
            "am einfachsten schickst du es als normales Foto."
        )
    return ImageInput(data=data, mime_type=mime_type)
