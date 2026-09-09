"""The real fastembed backend, end to end (V1_SPEC §3, §14).

Deselected by default (``-m 'not slow'``): the first run downloads ~67 MB, and
CI must never depend on the network (§19). Everything else in the suite runs
against the deterministic ``HashingEmbedding`` stub, which is fast and assertable
but is *not* a semantic model — so this file exists to prove that the configured
model, the one V1 actually ships with, works through the whole pipeline:

    settings → factory → sync → SqliteVectorIndex → retrieval funnel

and that it does not disturb the precedence guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import EmbeddingSettings, Settings
from repoatlas.embeddings.factory import build_embedding_model
from repoatlas.embeddings.fastembed_backend import DEFAULT_DIM, DEFAULT_MODEL, FastEmbedModel
from repoatlas.models.evidence import EvidenceTier
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def real_model() -> FastEmbedModel:
    model = FastEmbedModel(allow_download=True)
    if not model.available():
        pytest.skip(model.availability_detail())
    return model


def test_the_configured_model_is_the_one_that_loads(real_model: FastEmbedModel) -> None:
    assert real_model.id == DEFAULT_MODEL == "BAAI/bge-small-en-v1.5"
    assert real_model.dim == DEFAULT_DIM == 384


def test_real_vectors_have_the_declared_dimension(real_model: FastEmbedModel) -> None:
    """A dimension mismatch is silent in the vector index — it returns nothing —
    so the declared value and the produced value must be checked against reality."""
    vectors = real_model.embed(
        ["SERVICE_HOST is read from the environment", "def load_config() -> ServiceConfig:"]
    )
    assert [len(v) for v in vectors] == [DEFAULT_DIM, DEFAULT_DIM]
    assert real_model.dim == len(real_model.embed_query("where is the host configured"))


def test_real_embeddings_are_deterministic(real_model: FastEmbedModel) -> None:
    """Content addressing assumes the same text yields the same vector."""
    first = real_model.embed(["the deploy script invokes the python service"])
    second = real_model.embed(["the deploy script invokes the python service"])
    assert first == second


def test_the_query_prefix_actually_changes_the_vector(real_model: FastEmbedModel) -> None:
    """bge is asymmetric. If the prefix were dropped, recall would quietly degrade
    with nothing failing, so it is asserted rather than assumed."""
    text = "where is SERVICE_HOST configured"
    assert real_model.embed_query(text) != real_model.embed([text])[0]


def test_semantically_related_text_scores_above_unrelated_text(
    real_model: FastEmbedModel,
) -> None:
    """The property the hashing stub cannot have, and the reason a real model is
    worth carrying at all: relatedness without shared tokens."""
    import numpy as np

    query = np.asarray(real_model.embed_query("how is the service host configured"))
    related, unrelated = (
        np.asarray(v)
        for v in real_model.embed(
            [
                "The deployment layer sets the address the daemon binds to.",
                "Ruby logging helpers format timestamps for the audit trail.",
            ]
        )
    )
    similarity = lambda v: float(query @ v / (np.linalg.norm(query) * np.linalg.norm(v)))  # noqa: E731
    assert similarity(related) > similarity(unrelated)


@pytest.fixture
def real_workspace(tmp_path: Path, workspace_root: Path, real_model: FastEmbedModel) -> Workspace:
    """A workspace indexed with the real model, including its vectors."""
    del real_model
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "real" / "atlas.db")},
            "embeddings": EmbeddingSettings(provider="fastembed", allow_download=True).model_dump(
                mode="json"
            ),
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, workspace_root, config_path=tmp_path / "real-config.yaml")
    return workspace


def test_a_real_sync_stores_real_vectors(real_workspace: Workspace) -> None:
    assert isinstance(build_embedding_model(real_workspace.settings.embeddings), FastEmbedModel)
    assert real_workspace.vectors.count(DEFAULT_MODEL) > 0
    row = real_workspace.database.execute(
        "SELECT dim FROM embeddings WHERE model = ? LIMIT 1", (DEFAULT_MODEL,)
    ).fetchone()
    assert row["dim"] == DEFAULT_DIM


def test_a_prose_question_reaches_real_evidence(
    real_workspace: Workspace, workspace_root: Path
) -> None:
    """No identifier in the question appears in the answer's file, so only the
    semantic stage can find it."""
    context = real_workspace.resolver().resolve(workspace_root / "python-service")
    evidence = real_workspace.retriever().retrieve(
        "how does this service decide which address to bind to", context
    )
    assert evidence.candidates
    assert evidence.trace is not None and evidence.trace.semantic_used
    assert any("config" in candidate.path for candidate in evidence.candidates)


def test_the_precedence_guarantee_holds_with_a_real_model(
    real_workspace: Workspace, workspace_root: Path
) -> None:
    """The tier rule is not an artifact of the deterministic stub."""
    context = real_workspace.resolver().resolve(workspace_root / "python-service")
    for question in (
        "SERVICE_HOST",
        "how does the service load its configuration",
        "what configures the host address",
    ):
        evidence = real_workspace.retriever().retrieve(question, context)
        tiers = [candidate.tier for candidate in evidence.candidates]
        assert tiers == sorted(tiers)
        assert all(
            candidate.tier is EvidenceTier.DETERMINISTIC
            for candidate in evidence.candidates
            if candidate.source.value in {"exact", "symbol", "graph"}
        )


def test_a_second_real_sync_embeds_nothing(real_workspace: Workspace, workspace_root: Path) -> None:
    """Never re-embed unchanged data (§20) — asserted against the real backend,
    where the cost of getting it wrong is seconds per sync rather than microseconds."""
    from repoatlas.services.sync_service import sync

    report = sync(real_workspace, real_workspace.resolver().resolve(workspace_root))
    assert report.embedded_chunks == 0
    assert report.embedding_detail == "up to date"
