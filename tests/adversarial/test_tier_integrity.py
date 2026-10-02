"""Tier integrity: associative evidence must not be laundered into fact.

The tier is the whole of the precedence guarantee (V1_SPEC §14). A candidate that
enters the funnel as a *lead* must not leave it wearing the `DETERMINISTIC` label,
because that label is what exempts it from competing on score.

Graph expansion is the place this can go wrong quietly: it labels everything it
reaches `DETERMINISTIC`, which is right — an edge is a fact — but only if the
*seed* was itself a fact. ARCHITECTURE §10 specifies expansion "from the exact
entity hits". Seeding it from full-text entity recall instead turns a fuzzy name
match into six deterministic claims that outrank every genuine lead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.models.evidence import EvidenceSource, EvidenceTier
from repoatlas.services.workspace import Workspace

pytestmark = pytest.mark.adversarial


def _retrieve(workspace: Workspace, root: Path, repo: str, question: str):
    return workspace.retriever().retrieve(question, workspace.resolver().resolve(root / repo))


def _graph_seeds(workspace: Workspace, root: Path, repo: str, question: str) -> int:
    evidence = _retrieve(workspace, root, repo, question)
    assert evidence.trace is not None
    return next(s for s in evidence.trace.stages if s.name == "graph").candidates_in


def test_graph_expansion_is_seeded_only_by_exact_entity_matches(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """A prose question naming no entity exactly must seed no expansion."""
    seeds = _graph_seeds(
        adversarial, adversarial_root, "scripts", "What prepares this service before startup?"
    )
    assert seeds == 0, f"{seeds} seed(s) from a question that names nothing exactly"


def test_a_prose_question_produces_no_deterministic_graph_evidence(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The concrete symptom: Salt states outranking the shell script that actually
    prepares the service, because a full-text name match became a fact."""
    evidence = _retrieve(
        adversarial, adversarial_root, "scripts", "What prepares this service before startup?"
    )
    graph = [c for c in evidence.candidates if c.source is EvidenceSource.GRAPH]
    assert graph == [], [c.location for c in graph]


def test_naming_an_entity_exactly_still_seeds_expansion(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """The converse: the guarantee must not be bought by disabling the graph.
    `SERVICE_HOST` is an interned entity, and reaching Salt through it is the
    §21 scenario."""
    evidence = _retrieve(
        adversarial, adversarial_root, "python-service", "Where is SERVICE_HOST configured?"
    )
    assert evidence.trace is not None
    graph_stage = next(s for s in evidence.trace.stages if s.name == "graph")
    assert graph_stage.candidates_in > 0, "the named entity must seed expansion"
    assert graph_stage.candidates_out > 0, "expansion must reach something"
    # The Salt state is reached. Its final `source` is whichever retriever made the
    # strongest claim about that span — fusion keeps the best one — so the
    # assertion is about the evidence being present, not about which stage owns it.
    assert any(c.repository == "deployment" for c in evidence.candidates)


def test_an_exact_salt_state_name_still_seeds_expansion(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    assert _graph_seeds(adversarial, adversarial_root, "deployment", "service_config") > 0


@pytest.mark.parametrize(
    ("repo", "question"),
    [
        ("scripts", "What prepares this service before startup?"),
        ("python-service", "how does the backend obtain its address"),
        ("deployment", "where is configuration generated"),
        ("ts-service", "what happens when the client cannot connect"),
        ("python-service", "Where is SERVICE_HOST configured?"),
        ("python-service", "load_config"),
    ],
)
def test_every_deterministic_candidate_can_name_its_proof(
    adversarial: Workspace, adversarial_root: Path, repo: str, question: str
) -> None:
    """Each `DETERMINISTIC` candidate must come from a source that proves something:
    a verified literal, an exact name, or an edge. Nothing else may wear the tier.

    `SYMBOL` appears in both tiers by design — an exact name match is a fact, a
    full-text name match is a lead — which is precisely why the *seed* rule above
    matters. `LEXICAL` is never deterministic.
    """
    evidence = _retrieve(adversarial, adversarial_root, repo, question)
    for candidate in evidence.candidates:
        if candidate.tier is EvidenceTier.DETERMINISTIC:
            assert candidate.source in {
                EvidenceSource.EXACT,
                EvidenceSource.SYMBOL,
                EvidenceSource.GRAPH,
            }, (candidate.source, candidate.location)
        else:
            assert candidate.source is not EvidenceSource.EXACT
            assert candidate.source is not EvidenceSource.GRAPH
