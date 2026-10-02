"""Sync planning logic in isolation (V1_SPEC §12).

Pure unit tests: no filesystem, no git. The hashing callable is injected.
"""

from __future__ import annotations

from datetime import datetime, timezone

from repoatlas.gitstate.client import GitRepositoryState
from repoatlas.models.repository import FileState, Repository, SyncStatus
from repoatlas.sync.planner import RehashDecision, decide_rehash, plan_sync
from repoatlas.sync.scanner import ScannedFile

PARSER_VERSION = "python-ast:1"


def _repo(**overrides: object) -> Repository:
    base = {
        "id": 1,
        "name": "svc",
        "path": "/repos/svc",
        "head_sha": "abc123",
        "last_indexed_at": datetime.now(timezone.utc),
        "last_indexed_head": "abc123",
        "last_indexed_dirty": False,
        "last_sync_status": SyncStatus.OK,
    }
    base.update(overrides)
    return Repository.model_validate(base)


def _state(**overrides: object) -> GitRepositoryState:
    base = {"branch": "main", "head_sha": "abc123", "is_dirty": False, "known": True}
    base.update(overrides)
    return GitRepositoryState(**base)  # type: ignore[arg-type]


def _known(**overrides: object) -> FileState:
    base = {
        "file_id": 1,
        "size": 10,
        "mtime_ns": 100,
        "content_hash": "hash-a",
        "parser_version": PARSER_VERSION,
    }
    base.update(overrides)
    return FileState.model_validate(base)


# -- rehash decision --------------------------------------------------------


def test_no_rehash_when_nothing_changed() -> None:
    decision = decide_rehash(_repo(), _state())
    assert not decision.forced


def test_rehash_on_first_index() -> None:
    decision = decide_rehash(
        _repo(last_sync_status=SyncStatus.NEVER, last_indexed_at=None),
        _state(),
    )
    assert decision.forced
    assert decision.reason == "first index"


def test_rehash_when_head_changed() -> None:
    decision = decide_rehash(_repo(), _state(head_sha="def456"))
    assert decision.forced
    assert decision.reason == "HEAD changed since last index"


def test_rehash_when_dirty_state_changed() -> None:
    decision = decide_rehash(_repo(), _state(is_dirty=True))
    assert decision.forced
    assert decision.reason == "working-tree dirty state changed"


def test_rehash_when_previous_sync_interrupted() -> None:
    decision = decide_rehash(_repo(last_sync_status=SyncStatus.INTERRUPTED), _state())
    assert decision.forced
    assert "did not complete" in (decision.reason or "")


def test_rehash_when_git_state_unknown() -> None:
    decision = decide_rehash(_repo(), GitRepositoryState(known=False))
    assert decision.forced
    assert decision.reason == "git state unknown"


def test_rehash_when_forced() -> None:
    decision = decide_rehash(_repo(), _state(), force=True)
    assert decision.forced
    assert decision.reason == "--full requested"


# -- classification ---------------------------------------------------------


def _plan(scanned, known, *, forced=False, hashes=None):
    lookup = hashes or {}
    return plan_sync(
        scanned,
        known,
        rehash=RehashDecision(forced=forced),
        parser_version=PARSER_VERSION,
        hash_of=lambda rel: lookup.get(rel, "hash-a"),
    )


def test_stat_match_skips_hashing() -> None:
    calls: list[str] = []

    def tracking_hash(rel: str) -> str:
        calls.append(rel)
        return "hash-a"

    plan = plan_sync(
        [ScannedFile("a.py", size=10, mtime_ns=100)],
        {"a.py": _known()},
        rehash=RehashDecision(forced=False),
        parser_version=PARSER_VERSION,
        hash_of=tracking_hash,
    )
    assert plan.unchanged == ["a.py"]
    assert calls == [], "stat match must skip the hash entirely"


def test_forced_rehash_hashes_even_on_stat_match() -> None:
    calls: list[str] = []

    def tracking_hash(rel: str) -> str:
        calls.append(rel)
        return "hash-a"

    plan = plan_sync(
        [ScannedFile("a.py", size=10, mtime_ns=100)],
        {"a.py": _known()},
        rehash=RehashDecision(forced=True, reason="HEAD changed"),
        parser_version=PARSER_VERSION,
        hash_of=tracking_hash,
    )
    assert calls == ["a.py"], "forced rehash must hash despite matching stat"
    assert plan.unchanged == ["a.py"], "identical content is still unchanged"


def test_forced_rehash_detects_content_hidden_by_stat() -> None:
    """The scenario stat cannot see: same size, same mtime, different bytes."""
    plan = _plan(
        [ScannedFile("a.py", size=10, mtime_ns=100)],
        {"a.py": _known()},
        forced=True,
        hashes={"a.py": "hash-DIFFERENT"},
    )
    assert [f.rel_path for f in plan.changed] == ["a.py"]


def test_stat_mismatch_triggers_hash_which_may_show_no_change() -> None:
    """A touched-but-unmodified file costs a hash, not a reparse."""
    plan = _plan(
        [ScannedFile("a.py", size=10, mtime_ns=999)],
        {"a.py": _known()},
        hashes={"a.py": "hash-a"},
    )
    assert plan.unchanged == ["a.py"]
    assert plan.changed == []


def test_stat_mismatch_with_different_hash_is_changed() -> None:
    plan = _plan(
        [ScannedFile("a.py", size=11, mtime_ns=999)],
        {"a.py": _known()},
        hashes={"a.py": "hash-b"},
    )
    assert [f.rel_path for f in plan.changed] == ["a.py"]


def test_new_file_classified() -> None:
    plan = _plan([ScannedFile("new.py", size=1, mtime_ns=1)], {})
    assert [f.rel_path for f in plan.new] == ["new.py"]


def test_deleted_file_classified() -> None:
    plan = _plan([], {"gone.py": _known(file_id=42)})
    assert plan.deleted_file_ids == [42]
    assert plan.deleted_paths == ["gone.py"]


def test_parser_version_change_forces_reparse() -> None:
    """Stored facts are stale by definition when the parser changed."""
    plan = _plan(
        [ScannedFile("a.py", size=10, mtime_ns=100)],
        {"a.py": _known(parser_version="python-ast:0")},
    )
    assert [f.rel_path for f in plan.changed] == ["a.py"]
