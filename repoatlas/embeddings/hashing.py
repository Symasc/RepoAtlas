"""A deterministic embedding backend with no model and no network.

This is what makes the semantic path testable. Every assertion about fusion,
tiering and budgeting needs vectors that are stable across runs and machines, and
a real model gives neither without a download (V1_SPEC §19: no network in CI).

It is a hashed bag-of-tokens projection: tokens are hashed to dimensions with
signed weights, the result is L2-normalized, and cosine similarity therefore
behaves the way a real embedding does for the property under test — texts sharing
vocabulary score higher than texts that do not. It is **not** a semantic model and
is never the default.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

_TOKEN = re.compile(r"[A-Za-z0-9_]+")

#: Small enough to keep tests fast, large enough that collisions do not dominate.
_DEFAULT_DIM = 256


def _tokens(text: str) -> list[str]:
    found: list[str] = []
    for match in _TOKEN.finditer(text.lower()):
        token = match.group(0)
        found.append(token)
        # Split snake_case so `service_host` also matches `host`.
        if "_" in token:
            found.extend(part for part in token.split("_") if part)
    return found


class HashingEmbedding:
    """Deterministic, dependency-free, offline."""

    def __init__(self, dim: int = _DEFAULT_DIM) -> None:
        self._dim = dim

    @property
    def id(self) -> str:
        return f"hashing-{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def available(self) -> bool:
        return True

    def availability_detail(self) -> str:
        return "deterministic hashing backend"

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]
