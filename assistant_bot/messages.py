"""Gemeinsames Nachrichtenmodell für Conversation-Service und LLM-Service."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ImageInput:
    """Geprüftes Bild (Rohdaten + MIME-Typ), das an ein Vision-Modell geht."""

    data: bytes = field(repr=False)
    mime_type: str

    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{base64.b64encode(self.data).decode('ascii')}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Vom Modell angeforderter Werkzeugaufruf (Argumente als JSON-Text)."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    # Nur für Nutzernachrichten an ein Vision-Modell; wird nie im Verlauf gespeichert.
    images: tuple[ImageInput, ...] = ()

    def to_api_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.images:
            data["content"] = [
                {"type": "text", "text": self.content},
                *({"type": "image_url", "image_url": {"url": image.data_url()}} for image in self.images),
            ]
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        return data
