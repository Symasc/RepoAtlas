"""Embedding backend selection (V1_SPEC §3).

Configuration-driven, exactly like the reasoning model: nothing above this module
names a backend.
"""

from __future__ import annotations

from repoatlas.config.settings import EmbeddingSettings
from repoatlas.embeddings.fastembed_backend import FastEmbedModel
from repoatlas.embeddings.hashing import HashingEmbedding
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable


def build_embedding_model(settings: EmbeddingSettings) -> EmbeddingModel | None:
    """The configured backend, or ``None`` when embeddings are switched off.

    ``None`` is a supported state, not a failure: V1_SPEC §14 makes exact search
    mandatory and semantic search an aid, so a workspace with embeddings disabled
    is fully functional.
    """
    if not settings.enabled or settings.provider == "none":
        return None
    if settings.provider == "fastembed":
        return FastEmbedModel(
            settings.model,
            dim=settings.dim,
            batch_size=settings.batch_size,
            cache_dir=str(settings.cache_dir) if settings.cache_dir else None,
            allow_download=settings.allow_download,
        )
    if settings.provider == "hashing":
        return HashingEmbedding(dim=settings.dim)
    raise EmbeddingUnavailable(
        f"unknown embedding provider {settings.provider!r}; "
        "expected one of: fastembed, hashing, none"
    )
