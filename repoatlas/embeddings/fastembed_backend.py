"""fastembed backend (V1_SPEC §3).

``BAAI/bge-small-en-v1.5`` via ONNX Runtime: no PyTorch, no GPU requirement, no
paid API, 384 dimensions. The model is downloaded once on first use and cached by
fastembed; nothing here reaches the network at import time, so a machine that has
never embedded anything still starts instantly and every deterministic command
keeps working.

bge is an **asymmetric** retrieval model: queries are prefixed, documents are not.
Embedding both sides identically measurably degrades recall, so :meth:`embed` and
:meth:`embed_query` differ deliberately.

**Availability never downloads.** ``available()`` probes the local cache and
returns False if the model is absent. A ``sync`` must not silently stall for
minutes fetching 67 MB; the download is an explicit action (``sync --embed``), and
until then semantic retrieval is simply off while every deterministic command
keeps working.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from repoatlas.embeddings.protocol import EmbeddingUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastembed import TextEmbedding

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

#: The prefix bge-v1.5 was trained to see on the query side only.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class FastEmbedModel:
    """Local ONNX embeddings, loaded lazily."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        dim: int = DEFAULT_DIM,
        batch_size: int = 32,
        cache_dir: str | None = None,
        allow_download: bool = False,
        loader: Any | None = None,
    ) -> None:
        self._model_name = model_name
        self._dim = dim
        self._batch_size = batch_size
        self._cache_dir = cache_dir
        self._allow_download = allow_download
        self._loader = loader
        self._model: TextEmbedding | None = None
        self._failure: str | None = None

    @property
    def id(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return self._dim

    # -- loading -------------------------------------------------------------

    def _load(self) -> TextEmbedding:
        if self._model is not None:
            return self._model

        if self._loader is not None:
            try:
                loaded = cast("TextEmbedding", self._loader())
            except EmbeddingUnavailable:
                raise
            except Exception as exc:
                raise EmbeddingUnavailable(f"embedding loader failed: {exc}") from exc
            self._model = loaded
            return loaded

        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise EmbeddingUnavailable(f"fastembed is not installed: {exc}") from exc
        try:
            loaded = TextEmbedding(model_name=self._model_name, cache_dir=self._cache_dir)
        except Exception as exc:
            raise EmbeddingUnavailable(
                f"could not load embedding model {self._model_name!r}: {exc}"
            ) from exc
        self._model = loaded
        return loaded

    # -- embedding -----------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = [
            [float(value) for value in vector]
            for vector in model.embed(list(texts), batch_size=self._batch_size)
        ]
        if len(vectors) != len(texts):  # pragma: no cover - defensive
            raise EmbeddingUnavailable(
                f"model returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed([f"{_QUERY_PREFIX}{text}"])
        return vectors[0] if vectors else []

    # -- availability --------------------------------------------------------

    def available(self) -> bool:
        """Whether the model can be used **without going to the network**.

        Checked before any embedding work is queued, so an absent model produces
        one clear message rather than a multi-minute stall inside a sync. When
        ``allow_download`` is set the check performs the real load, which is what
        ``sync --embed`` turns on.
        """
        if self._model is not None:
            self._failure = None
            return True

        # An injected loader is local by construction, so trying it is cheap; a
        # download is not, and happens only when explicitly authorized.
        if self._loader is not None or self._allow_download:
            try:
                self._load()
            except EmbeddingUnavailable as exc:
                self._failure = str(exc)
                return False
            self._failure = None
            return True

        if self._cached():
            self._failure = None
            return True

        self._failure = f"embedding model {self._model_name!r} is not downloaded"
        return False

    def _cached(self) -> bool:
        """Whether the model's files are already on disk. Never fetches.

        The empty cache is checked first, and answered without importing
        ``fastembed``. That import pulls in ONNX Runtime, which is both slow and
        — on a read-only container filesystem — noisy, and it is pure waste when
        the directory it would search does not exist. The mapping from a model
        name to its Hugging Face repository is static data that only fastembed
        knows, so the import is still needed once there is a cache to look in.
        """
        try:
            from huggingface_hub import snapshot_download
            from huggingface_hub.constants import HF_HUB_CACHE
        except ImportError:  # pragma: no cover - dependency is required
            return False

        cache_root = Path(self._cache_dir) if self._cache_dir else Path(HF_HUB_CACHE)
        if not cache_root.is_dir():
            return False

        try:
            from fastembed import TextEmbedding
        except ImportError:  # pragma: no cover - dependency is required
            return False

        repository = self._huggingface_repository(TextEmbedding)
        if repository is None:
            return False
        try:
            snapshot_download(repository, local_files_only=True, cache_dir=self._cache_dir)
        except Exception:
            return False
        return True

    def _huggingface_repository(self, embedding_class: Any) -> str | None:
        for description in embedding_class.list_supported_models():
            if description.get("model") != self._model_name:
                continue
            sources = description.get("sources") or {}
            repository = sources.get("hf")
            return str(repository) if repository else None
        return None

    def availability_detail(self) -> str:
        if self._failure is None:
            return f"fastembed model {self._model_name} ready"
        return (
            f"{self._failure}. Run `repoatlas sync --embed` once to fetch it. "
            "Semantic retrieval stays off until then; exact, symbol and graph "
            "retrieval are unaffected."
        )
