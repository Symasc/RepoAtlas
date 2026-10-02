"""The deterministic sync-capability evaluation corpus (V2 Slice I, §32).

A fifth corpus, separate from retrieval, graph, dependency and grounding, because
it measures the one capability that writes. Its questions are not about ranking or
provenance but about effect: did the index change by exactly what the working tree
changed by, did nothing the admission policy refuses get in, and did evidence held
by a live investigation move to exactly the status the new index justifies.

Every case mutates, so every case builds its own copy of the adversarial
repositories. There is one parametrized test per case, asserting everything that
case expects at once, which keeps the corpus to one build per case rather than one
per assertion.

Ground truth lives in ``sync_cases.yaml`` and was written by reasoning about the
setup, never copied from a run. No model is consulted and nothing is embedded.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.errors import RepoAtlasError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.sync import SyncRequest, SyncResponse, SyncStatus
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.sync.lock import writer_lock
from tests.conftest import git
from tests.evaluation.harness import evaluation_workspace

CASES_PATH = Path(__file__).parent / "sync_cases.yaml"

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: Every category §32 names. The corpus must cover all of them.
REQUIRED_CATEGORIES = frozenset(
    {
        "noop",
        "change",
        "removal",
        "dirty",
        "admission",
        "evidence",
        "contention",
        "initialization",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class SyncCase:
    id: str
    category: str
    setup: tuple[dict[str, Any], ...]
    evidence: dict[str, Any] | None
    uninitialized: bool
    expect: dict[str, Any]
    notes: str


def load_sync_cases() -> tuple[SyncCase, ...]:
    raw: dict[str, Any] = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases = tuple(
        SyncCase(
            id=entry["id"],
            category=entry["category"],
            setup=tuple(entry.get("setup") or ()),
            evidence=entry.get("evidence"),
            uninitialized=bool(entry.get("uninitialized", False)),
            expect=dict(entry["expect"]),
            notes=entry.get("notes", ""),
        )
        for entry in raw["cases"]
    )
    if len({case.id for case in cases}) != len(cases):
        raise AssertionError("sync case ids must be unique")
    return cases


CASES = load_sync_cases()


# ---------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _hashes(workspace: Workspace, slug: str, paths: list[str]) -> dict[str, str]:
    repo = next(r for r in workspace.repositories.list_all() if r.slug == slug)
    assert repo.id is not None
    return {p: s.content_hash for p, s in workspace.files.states_for(repo.id, paths).items()}


def _apply(root: Path, step: dict[str, Any]) -> None:
    if "append" in step:
        spec = step["append"]
        path = root / spec["repo"] / spec["path"]
        path.write_text(path.read_text(encoding="utf-8") + "\n# appended by evaluation\n")
    elif "delete" in step:
        spec = step["delete"]
        (root / spec["repo"] / spec["path"]).unlink()
    elif "write" in step:
        spec = step["write"]
        path = root / spec["repo"] / spec["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec["text"], encoding="utf-8")
    elif "commit" in step:
        repo = root / step["commit"]["repo"]
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "evaluation setup")
    elif "hold_writer_lock" in step:
        return  # applied around the sync call itself
    else:
        raise AssertionError(f"unknown setup step: {step}")


@contextmanager
def _held(workspace: Workspace) -> Iterator[None]:
    """Another writer holds V1's lock, and this sync refuses rather than waits."""
    workspace.settings = workspace.settings.model_copy(
        update={
            "storage": workspace.settings.storage.model_copy(update={"lock_timeout_seconds": 0.0})
        }
    )
    with writer_lock(Path(workspace.database.path), timeout=0.0):
        yield


def _run_uninitialized(case: SyncCase, tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        service = CapabilityService(workspace)
        with pytest.raises(RepoAtlasError) as raised:
            service.sync(SyncRequest(context=GLOBAL))
        error = capability_error(raised.value)
        assert error.code == case.expect["error"], case.notes
        assert error.retryable is case.expect["retryable"]
        assert workspace.root is None
        assert workspace.repositories.list_all() == []
        assert len(service.sessions) == 0
    finally:
        workspace.close()


def _check(
    case: SyncCase,
    workspace: Workspace,
    service: CapabilityService,
    response: SyncResponse,
    *,
    tree_before: dict[str, bytes],
    root: Path,
    hashes_before: dict[str, str],
    investigation: str | None,
    cited: str | None,
) -> None:
    expect = case.expect
    assert response.status is SyncStatus(expect["status"]), case.notes

    if expect.get("no_changes"):
        for view in response.repositories:
            assert (view.changed, view.new, view.deleted) == (0, 0, 0), view.repository_slug

    slug = expect.get("repository")
    if slug is not None:
        view = next(v for v in response.repositories if v.repository_slug == slug)
        if "counts" in expect:
            counts = expect["counts"]
            assert (view.changed, view.new, view.deleted) == (
                counts["changed"],
                counts["new"],
                counts["deleted"],
            ), case.notes
        if "dirty" in expect:
            assert view.is_dirty is expect["dirty"], case.notes
        indexed = set(
            _hashes(workspace, slug, [*expect.get("indexed", []), *expect.get("not_indexed", [])])
        )
        for path in expect.get("indexed", []):
            assert path in indexed, f"{case.id}: {path} should be indexed"
        for path in expect.get("not_indexed", []):
            assert path not in indexed, f"{case.id}: {path} must not be indexed"
        if "hash_changed" in expect:
            after = _hashes(workspace, slug, expect["hash_changed"])
            for path in expect["hash_changed"]:
                assert after.get(path) != hashes_before.get(path), f"{case.id}: {path}"

    if expect.get("tree_unchanged"):
        assert _tree(root) == tree_before, f"{case.id}: sync modified the working tree"

    if investigation is not None:
        assert cited is not None
        entry = service.sessions._sessions[investigation].ledger.get(cited)
        assert entry is not None
        assert entry.status.value == expect["evidence_status"], case.notes
        reported = {
            f"{t.previous.value}->{t.current.value}": t.count
            for t in response.evidence_revalidation.transitions
        }
        assert reported == dict(expect["transitions"]), case.notes
        assert response.evidence_revalidation.investigations_checked == 1
    else:
        assert len(service.sessions) == 0, "sync created an investigation"


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_sync_outcome_is_exactly_the_expected_one(case: SyncCase, tmp_path: Path) -> None:
    if case.uninitialized:
        _run_uninitialized(case, tmp_path)
        return

    with evaluation_workspace() as (workspace, root):
        service = CapabilityService(workspace)
        investigation: str | None = None
        cited: str | None = None
        if case.evidence is not None:
            spec = case.evidence
            inspected = service.inspect(
                InspectRequest(
                    context=CapabilityContext(
                        scope=CapabilityScope.REPOSITORY, repository=spec["repo"]
                    ),
                    target=InspectTarget(path=spec["path"]),
                    line_start=spec["line_start"],
                    line_end=spec["line_end"],
                    context_lines=0,
                    allow_worktree=False,
                )
            )
            assert len(inspected.segments) == 1, f"{case.id}: the corpus assumes one segment"
            investigation, cited = inspected.investigation_id, inspected.segments[0].id

        slug = case.expect.get("repository")
        hashes_before = (
            _hashes(workspace, slug, case.expect.get("hash_changed", [])) if slug else {}
        )
        for step in case.setup:
            _apply(root, step)
        tree_before = _tree(root)
        generation = service.generation
        holding = any("hold_writer_lock" in step for step in case.setup)

        if "error" in case.expect:
            with (
                _held(workspace) if holding else nullcontext(),
                pytest.raises(RepoAtlasError) as raised,
            ):
                service.sync(SyncRequest(context=GLOBAL))
            error = capability_error(raised.value)
            assert error.code == case.expect["error"], case.notes
            assert error.retryable is case.expect["retryable"]
            assert service.generation == generation, "a refused sync moved the generation"
            return

        response = service.sync(SyncRequest(context=GLOBAL))
        _check(
            case,
            workspace,
            service,
            response,
            tree_before=tree_before,
            root=root,
            hashes_before=hashes_before,
            investigation=investigation,
            cited=cited,
        )


def test_the_corpus_covers_every_required_category() -> None:
    covered = {case.category for case in CASES}
    assert covered >= REQUIRED_CATEGORIES, REQUIRED_CATEGORIES - covered


def test_every_exclusion_case_carries_a_control() -> None:
    """A refusal is only evidence of policy if the same run indexed something else."""
    for case in CASES:
        if case.expect.get("not_indexed") and case.category == "admission":
            assert case.expect.get("indexed"), f"{case.id} has no visible control"


def test_the_corpus_imports_no_model_or_embedding_dependency() -> None:
    """Checked on imports, not text — the assertion itself has to name what it forbids."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith("repoatlas.llm") for name in imported), imported
    assert not any(name.split(".")[0] == "fastembed" for name in imported), imported
