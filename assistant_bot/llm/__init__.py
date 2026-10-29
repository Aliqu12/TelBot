"""LLM-Schicht: ein Adapter für alle OpenAI-kompatiblen Anbieter (OpenRouter, Groq)."""

from .base import (
    ChatModel,
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMInsufficientCreditsError,
    LLMRateLimitError,
    LLMRequestTooLargeError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelTurn,
)
from .factory import PROVIDERS, ProviderSpec, create_chat_model, create_image_model
from .openai_compatible import OpenAICompatibleChatModel

__all__ = [
    "PROVIDERS",
    "ChatModel",
    "LLMConfigurationError",
    "LLMEmptyResponseError",
    "LLMError",
    "LLMInsufficientCreditsError",
    "LLMRateLimitError",
    "LLMRequestTooLargeError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "ModelTurn",
    "OpenAICompatibleChatModel",
    "ProviderSpec",
    "create_chat_model",
    "create_image_model",
]
