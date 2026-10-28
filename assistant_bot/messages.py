"""Gemeinsames Nachrichtenmodell für Conversation-Service und LLM-Service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


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

    def to_api_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            data["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        return data
