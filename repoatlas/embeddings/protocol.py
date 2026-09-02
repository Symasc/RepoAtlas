"""The embedding contract (ARCHITECTURE.md §4).

One interface, so the vector backend is replaceable without touching a caller —
the same seam the reasoning model sits behind (V1_SPEC §3).

An embedding model is a **ranking aid, never a source of facts**. Nothing in the
system may assert a relationship because two vectors are close; semantic
similarity only ever changes the order in which real, provenanced evidence is
considered (V1_SPEC §2, §14).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingUnavailable(RuntimeError):
    """The configured embedding backend cannot be used.

    Never fatal: retrieval degrades to its deterministic stages, which are the
    ones V1_SPEC §14 makes mandatory in the first place.
    """


@runtime_checkable
class EmbeddingModel(Protocol):
    """Turns text into vectors."""

    @property
    def id(self) -> str:
        """Stable identifier stored beside every vector.

        A change here means stored vectors are for a different model, so they are
        simply not found for the new id and are recomputed (ARCHITECTURE.md §9).
        """
        ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed documents. One vector per input, in order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a question.

        Separate from :meth:`embed` because retrieval models are often asymmetric:
        bge expects a query prefix that documents must not receive.
        """
        ...

    def available(self) -> bool: ...

    def availability_detail(self) -> str:
        """Why the model is unusable, phrased as something the user can act on."""
        ...
