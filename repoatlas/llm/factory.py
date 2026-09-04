"""Configuration-driven backend selection (V1_SPEC §3).

Nothing above this function names a backend, so adding vLLM, llama.cpp or an
OpenAI-compatible server is a change here and nowhere else.
"""

from __future__ import annotations

from repoatlas.config.settings import LLMSettings
from repoatlas.llm.null import NullBackend
from repoatlas.llm.ollama import OllamaBackend
from repoatlas.llm.protocol import ModelConfigurationError, ReasoningModel

_PROVIDERS = ("ollama", "none")


def build_model(settings: LLMSettings) -> ReasoningModel:
    """Instantiate the configured reasoning backend."""
    provider = settings.provider.strip().lower()
    if provider == "ollama":
        return OllamaBackend(settings)
    if provider in {"none", "null"}:
        return NullBackend()
    # RepoAtlas's own configuration, not a backend that failed: nothing was
    # contacted, so nothing is unavailable.
    raise ModelConfigurationError(
        f"Unknown LLM provider: {settings.provider!r}",
        detail="Supported providers:\n\n" + "\n".join(f"  {p}" for p in _PROVIDERS),
    )
