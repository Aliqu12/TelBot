"""Tests für die LLM-Schicht mit gemocktem Client (keine echten API-Anfragen)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest

from assistant_bot.config import load_settings
from assistant_bot.llm import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMInsufficientCreditsError,
    LLMRateLimitError,
    LLMRequestTooLargeError,
    LLMTimeoutError,
    LLMUnavailableError,
    OpenAICompatibleChatModel,
    create_chat_model,
    create_image_model,
)
from assistant_bot.messages import ChatMessage

_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
MESSAGES = [ChatMessage("system", "SYSTEM"), ChatMessage("user", "Hallo")]


def make_model(
    *, result: object = None, error: Exception | None = None, **options: object
) -> tuple[OpenAICompatibleChatModel, AsyncMock]:
    create = AsyncMock(return_value=result, side_effect=error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    model = OpenAICompatibleChatModel(
        client,  # type: ignore[arg-type]
        model="test-model",
        temperature=0.5,
        max_tokens=256,
        **options,  # type: ignore[arg-type]
    )
    return model, create


def completion(content: str | None, finish_reason: str = "stop", **extra: object) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason, **extra)]
    )


def status_error(status: int, message: str = "error") -> openai.APIStatusError:
    response = httpx2.Response(status, request=_REQUEST)
    return openai.APIStatusError(message, response=response, body=None)


def test_successful_completion_returns_stripped_text_and_sends_messages() -> None:
    model, create = make_model(result=completion("  Hallo Ali!  \n"))

    answer = asyncio.run(model.complete(MESSAGES))

    assert answer == "Hallo Ali!"
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["temperature"] == 0.5
    assert kwargs["max_tokens"] == 256
    assert kwargs["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Hallo"},
    ]


def test_max_tokens_parameter_name_is_configurable() -> None:
    model, create = make_model(result=completion("ok"), max_tokens_param="max_completion_tokens")

    asyncio.run(model.complete(MESSAGES))

    kwargs = create.await_args.kwargs
    assert kwargs["max_completion_tokens"] == 256
    assert "max_tokens" not in kwargs
    assert "extra_body" not in kwargs


def test_extra_body_is_sent_with_the_request() -> None:
    body = {"reasoning": {"enabled": False}}
    model, create = make_model(result=completion("ok"), extra_body=body)

    asyncio.run(model.complete(MESSAGES))

    assert create.await_args.kwargs["extra_body"] == body


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(429), LLMRateLimitError),
        (status_error(413), LLMRequestTooLargeError),
        (status_error(402), LLMInsufficientCreditsError),
        (status_error(408), LLMTimeoutError),
        (openai.APITimeoutError(request=_REQUEST), LLMTimeoutError),
        (openai.APIConnectionError(message="down", request=_REQUEST), LLMUnavailableError),
        (status_error(502), LLMUnavailableError),
        (status_error(503), LLMUnavailableError),
        (status_error(401), LLMConfigurationError),
        (status_error(404), LLMConfigurationError),
        (status_error(403), LLMError),
        (status_error(400), LLMError),
    ],
    ids=["429", "413", "402", "408", "timeout", "connection", "502", "503", "401", "404", "403", "400"],
)
def test_sdk_errors_are_translated(error: Exception, expected: type[LLMError]) -> None:
    model, _ = make_model(error=error)

    with pytest.raises(expected) as exc_info:
        asyncio.run(model.complete(MESSAGES))

    assert type(exc_info.value) is expected


def test_error_detail_of_oversized_request_is_logged_without_leaking_anything_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model, _ = make_model(error=status_error(413, "Limit 8000, angefragt 9500"), provider_name="OpenRouter")

    with caplog.at_level("WARNING"), pytest.raises(LLMRequestTooLargeError):
        asyncio.run(model.complete(MESSAGES))

    assert "OpenRouter" in caplog.text
    assert "Limit 8000, angefragt 9500" in caplog.text


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_response_raises(content: str | None) -> None:
    model, _ = make_model(result=completion(content))

    with pytest.raises(LLMEmptyResponseError):
        asyncio.run(model.complete(MESSAGES))


def test_missing_choices_raises() -> None:
    model, _ = make_model(result=SimpleNamespace(choices=[]))

    with pytest.raises(LLMEmptyResponseError):
        asyncio.run(model.complete(MESSAGES))


def test_error_object_in_successful_response_is_unavailable_error() -> None:
    body = SimpleNamespace(choices=None, error={"code": 502, "message": "Provider disconnected"})
    model, _ = make_model(result=body)

    with pytest.raises(LLMUnavailableError):
        asyncio.run(model.complete(MESSAGES))


def test_finish_reason_error_in_choice_is_unavailable_error() -> None:
    result = completion(
        "", finish_reason="error", error=SimpleNamespace(code=502, message="Upstream failed")
    )
    model, _ = make_model(result=result)

    with pytest.raises(LLMUnavailableError):
        asyncio.run(model.complete(MESSAGES))


def test_reasoning_only_answer_cut_by_token_limit_hints_at_max_tokens(caplog: pytest.LogCaptureFixture) -> None:
    model, _ = make_model(result=completion("", "length"))

    with caplog.at_level("WARNING"), pytest.raises(LLMEmptyResponseError):
        asyncio.run(model.complete(MESSAGES))

    assert "LLM_MAX_TOKENS" in caplog.text


def test_reasoning_block_is_stripped_from_answer() -> None:
    model, _ = make_model(result=completion("<think>Ich überlege …</think>\n\nRechnung Nr. 4711"))

    assert asyncio.run(model.complete(MESSAGES)) == "Rechnung Nr. 4711"


def test_truncated_reasoning_only_counts_as_empty_answer() -> None:
    model, _ = make_model(result=completion("<think>Ich überlege und werde abgeschnit", "length"))

    with pytest.raises(LLMEmptyResponseError):
        asyncio.run(model.complete(MESSAGES))


def test_aclose_closes_client() -> None:
    model, _ = make_model()

    asyncio.run(model.aclose())

    model._client.close.assert_awaited_once()  # type: ignore[attr-defined]


# --- Fabrik ---------------------------------------------------------------------


def test_factory_builds_openrouter_model_by_default(valid_env: dict[str, str]) -> None:
    settings = load_settings(valid_env)

    model = create_chat_model(settings)

    assert str(model._client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
    assert model._client.api_key == settings.llm_api_key
    assert model._model == settings.llm_model
    assert model._max_tokens_param == "max_tokens"
    assert model._provider == "OpenRouter"
    assert model._extra_body == {"reasoning": {"enabled": False}}
    asyncio.run(model.aclose())


def test_factory_hides_reasoning_when_enabled(valid_env: dict[str, str]) -> None:
    model = create_chat_model(load_settings({**valid_env, "LLM_REASONING": "true"}))

    assert model._extra_body == {"reasoning": {"exclude": True}}
    asyncio.run(model.aclose())


def test_factory_builds_groq_model_with_groq_url_and_parameter_name(valid_env: dict[str, str]) -> None:
    env = {k: v for k, v in valid_env.items() if k != "OPENROUTER_API_KEY"}
    settings = load_settings({**env, "LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk_dummy"})

    model = create_chat_model(settings)

    assert str(model._client.base_url).rstrip("/") == "https://api.groq.com/openai/v1"
    assert model._client.api_key == "gsk_dummy"
    assert model._max_tokens_param == "max_completion_tokens"
    assert model._extra_body is None
    asyncio.run(model.aclose())


def test_image_model_defaults_to_the_text_model_and_shares_it(valid_env: dict[str, str]) -> None:
    chat_model = create_chat_model(load_settings(valid_env))
    same_name = load_settings({**valid_env, "LLM_IMAGE_MODEL": valid_env["LLM_MODEL"]})

    assert create_image_model(load_settings(valid_env), chat_model) is chat_model
    assert create_image_model(same_name, chat_model) is chat_model
    asyncio.run(chat_model.aclose())


def test_separate_image_model_is_created_when_configured(valid_env: dict[str, str]) -> None:
    settings = load_settings({**valid_env, "LLM_IMAGE_MODEL": "other-vendor/vision"})
    chat_model = create_chat_model(settings)

    image_model = create_image_model(settings, chat_model)

    assert image_model is not chat_model
    assert image_model._model == "other-vendor/vision"  # type: ignore[attr-defined]
    assert chat_model._model == settings.llm_model
    asyncio.run(chat_model.aclose())
    asyncio.run(image_model.aclose())
