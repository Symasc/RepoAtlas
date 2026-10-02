"""V1 ``status`` output is unchanged by the typed freshness model (V1_SPEC §16).

``index_state`` is a public string. It is printed in the status table, emitted in
``--json``, and — this is the part that makes it load-bearing rather than
cosmetic — ``status`` derives its *aggregate* verdict by inspecting these very
strings. A single changed character therefore changes machine-readable output and
can change the overall verdict.

So the strings are pinned here, one per typed state, character for character,
against the branch of the old ``_index_state`` that produced each. The typed model
is free to be more precise than the sentence it renders; the sentence is not free
to move.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from repoatlas.freshness import legacy_index_state, repository_freshness
from repoatlas.gitstate.client import GitRepositoryState
from repoatlas.models.freshness import FreshnessState
from repoatlas.models.reports import StatusReport
from repoatlas.models.repository import Repository, SyncStatus
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace

INDEXED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
HEAD = "a" * 40

#: The complete historical vocabulary of ``index_state``, keyed by the typed state
#: that replaced the branch producing it. Every value is the exact literal that
#: appeared in ``services/status_service.py`` before the refactor.
V1_INDEX_STATE_STRINGS: dict[FreshnessState, str] = {
    FreshnessState.NOT_INDEXED: "not indexed",
    FreshnessState.UNAVAILABLE: "unavailable (git could not enumerate the repository)",
    FreshnessState.INTERRUPTED: "interrupted",
    FreshnessState.STALE_BUILD: "stale (indexed by an older build)",
    FreshnessState.UNKNOWN: "unknown",
    FreshnessState.STALE_HEAD: "stale (HEAD changed)",
    FreshnessState.STALE_WORKTREE: "stale (working tree changed)",
    FreshnessState.DIRTY: "current (dirty working tree)",
    FreshnessState.CURRENT_CLEAN: "current",
    # Unreachable from `status`, which always resolves at LIVE depth. Rendered as
    # the honest word rather than left to raise: at STORED depth HEAD was never
    # consulted, and "unknown" is what V1's vocabulary has for that.
    FreshnessState.INDEXED: "unknown",
}


def _repo(
    *,
    status_value: SyncStatus = SyncStatus.OK,
    indexed: datetime | None = INDEXED_AT,
    indexed_dirty: bool | None = False,
) -> Repository:
    return Repository(
        id=1,
        name="service",
        slug="team/service",
        path=Path("/managed/team/service"),
        last_indexed_at=indexed,
        last_indexed_head=HEAD,
        last_indexed_dirty=indexed_dirty,
        last_sync_status=status_value,
    )


def _live(
    *, head: str | None = HEAD, dirty: bool = False, known: bool = True
) -> GitRepositoryState:
    return GitRepositoryState(branch="main", head_sha=head, is_dirty=dirty, known=known)


#: ``(label, repository, outdated files, live git state, expected V1 string)``,
#: one row per branch the old implementation had.
LEGACY_CASES = [
    ("never indexed", _repo(indexed=None), 0, _live(), "not indexed"),
    (
        "enumeration failed",
        _repo(status_value=SyncStatus.FAILED),
        0,
        _live(),
        "unavailable (git could not enumerate the repository)",
    ),
    ("interrupted", _repo(status_value=SyncStatus.INTERRUPTED), 0, _live(), "interrupted"),
    ("never run", _repo(status_value=SyncStatus.NEVER), 0, _live(), "interrupted"),
    ("older build", _repo(), 4, _live(), "stale (indexed by an older build)"),
    ("git unknown", _repo(), 0, _live(known=False), "unknown"),
    ("head moved", _repo(), 0, _live(head="b" * 40), "stale (HEAD changed)"),
    (
        "became dirty",
        _repo(indexed_dirty=False),
        0,
        _live(dirty=True),
        "stale (working tree changed)",
    ),
    (
        "became clean",
        _repo(indexed_dirty=True),
        0,
        _live(dirty=False),
        "stale (working tree changed)",
    ),
    (
        "still dirty",
        _repo(indexed_dirty=True),
        0,
        _live(dirty=True),
        "current (dirty working tree)",
    ),
    ("clean and current", _repo(indexed_dirty=False), 0, _live(), "current"),
    (
        "dirty never recorded",
        _repo(indexed_dirty=None),
        0,
        _live(dirty=True),
        "current (dirty working tree)",
    ),
    ("clean, dirty never recorded", _repo(indexed_dirty=None), 0, _live(), "current"),
]


def test_every_legacy_branch_still_renders_its_exact_string() -> None:
    """The golden pin. Each row is one branch of the pre-refactor function."""
    rendered = {
        label: legacy_index_state(repository_freshness(repo, outdated_files=outdated, live=live))
        for label, repo, outdated, live in ((c[0], c[1], c[2], c[3]) for c in LEGACY_CASES)
    }
    expected = {case[0]: case[4] for case in LEGACY_CASES}
    assert rendered == expected


def test_the_precedence_between_branches_is_unchanged() -> None:
    """An older build outranks git state, and both outrank a clean tree.

    The order matters: a facts-version change makes stored facts stale while HEAD,
    mtime and content hash all still agree, so checking git first would report
    such an index as current (V1_SPEC §12).
    """
    stale_build_and_moved_head = repository_freshness(
        _repo(), outdated_files=1, live=_live(head="b" * 40)
    )
    assert legacy_index_state(stale_build_and_moved_head) == "stale (indexed by an older build)"

    interrupted_and_outdated = repository_freshness(
        _repo(status_value=SyncStatus.INTERRUPTED), outdated_files=9, live=_live()
    )
    assert legacy_index_state(interrupted_and_outdated) == "interrupted"

    never_indexed_and_failed = repository_freshness(
        _repo(status_value=SyncStatus.FAILED, indexed=None), outdated_files=9, live=_live()
    )
    assert legacy_index_state(never_indexed_and_failed) == "not indexed"


def test_every_typed_state_renders_a_pinned_string() -> None:
    """Total, so a new state cannot ship without deciding what V1 prints for it."""
    for state in FreshnessState:
        assert state in V1_INDEX_STATE_STRINGS, state
        freshness = repository_freshness(_repo(), outdated_files=0, live=_live()).model_copy(
            update={"state": state}
        )
        assert legacy_index_state(freshness) == V1_INDEX_STATE_STRINGS[state]


def test_the_rendered_vocabulary_has_not_grown_or_shrunk() -> None:
    """The set of sentences ``status`` can print is itself the contract."""
    assert set(V1_INDEX_STATE_STRINGS.values()) == {
        "not indexed",
        "unavailable (git could not enumerate the repository)",
        "interrupted",
        "stale (indexed by an older build)",
        "unknown",
        "stale (HEAD changed)",
        "stale (working tree changed)",
        "current (dirty working tree)",
        "current",
    }


def test_the_aggregate_still_reads_the_strings_it_always_did() -> None:
    """``status``'s overall verdict is derived from ``index_state``, unchanged."""
    for state, rendered in V1_INDEX_STATE_STRINGS.items():
        if state is FreshnessState.STALE_BUILD:
            assert rendered.startswith("stale")
        if state is FreshnessState.CURRENT_CLEAN:
            assert rendered == "current"


# -- end to end -------------------------------------------------------------------


def test_a_synced_workspace_reports_current_in_json_and_in_the_model(
    initialized: Workspace, workspace_root: Path
) -> None:
    context = initialized.resolver().resolve(workspace_root)
    sync(initialized, context)

    report = status(initialized, initialized.resolver().resolve(workspace_root))
    assert isinstance(report, StatusReport)
    assert report.index_state == "current"
    assert {r.index_state for r in report.repositories} == {"current"}

    payload = json.loads(report.model_dump_json())
    assert payload["index_state"] == "current"
    assert all(entry["index_state"] == "current" for entry in payload["repositories"])
    # The V1 field stays a plain string, not an enum, so `--json` is byte-stable.
    assert isinstance(payload["index_state"], str)


def test_a_dirty_workspace_still_reports_v1s_exact_dirty_sentence(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The sentence V2's model refuses to believe is still the sentence V1 prints."""
    sync(initialized, initialized.resolver().resolve(workspace_root))
    (workspace_root / "python-service" / "src" / "config.py").write_text(
        "SERVICE_HOST = 'edited'\n", encoding="utf-8"
    )
    sync(initialized, initialized.resolver().resolve(workspace_root))

    report = status(initialized, initialized.resolver().resolve(workspace_root))
    states = {r.name: r.index_state for r in report.repositories}
    assert states["python-service"] == "current (dirty working tree)"
