"""Exact search and retrieval (V1_SPEC §14, §19)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.retrieval.query import analyze
from repoatlas.services.retrieval_service import locate
from repoatlas.services.workspace import Workspace


def _locate(workspace: Workspace, question: str, cwd: Path):
    context = workspace.resolver().resolve(cwd)
    return locate(workspace, question, context)


# -- query analysis ---------------------------------------------------------


def test_analysis_extracts_env_var_names() -> None:
    assert analyze("Where is SERVICE_HOST configured?").env_vars == ("SERVICE_HOST",)


def test_analysis_extracts_quoted_strings() -> None:
    assert analyze('why does "connection refused" happen?').quoted == ("connection refused",)


def test_analysis_extracts_paths_and_dotted_names() -> None:
    analysis = analyze("what calls os.getenv in src/config.py?")
    assert "src/config.py" in analysis.paths
    assert "os.getenv" in analysis.dotted


def test_analysis_drops_question_words() -> None:
    assert "where" not in analyze("Where is the thing?").identifiers


# -- exact search -----------------------------------------------------------


def test_exact_search_finds_environment_variable(
    initialized: Workspace, workspace_root: Path
) -> None:
    evidence = _locate(initialized, "Where is SERVICE_HOST configured?", workspace_root)
    paths = {c.path for c in evidence.candidates}
    assert any("config.py" in p for p in paths)


def test_exact_search_is_not_replaced_by_approximation(
    initialized: Workspace,
) -> None:
    """A literal that does not exist must return nothing, not a near match."""
    assert initialized.search.search_literal("TOTALLY_ABSENT_IDENTIFIER") == []


def test_literal_search_verifies_substring(initialized: Workspace) -> None:
    hits = initialized.search.search_literal("SERVICE_HOST")
    assert hits
    assert all("SERVICE_HOST" in hit.text for hit in hits)


def test_symbol_lookup_finds_a_definition(initialized: Workspace) -> None:
    hits = initialized.search.search_entities("load_config", None, None, 10)
    assert any(hit.name == "load_config" for hit in hits)


def test_search_results_carry_line_provenance(initialized: Workspace) -> None:
    """Every fact must be citable as repo/path:line (V1_SPEC §11)."""
    hits = initialized.search.search_literal("SERVICE_HOST")
    for hit in hits:
        assert hit.line_start >= 1
        assert hit.line_end >= hit.line_start
        assert hit.repository
        assert hit.path


def test_evidence_location_formatting(initialized: Workspace, workspace_root: Path) -> None:
    evidence = _locate(initialized, "SERVICE_HOST", workspace_root)
    assert evidence.candidates
    for candidate in evidence.candidates:
        assert ":" in candidate.location
        assert candidate.location.startswith(candidate.repository)


# -- scoping and bias -------------------------------------------------------


def test_primary_repository_is_ranked_first(initialized: Workspace, workspace_root: Path) -> None:
    """Asked inside python-service, its own evidence should lead (V1_SPEC §6)."""
    deep = workspace_root / "python-service" / "src" / "deep" / "nested" / "path"
    evidence = _locate(initialized, "Where is SERVICE_HOST read?", deep)
    assert evidence.primary_repository == "python-service"
    assert evidence.candidates
    assert evidence.candidates[0].repository == "python-service"


def test_repository_scope_still_reaches_other_repositories(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Primary scope is a bias, not a wall (V1_SPEC §6)."""
    deep = workspace_root / "python-service" / "src"
    evidence = _locate(initialized, "SERVICE_HOST", deep)
    repositories = {c.repository for c in evidence.candidates}
    assert "python-service" in repositories
    assert len(repositories) > 1, "must be able to follow evidence into other repos"


def test_global_scope_searches_everything(initialized: Workspace, workspace_root: Path) -> None:
    evidence = _locate(initialized, "SERVICE_HOST", workspace_root)
    assert evidence.scope == "global"
    assert {c.repository for c in evidence.candidates} >= {"python-service"}


def test_evidence_set_is_bounded(initialized: Workspace, workspace_root: Path) -> None:
    """Never send a whole repository to the model (V1_SPEC §14)."""
    evidence = _locate(initialized, "def", workspace_root)
    assert len(evidence.candidates) <= 12


def test_unmatched_question_returns_no_evidence(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Insufficient evidence is reported, never fabricated."""
    evidence = _locate(initialized, "ZZZ_NONEXISTENT_TOKEN_QQQ", workspace_root)
    assert evidence.candidates == ()


def test_deduplication_removes_contained_ranges(
    initialized: Workspace, workspace_root: Path
) -> None:
    evidence = _locate(initialized, "SERVICE_HOST", workspace_root)
    seen = {(c.repository, c.path, c.line_start, c.line_end) for c in evidence.candidates}
    assert len(seen) == len(evidence.candidates)
