from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from assistant_bot.assistant import Assistant
from assistant_bot.conversation import ConversationService
from assistant_bot.llm import LLMRateLimitError
from assistant_bot.messages import ChatMessage


def make_service(tmp_path: Path, max_history: int = 20) -> ConversationService:
    service = ConversationService(tmp_path / "sub" / "test.db", max_history)
    service.initialize()
    return service


class FakeChatModel:
    def __init__(self, answer: str = "Antwort", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.answer

    async def aclose(self) -> None:
        pass


def test_initialize_creates_database_directory(tmp_path: Path) -> None:
    make_service(tmp_path)
    assert (tmp_path / "sub" / "test.db").exists()


def test_history_is_returned_in_chronological_order(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    asyncio.run(service.add_exchange(1, "Frage 1", "Antwort 1"))
    asyncio.run(service.add_exchange(1, "Frage 2", "Antwort 2"))

    history = asyncio.run(service.get_history(1))

    assert [(m.role, m.content) for m in history] == [
        ("user", "Frage 1"),
        ("assistant", "Antwort 1"),
        ("user", "Frage 2"),
        ("assistant", "Antwort 2"),
    ]


def test_history_is_limited_and_starts_with_user_message(tmp_path: Path) -> None:
    service = make_service(tmp_path, max_history=3)
    for i in range(1, 4):
        asyncio.run(service.add_exchange(1, f"Frage {i}", f"Antwort {i}"))

    history = asyncio.run(service.get_history(1))

    # Die letzten 3 wären [Antwort 2, Frage 3, Antwort 3] -> führende Antwort wird verworfen.
    assert [m.content for m in history] == ["Frage 3", "Antwort 3"]
    assert history[0].role == "user"


def test_reset_only_affects_the_given_chat(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    asyncio.run(service.add_exchange(1, "a", "b"))
    asyncio.run(service.add_exchange(2, "c", "d"))

    deleted = asyncio.run(service.reset(1))

    assert deleted == 2
    assert asyncio.run(service.get_history(1)) == []
    assert len(asyncio.run(service.get_history(2))) == 2


def test_history_survives_restart(tmp_path: Path) -> None:
    first = make_service(tmp_path)
    asyncio.run(first.add_exchange(1, "Merk dir: 42", "Gemerkt."))

    restarted = make_service(tmp_path)  # neue Instanz, gleiche Datei

    assert [m.content for m in asyncio.run(restarted.get_history(1))] == ["Merk dir: 42", "Gemerkt."]


def test_assistant_sends_system_prompt_history_and_new_message(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    asyncio.run(service.add_exchange(1, "Hallo", "Hi!"))
    model = FakeChatModel(answer="Gut, danke.")
    assistant = Assistant(model, service, system_prompt="SYSTEM")

    answer = asyncio.run(assistant.reply(1, "Wie geht's?"))

    assert answer == "Gut, danke."
    sent = model.calls[0]
    assert [(m.role, m.content) for m in sent] == [
        ("system", "SYSTEM"),
        ("user", "Hallo"),
        ("assistant", "Hi!"),
        ("user", "Wie geht's?"),
    ]
    assert [m.content for m in asyncio.run(service.get_history(1))][-2:] == ["Wie geht's?", "Gut, danke."]


def test_failed_llm_request_does_not_change_history(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assistant = Assistant(FakeChatModel(error=LLMRateLimitError()), service, system_prompt="SYSTEM")

    with pytest.raises(LLMRateLimitError):
        asyncio.run(assistant.reply(1, "Hallo"))

    assert asyncio.run(service.get_history(1)) == []


def test_assistant_reset_clears_history(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    assistant = Assistant(FakeChatModel(), service, system_prompt="SYSTEM")
    asyncio.run(assistant.reply(1, "Hallo"))

    asyncio.run(assistant.reset(1))

    assert asyncio.run(service.get_history(1)) == []
