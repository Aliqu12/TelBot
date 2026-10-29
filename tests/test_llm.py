"""Tests für den LLM-Service mit gemocktem Groq-Client (keine echten API-Anfragen)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import groq
import httpx
import pytest

from assistant_bot.llm import (
    GroqChatModel,
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMRateLimitError,
    LLMRequestTooLargeError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from assistant_bot.messages import ChatMessage

_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
MESSAGES = [ChatMessage("system", "SYSTEM"), ChatMessage("user", "Hallo")]


def make_model(*, result: object = None, error: Exception | None = None) -> tuple[GroqChatModel, AsyncMock]:
    create = AsyncMock(return_value=result, side_effect=error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    model = GroqChatModel(client, model="test-model", temperature=0.5, max_completion_tokens=256)  # type: ignore[arg-type]
    return model, create


def completion(content: str | None, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)]
    )


def status_error(cls: type[groq.APIStatusError], status: int) -> groq.APIStatusError:
    return cls("error", response=httpx.Response(status, request=_REQUEST), body=None)


def test_successful_completion_returns_stripped_text_and_sends_messages() -> None:
    model, create = make_model(result=completion("  Hallo Ali!  \n"))

    answer = asyncio.run(model.complete(MESSAGES))

    assert answer == "Hallo Ali!"
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["temperature"] == 0.5
    assert kwargs["max_completion_tokens"] == 256
    assert kwargs["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Hallo"},
    ]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(groq.RateLimitError, 429), LLMRateLimitError),
        (status_error(groq.APIStatusError, 413), LLMRequestTooLargeError),
        (groq.APITimeoutError(request=_REQUEST), LLMTimeoutError),
        (groq.APIConnectionError(message="down", request=_REQUEST), LLMUnavailableError),
        (status_error(groq.InternalServerError, 503), LLMUnavailableError),
        (status_error(groq.AuthenticationError, 401), LLMConfigurationError),
        (status_error(groq.PermissionDeniedError, 403), LLMConfigurationError),
        (status_error(groq.NotFoundError, 404), LLMConfigurationError),
        (status_error(groq.BadRequestError, 400), LLMError),
    ],
)
def test_sdk_errors_are_translated(error: Exception, expected: type[LLMError]) -> None:
    model, _ = make_model(error=error)

    with pytest.raises(expected) as exc_info:
        asyncio.run(model.complete(MESSAGES))

    assert type(exc_info.value) is expected


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_response_raises(content: str | None) -> None:
    model, _ = make_model(result=completion(content))

    with pytest.raises(LLMEmptyResponseError):
        asyncio.run(model.complete(MESSAGES))


def test_missing_choices_raises() -> None:
    model, _ = make_model(result=SimpleNamespace(choices=[]))

    with pytest.raises(LLMEmptyResponseError):
        asyncio.run(model.complete(MESSAGES))


def test_aclose_closes_client() -> None:
    model, _ = make_model()

    asyncio.run(model.aclose())

    model._client.close.assert_awaited_once()  # type: ignore[attr-defined]
