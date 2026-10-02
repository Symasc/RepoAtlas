"""Performance sanity, measured with the Slice D instrumentation (V1_SPEC §20).

These are **sanity bounds, not benchmarks**. The rule is "do not prematurely
optimize, but never scan every repository for every question, re-index everything
after one file changes, or re-embed unchanged data" — all of which are *shape*
properties, not speed properties. So most of what is asserted here is shape:
which stages ran, how many rows they touched, and how much the funnel narrowed.

The few wall-clock bounds are deliberately an order of magnitude above what the
fixture set costs, because a test that fails on a loaded CI box teaches people to
ignore it. They exist to catch a change that makes something *categorically*
slower — a full rescan where an incremental pass belonged — not to police
milliseconds.

``--durations`` and ``-s`` print the measured numbers; the traces are the same
ones ``--verbose`` shows a user.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.models.evidence import EvidenceSet
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace

#: Wall-clock ceilings for the fixture set (7 repositories, ~30 files). Roughly
#: 20x the observed cost, so only a categorical regression trips them.
FULL_SYNC_BUDGET_S = 30.0
NO_OP_SYNC_BUDGET_S = 10.0
RETRIEVAL_BUDGET_MS = 2000.0

QUESTIONS = (
    "SERVICE_HOST",
    "Where is SERVICE_HOST configured?",
    "how does the service load its configuration",
    "load_config",
    "which repository provides common-lib",
)


@pytest.fixture
def measured(initialized: Workspace, workspace_root: Path) -> Iterator[Workspace]:
    yield initialized


def _retrieve(workspace: Workspace, question: str, at: Path | None = None) -> EvidenceSet:
    context = workspace.resolver().resolve(at or workspace.root or Path())
    return workspace.retriever().retrieve(question, context)


def _stage(evidence: EvidenceSet, name: str):
    assert evidence.trace is not None
    return next(stage for stage in evidence.trace.stages if stage.name == name)


# -- retrieval ---------------------------------------------------------------


@pytest.mark.parametrize("question", QUESTIONS)
@pytest.mark.timing
def test_retrieval_completes_well_inside_the_budget(measured: Workspace, question: str) -> None:
    started = time.perf_counter()
    evidence = _retrieve(measured, question)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert evidence.trace is not None
    print(f"\n{question!r}: {elapsed_ms:.1f}ms wall, {evidence.trace.total_ms:.1f}ms traced")
    for stage in evidence.trace.stages:
        print(
            f"  {stage.name:<10} {stage.duration_ms:8.3f}ms  "
            f"{stage.candidates_in:>4} -> {stage.candidates_out:<4} {stage.detail}"
        )
    assert elapsed_ms < RETRIEVAL_BUDGET_MS


@pytest.mark.timing
def test_the_trace_accounts_for_the_time_actually_spent(measured: Workspace) -> None:
    """A trace that omits a stage would make a future regression invisible."""
    started = time.perf_counter()
    evidence = _retrieve(measured, "Where is SERVICE_HOST configured?")
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert evidence.trace is not None
    assert {stage.name for stage in evidence.trace.stages} == {
        "exact",
        "file",
        "lexical",
        "symbol",
        "graph",
        "fuse",
        "dedup",
        "rerank",
        "budget",
    }
    # The traced total is a lower bound on the wall time and a large fraction of
    # it: if most of the cost were outside every stage, the instrument would be
    # measuring the wrong thing.
    assert evidence.trace.total_ms <= elapsed_ms + 1.0
    assert evidence.trace.total_ms > elapsed_ms * 0.3


@pytest.mark.parametrize("question", QUESTIONS)
def test_the_funnel_narrows_rather_than_widens(measured: Workspace, question: str) -> None:
    """Every stage after fusion must reduce or preserve the set. A funnel that
    grows at the end is how whole repositories end up in a prompt."""
    evidence = _retrieve(measured, question)
    assert evidence.trace is not None
    for name in ("fuse", "dedup", "rerank", "budget"):
        stage = _stage(evidence, name)
        assert stage.candidates_out <= stage.candidates_in, name


def test_a_scoped_question_reads_less_than_a_global_one(
    measured: Workspace, workspace_root: Path
) -> None:
    """ "Never scan every repo for every question" is enforced by SQL scoping, so
    the scoped run must genuinely consider fewer candidates."""
    scoped = _retrieve(measured, "configuration", workspace_root / "python-service")
    everything = _retrieve(measured, "configuration", workspace_root)
    assert scoped.trace is not None and everything.trace is not None
    assert scoped.trace.scoped_repositories < everything.trace.scoped_repositories or (
        _stage(scoped, "lexical").candidates_out <= _stage(everything, "lexical").candidates_out
    )


def test_graph_expansion_stays_inside_its_bounds(measured: Workspace) -> None:
    """Unbounded traversal over a linked multi-repository graph is the failure mode
    that turns one question into a full scan."""
    limits = measured.settings.retrieval
    for question in QUESTIONS:
        evidence = _retrieve(measured, question)
        assert evidence.trace is not None
        assert evidence.trace.graph_hops <= limits.graph_hops
        assert evidence.trace.graph_nodes_visited <= limits.graph_seeds * limits.graph_fanout


def test_the_evidence_set_never_exceeds_either_budget(measured: Workspace) -> None:
    limits = measured.settings.retrieval
    for question in QUESTIONS:
        evidence = _retrieve(measured, question)
        assert len(evidence.candidates) <= limits.final
        assert evidence.trace is not None
        assert evidence.trace.token_estimate <= limits.token_budget


@pytest.mark.timing
def test_repeated_retrieval_does_not_degrade(measured: Workspace) -> None:
    """A cache that grows unboundedly, or per-call index rebuilding, shows up as a
    late run costing far more than an early one."""
    timings = []
    for _ in range(12):
        started = time.perf_counter()
        _retrieve(measured, "Where is SERVICE_HOST configured?")
        timings.append((time.perf_counter() - started) * 1000.0)
    first_third = sum(timings[:4]) / 4
    last_third = sum(timings[-4:]) / 4
    print(f"\nfirst 4: {first_third:.1f}ms, last 4: {last_third:.1f}ms")
    assert last_third < max(first_third * 5.0, 50.0)


# -- sync ---------------------------------------------------------------------


@pytest.mark.timing
def test_a_full_index_of_the_fixture_set_is_quick(
    workspace: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    from repoatlas.services.init_service import initialize

    started = time.perf_counter()
    initialize(workspace, workspace_root, config_path=tmp_path / "perf-config.yaml")
    elapsed = time.perf_counter() - started
    files = workspace.database.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    print(f"\nfull index: {elapsed:.2f}s for {files} files")
    assert elapsed < FULL_SYNC_BUDGET_S


@pytest.mark.timing
def test_an_unchanged_sync_costs_far_less_than_the_first(
    measured: Workspace, workspace_root: Path
) -> None:
    """ "Never re-index everything after one file changes" — the no-op case is the
    strongest version of that: nothing changed, so nothing should be reparsed."""
    started = time.perf_counter()
    report = sync(measured, measured.resolver().resolve(workspace_root))
    elapsed = time.perf_counter() - started
    print(f"\nno-op sync: {elapsed:.2f}s")

    assert elapsed < NO_OP_SYNC_BUDGET_S
    assert report.files_indexed == 0
    assert all(entry.unchanged > 0 or entry.files_indexed == 0 for entry in report.repositories)


def test_one_changed_file_reindexes_one_file(measured: Workspace, workspace_root: Path) -> None:
    (workspace_root / "python-service" / "src" / "config.py").write_text("X = 1\n")
    report = sync(measured, measured.resolver().resolve(workspace_root))
    assert report.files_indexed == 1
    changed = [entry for entry in report.repositories if entry.touched]
    assert [entry.repository for entry in changed] == ["python-service"]


def test_the_global_link_pass_is_measured_and_bounded(
    measured: Workspace, workspace_root: Path
) -> None:
    """The link pass is global by necessity, so it is the one part of a sync whose
    cost does not shrink with scope. That makes it the thing to watch."""
    report = sync(measured, measured.resolver().resolve(workspace_root / "python-service"))
    trace = report.link_trace
    assert trace is not None
    print(
        f"\nglobal link: {trace.duration_ms:.1f}ms, {trace.claims_considered} claims, "
        f"{trace.files_indexed} files, {trace.symbols_indexed} symbols"
    )
    for stage in trace.stages:
        print(f"  {stage.name:<14} {stage.duration_ms:8.3f}ms  {stage.detail}")

    assert trace.duration_ms > 0.0
    assert trace.claims_considered > 0
    assert sum(stage.duration_ms for stage in trace.stages) <= trace.duration_ms + 1.0


def test_scoped_sync_does_not_touch_other_repositories(
    measured: Workspace, workspace_root: Path
) -> None:
    (workspace_root / "python-service" / "src" / "config.py").write_text("Y = 2\n")
    report = sync(measured, measured.resolver().resolve(workspace_root / "python-service"))
    assert [entry.repository for entry in report.repositories] == ["python-service"]
