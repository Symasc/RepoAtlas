"""Embedding backends and the vector index (V1_SPEC §3, §14, §20, §19).

Nothing here downloads a model or touches the network. ``FastEmbedModel`` is
exercised through an injected loader and through its cache probe; the real model
is behind the ``slow`` marker, which is deselected by default.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from repoatlas.config.settings import EmbeddingSettings, Settings
from repoatlas.embeddings.factory import build_embedding_model
from repoatlas.embeddings.fastembed_backend import DEFAULT_DIM, DEFAULT_MODEL, FastEmbedModel
from repoatlas.embeddings.hashing import HashingEmbedding
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable
from repoatlas.indexing.chunking import chunk_hash
from repoatlas.services.workspace import Workspace
from repoatlas.storage.sqlite.vectors import decode_vector, encode_vector


def cosine(a: list[float], b: list[float]) -> float:
    left, right = np.asarray(a), np.asarray(b)
    return float(left @ right / (np.linalg.norm(left) * np.linalg.norm(right)))


# --- the protocol and its backends -------------------------------------------


def test_hashing_backend_satisfies_the_protocol() -> None:
    assert isinstance(HashingEmbedding(), EmbeddingModel)


def test_fastembed_backend_satisfies_the_protocol() -> None:
    assert isinstance(FastEmbedModel(), EmbeddingModel)


def test_hashing_embeddings_are_deterministic() -> None:
    """Stable across runs and machines, which is what makes the stage assertable."""
    first = HashingEmbedding().embed(["load_config reads SERVICE_HOST"])
    second = HashingEmbedding().embed(["load_config reads SERVICE_HOST"])
    assert first == second


def test_hashing_embeddings_are_normalized() -> None:
    vector = HashingEmbedding().embed(["some text with tokens"])[0]
    assert np.isclose(np.linalg.norm(vector), 1.0)


def test_shared_vocabulary_scores_higher_than_unrelated_text() -> None:
    model = HashingEmbedding()
    query = model.embed_query("where is SERVICE_HOST read from the environment")
    related, unrelated = model.embed(
        [
            "host = os.getenv('SERVICE_HOST')",
            "def multiply(a, b): return a * b",
        ]
    )
    assert cosine(query, related) > cosine(query, unrelated)


def test_empty_input_embeds_to_nothing() -> None:
    assert HashingEmbedding().embed([]) == []


# --- fastembed, without the network ------------------------------------------


class _FakeEmbedder:
    """Stands in for ``TextEmbedding``; records what it was asked to embed."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        self.seen.extend(texts)
        return [[float(len(text)), 1.0, 0.0] for text in texts]


def test_fastembed_defaults_match_the_specification() -> None:
    assert DEFAULT_MODEL == "BAAI/bge-small-en-v1.5"
    assert DEFAULT_DIM == 384


def test_fastembed_prefixes_queries_but_not_documents() -> None:
    """bge is asymmetric; embedding both sides identically degrades recall."""
    fake = _FakeEmbedder()
    model = FastEmbedModel(loader=lambda: fake)

    model.embed(["a document"])
    model.embed_query("a question")

    assert fake.seen[0] == "a document"
    assert fake.seen[1].endswith("a question")
    assert fake.seen[1] != "a question"


def test_fastembed_availability_never_downloads_by_default() -> None:
    """A sync must not stall for minutes fetching a model (ARCHITECTURE.md §10)."""
    model = FastEmbedModel(model_name="definitely/not-a-real-model")
    assert model.available() is False
    detail = model.availability_detail()
    assert "not downloaded" in detail
    assert "repoatlas sync --embed" in detail
    assert "unaffected" in detail


def test_fastembed_is_available_once_a_loader_is_injected() -> None:
    assert FastEmbedModel(loader=_FakeEmbedder).available() is True


def test_fastembed_download_failure_is_reported_not_raised() -> None:
    def explode() -> object:
        raise EmbeddingUnavailable("network is down")

    model = FastEmbedModel(allow_download=True, loader=explode)
    assert model.available() is False
    assert "network is down" in model.availability_detail()


@pytest.mark.slow
def test_real_fastembed_model_produces_384_dimensions() -> None:
    """Deselected by default; downloads ~67 MB on first run."""
    model = FastEmbedModel(allow_download=True)
    assert model.available()
    vectors = model.embed(["SERVICE_HOST is read from the environment"])
    assert len(vectors[0]) == DEFAULT_DIM


# --- the factory --------------------------------------------------------------


def test_factory_selects_the_configured_provider() -> None:
    assert isinstance(
        build_embedding_model(EmbeddingSettings(provider="hashing")), HashingEmbedding
    )
    assert isinstance(
        build_embedding_model(EmbeddingSettings(provider="fastembed")), FastEmbedModel
    )


def test_disabled_embeddings_are_a_supported_state_not_an_error() -> None:
    assert build_embedding_model(EmbeddingSettings(enabled=False)) is None
    assert build_embedding_model(EmbeddingSettings(provider="none")) is None


def test_unknown_provider_names_the_valid_ones() -> None:
    with pytest.raises(EmbeddingUnavailable, match="fastembed, hashing, none"):
        build_embedding_model(EmbeddingSettings(provider="magic"))


def test_default_provider_is_fastembed() -> None:
    assert Settings().embeddings.provider == "fastembed"
    assert Settings().embeddings.allow_download is False


# --- vector storage -----------------------------------------------------------


def test_vectors_round_trip_through_float16() -> None:
    original = [0.5, -0.25, 0.125]
    assert decode_vector(encode_vector(original)).tolist() == original


def test_vectors_are_stored_and_counted(initialized: Workspace) -> None:
    assert initialized.vectors.count("hashing-128") > 0


def test_a_chunk_hash_depends_only_on_its_text() -> None:
    assert chunk_hash("same text") == chunk_hash("same text")
    assert chunk_hash("same text") != chunk_hash("other text")


def test_query_returns_nothing_when_dimensions_disagree(initialized: Workspace) -> None:
    """Vectors from another model are not comparable; ranking them would invent one."""
    assert initialized.vectors.query([0.1] * 999, "hashing-128", None, 10, 0.0) == []


def test_query_is_scoped_before_scoring(initialized: Workspace) -> None:
    repo = initialized.repositories.get_by_name("python-service")
    assert repo is not None and repo.id is not None
    model = initialized.embeddings()
    assert model is not None
    hits = initialized.vectors.query(
        model.embed_query("configuration"), model.id, [repo.id], 20, -1.0
    )
    assert hits
    assert {hit.repository for hit in hits} == {"python-service"}


def test_the_similarity_floor_drops_weak_neighbours(initialized: Workspace) -> None:
    model = initialized.embeddings()
    assert model is not None
    vector = model.embed_query("configuration")
    everything = initialized.vectors.query(vector, model.id, None, 100, -1.0)
    strong = initialized.vectors.query(vector, model.id, None, 100, 0.5)
    assert len(strong) < len(everything)
    assert all(hit.score >= 0.5 for hit in strong)


# --- incremental embedding (V1_SPEC §20) --------------------------------------


def test_a_second_sync_embeds_nothing(initialized: Workspace, workspace_root: Path) -> None:
    from repoatlas.services.sync_service import sync

    report = sync(initialized, initialized.resolver().resolve(workspace_root))
    assert report.embedded_chunks == 0
    assert report.embedding_detail == "up to date"


def test_only_the_changed_chunks_are_re_embedded(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Editing one file must not re-embed the chunks whose text did not move."""
    from repoatlas.services.sync_service import sync

    engine = initialized.sync_engine()
    model = initialized.embeddings()
    assert model is not None

    before = initialized.vectors.count(model.id)
    target = workspace_root / "python-service" / "src" / "config.py"
    target.write_text(target.read_text() + "\n\ndef added_helper():\n    return 1\n")

    report = sync(initialized, initialized.resolver().resolve(workspace_root / "python-service"))

    assert report.embedded_chunks > 0, "the new text is embedded"
    assert report.embedded_chunks < before, "the untouched chunks are not"
    assert engine.embed_pending(None).embedded == 0


def test_unchanged_text_keeps_its_vector_across_a_reparse(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Chunk rows are replaced wholesale on reparse; vectors are keyed by text."""
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    target = workspace_root / "python-service" / "src" / "config.py"
    text = target.read_text()

    hashes_before = {
        row["content_hash"]
        for row in initialized.database.execute(
            "SELECT c.content_hash AS content_hash FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE f.rel_path = 'src/config.py'"
        ).fetchall()
    }
    # Touch the file without changing a byte of its content.
    target.write_text(text)
    sync(initialized, initialized.resolver().resolve(workspace_root / "python-service"))

    stored = {
        row["content_hash"]
        for row in initialized.database.execute("SELECT content_hash FROM embeddings").fetchall()
    }
    assert hashes_before <= stored


def test_orphaned_vectors_are_swept(initialized: Workspace, workspace_root: Path) -> None:
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    before = initialized.vectors.count(model.id)

    (workspace_root / "python-service" / "src" / "config.py").unlink()
    sync(initialized, initialized.resolver().resolve(workspace_root / "python-service"))

    assert initialized.vectors.count(model.id) < before


def test_changing_the_model_re_embeds_without_reparsing(initialized: Workspace) -> None:
    """Vectors are keyed by model, so a switch finds none and recomputes."""
    engine = initialized.sync_engine()
    assert engine.embed_pending(None).embedded == 0

    other = Workspace(
        settings=initialized.settings.model_copy(
            update={"embeddings": initialized.settings.embeddings.model_copy(update={"dim": 64})}
        ),
        database=initialized.database,
        repositories=initialized.repositories,
        files=initialized.files,
        knowledge=initialized.knowledge,
        dependencies=initialized.dependencies,
        search=initialized.search,
        vectors=initialized.vectors,
        registry=initialized.registry,
        dependency_registry=initialized.dependency_registry,
        git=initialized.git,
    )
    result = other.sync_engine().embed_pending(None)
    assert result.embedded > 0
    assert initialized.vectors.count("hashing-64") == result.embedded


def test_embedding_is_skipped_when_the_model_is_unavailable(initialized: Workspace) -> None:
    """Never fatal: the mandatory retrieval stages do not depend on it."""
    engine = initialized.sync_engine()
    initialized.vectors.delete_model("hashing-128")

    class _Missing:
        id = "missing-model"
        dim = 8

        def embed(self, texts: object) -> list[list[float]]:  # pragma: no cover - not reached
            raise AssertionError("must not be called when unavailable")

        def embed_query(self, text: str) -> list[float]:  # pragma: no cover - not reached
            raise AssertionError("must not be called when unavailable")

        def available(self) -> bool:
            return False

        def availability_detail(self) -> str:
            return "model not downloaded"

    engine._embeddings = _Missing()
    result = engine.embed_pending(None)
    assert result.embedded == 0
    assert result.pending > 0
    assert result.detail == "model not downloaded"


# --- content addressing when several chunks share one hash (V1_SPEC §20) ------
#
# Vectors are keyed by ``(content_hash, model)``, not by chunk id, so identical
# text anywhere in the managed set shares exactly one vector. That is what makes
# re-embedding incremental, and it is also the case a chunk-keyed cascade would
# get wrong: deleting one of several chunks that share a hash must **not** remove
# the vector the others still rely on.


def _shared_text() -> str:
    return "def duplicated_helper():\n    return SHARED_MARKER_VALUE\n"


def _plant_duplicate_files(workspace_root: Path, count: int) -> list[Path]:
    """The same bytes in ``count`` different files across two repositories."""
    targets = [
        workspace_root / "python-service" / "src" / f"copy_{index}.py"
        if index % 2 == 0
        else workspace_root / "scripts" / f"copy_{index}.py"
        for index in range(count)
    ]
    for target in targets:
        target.write_text(_shared_text())
    return targets


def _shared_hash(workspace: Workspace) -> str:
    """The hash the duplicated text actually chunked to.

    Read back rather than recomputed, so the test asserts sharing rather than
    reimplementing how a file is split into chunks.
    """
    row = workspace.database.execute(
        "SELECT DISTINCT content_hash FROM chunks WHERE text LIKE '%SHARED_MARKER_VALUE%'"
    ).fetchall()
    assert len(row) == 1, "the planted copies must chunk identically"
    return str(row[0]["content_hash"])


def _vector_rows(workspace: Workspace, content_hash: str, model: str) -> int:
    row = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM embeddings WHERE content_hash = ? AND model = ?",
        (content_hash, model),
    ).fetchone()
    return int(row["n"])


def _chunk_rows(workspace: Workspace, content_hash: str) -> int:
    row = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return int(row["n"])


def test_identical_text_in_many_files_is_embedded_once(
    initialized: Workspace, workspace_root: Path
) -> None:
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    _plant_duplicate_files(workspace_root, 6)
    before = initialized.vectors.count(model.id)
    sync(initialized, initialized.resolver().resolve(workspace_root))

    shared = _shared_hash(initialized)
    assert _chunk_rows(initialized, shared) == 6
    assert _vector_rows(initialized, shared, model.id) == 1
    # Six new chunks, one new vector: the cost of duplication is storage, not
    # embedding work.
    assert initialized.vectors.count(model.id) == before + 1


def test_adding_another_copy_embeds_nothing_at_all(
    initialized: Workspace, workspace_root: Path
) -> None:
    from repoatlas.services.sync_service import sync

    _plant_duplicate_files(workspace_root, 2)
    sync(initialized, initialized.resolver().resolve(workspace_root))

    (workspace_root / "ts-service" / "copy.py").write_text(_shared_text())
    report = sync(initialized, initialized.resolver().resolve(workspace_root))
    assert report.embedded_chunks == 0
    assert _chunk_rows(initialized, _shared_hash(initialized)) == 3


def test_deleting_one_sharer_keeps_the_vector_the_others_need(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The defect a chunk-id cascade would have: removing one copy must not blind
    the retriever to the others."""
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    targets = _plant_duplicate_files(workspace_root, 4)
    sync(initialized, initialized.resolver().resolve(workspace_root))
    shared = _shared_hash(initialized)

    targets[0].unlink()
    sync(initialized, initialized.resolver().resolve(workspace_root))
    assert _chunk_rows(initialized, shared) == 3
    assert _vector_rows(initialized, shared, model.id) == 1

    # And the surviving copies are still semantically retrievable.
    query = model.embed_query("duplicated helper")
    hits = initialized.vectors.query(query, model.id, None, 50, -1.0)
    assert [hit for hit in hits if hit.text.strip() == _shared_text().strip()]


def test_the_vector_is_swept_only_when_the_last_sharer_goes(
    initialized: Workspace, workspace_root: Path
) -> None:
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    targets = _plant_duplicate_files(workspace_root, 3)
    sync(initialized, initialized.resolver().resolve(workspace_root))
    shared = _shared_hash(initialized)

    for target in targets[:-1]:
        target.unlink()
        sync(initialized, initialized.resolver().resolve(workspace_root))
        assert _vector_rows(initialized, shared, model.id) == 1

    targets[-1].unlink()
    sync(initialized, initialized.resolver().resolve(workspace_root))
    assert _chunk_rows(initialized, shared) == 0
    assert _vector_rows(initialized, shared, model.id) == 0


def test_a_sweep_never_removes_a_vector_a_chunk_still_uses(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The sweep is defined by chunk membership, so running it repeatedly — and
    when nothing changed — is a no-op."""
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    _plant_duplicate_files(workspace_root, 3)
    sync(initialized, initialized.resolver().resolve(workspace_root))
    before = initialized.vectors.count(model.id)

    assert initialized.vectors.gc_orphan_vectors() == 0
    assert initialized.vectors.gc_orphan_vectors() == 0
    assert initialized.vectors.count(model.id) == before


def test_a_shared_hash_is_offered_to_the_model_once(
    initialized: Workspace, workspace_root: Path
) -> None:
    """``missing_hashes`` de-duplicates, so the backend is asked once even though
    many chunks are waiting on the same text."""
    from repoatlas.services.sync_service import sync

    model = initialized.embeddings()
    assert model is not None
    _plant_duplicate_files(workspace_root, 5)
    sync(initialized, initialized.resolver().resolve(workspace_root))

    shared = _shared_hash(initialized)
    with initialized.database.transaction() as conn:
        conn.execute(
            "DELETE FROM embeddings WHERE content_hash = ? AND model = ?", (shared, model.id)
        )
    pending = initialized.vectors.missing_hashes(model.id)
    assert pending.count(shared) == 1
    assert len(initialized.vectors.texts_for_hashes(pending)) == len(set(pending))


def test_each_model_keeps_its_own_vector_for_a_shared_hash(initialized: Workspace) -> None:
    """The key is ``(content_hash, model)``: two models coexist, and deleting one
    leaves the other intact."""
    shared = chunk_hash("SELECT DISTINCT content that two models both embed")
    initialized.vectors.upsert_many("model-a", 4, [(shared, [1.0, 0.0, 0.0, 0.0])])
    initialized.vectors.upsert_many("model-b", 4, [(shared, [0.0, 1.0, 0.0, 0.0])])
    assert _vector_rows(initialized, shared, "model-a") == 1
    assert _vector_rows(initialized, shared, "model-b") == 1

    initialized.vectors.delete_model("model-a")
    assert _vector_rows(initialized, shared, "model-a") == 0
    assert _vector_rows(initialized, shared, "model-b") == 1
