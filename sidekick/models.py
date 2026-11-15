"""Pick the chat model from a "provider:model" string, so any LangChain provider works.

Examples (set SIDEKICK_MODEL in .env):
    openai:gpt-5.4-mini
    anthropic:claude-opus-5-5
    google_genai:gemini-2.5-flash
    groq:llama-3.3-70b-versatile
    ollama:qwen3:14b
    openrouter:anthropic/claude-sonnet-5.5      (any model on OpenRouter)

Any OpenAI-compatible server (LM Studio, vLLM, Together…) also works: use the
"openai:" prefix and set SIDEKICK_BASE_URL and SIDEKICK_API_KEY.
"""

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

DEFAULT_MODEL = "openai:gpt-5.4-mini"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def model_spec(env_var: str = "SIDEKICK_MODEL", fallback: str | None = None) -> str:
    return os.getenv(env_var) or fallback or DEFAULT_MODEL


def provider_of(spec: str) -> str:
    return spec.split(":", 1)[0] if ":" in spec else "auto"


def build_model(spec: str) -> BaseChatModel:
    provider, _, name = spec.partition(":")
    kwargs = {}
    if os.getenv("SIDEKICK_MAX_TOKENS"):
        kwargs["max_tokens"] = int(os.environ["SIDEKICK_MAX_TOKENS"])

    if provider == "openrouter":
        # OpenRouter reserves credit for max_tokens up front, so don't ask for the model's maximum
        kwargs.setdefault("max_tokens", 16000)
        return init_chat_model(
            name,
            model_provider="openai",
            base_url=OPENROUTER_URL,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            **kwargs,
        )

    if os.getenv("SIDEKICK_BASE_URL"):
        kwargs["base_url"] = os.environ["SIDEKICK_BASE_URL"]
        if os.getenv("SIDEKICK_API_KEY"):
            kwargs["api_key"] = os.environ["SIDEKICK_API_KEY"]
    # A bare name like "gpt-5.4-mini" or "claude-opus-5-5" still works: LangChain infers the provider
    return init_chat_model(spec, **kwargs)


def build_structured(spec: str, schema):
    """Structured output that works across providers.

    Anthropic's newest models reject forced tool use, so ask for native JSON-schema
    output there; every other provider keeps LangChain's default."""
    model = build_model(spec)
    if provider_of(spec) == "anthropic" or (provider_of(spec) == "auto" and spec.startswith("claude")):
        return model.with_structured_output(schema, method="json_schema")
    return model.with_structured_output(schema)
