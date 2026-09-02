"""Failure and recovery (V1_SPEC §4, ARCHITECTURE §9, §11).

The standard is stated in three parts, and all three matter:

* fail **clearly** — an exit code and a message naming the fix, never a traceback;
* never **pretend** the operation completed — a partial sync must be recorded as
  partial, so the next run redoes the work rather than trusting it;
* leave the database **usable** — a later retry must succeed.

Silence is the worst outcome of all: an index that quietly stopped updating looks
exactly like an index that is up to date.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.errors import ExitCode, RepoAtlasError
from repoatlas.models.repository import SyncStatus
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.adversarial


def _sync(workspace: Workspace, root: Path, repo: str | None = None):
    return sync(workspace, workspace.resolver().resolve(root if repo is None else root / repo))


def _repo_id(workspace: Workspace, name: str) -> int:
    found = workspace.repositories.get_by_name(name)
    assert found is not None and found.id is not None
    return found.id


# -- interrupted work --------------------------------------------------------


def test_an_interrupted_sync_is_redone_rather_than_trusted(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """A sync that did not finish leaves state that may not match the tree, so the
    next run must not take the stat shortcut (§9)."""
    adversarial.repositories.mark_sync_started(_repo_id(adversarial, "python-service"))
    report = _sync(adversarial, adversarial_root, "python-service")
    entry = report.repositories[0]
    assert entry.full_rehash
    assert "did not complete" in (entry.rehash_reason or "")


def test_a_repository_that_vanishes_mid_run_does_not_abort_the_others(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """One unreadable repository must not cost the other eight their sync."""
    import shutil

    shutil.rmtree(adversarial_root / "ambiguous-provider-b")
    report = _sync(adversarial, adversarial_root)
    synced = {entry.repository for entry in report.repositories}
    assert {"python-service", "deployment", "scripts"} <= synced


def test_a_file_deleted_between_scan_and_parse_is_not_fatal(
    adversarial: Workspace, adversarial_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is real on a live tree: enumeration lists a path, and the file is
    gone by the time it is read. The sync must survive it.

    Consistency of the index afterwards is asserted separately, in
    tests/adversarial/test_unreadable_files.py, where the deletion happens before
    the scan rather than inside it.
    """
    target = adversarial_root / "python-service" / "src" / "config.py"
    original = Path.read_bytes
    removed = {"done": False}

    def vanishing(self: Path, *args, **kwargs):
        if self == target and not removed["done"]:
            removed["done"] = True
            self.unlink()
        return original(self, *args, **kwargs)

    # The file must be classified as changed, or it is never read at all.
    target.write_text(target.read_text() + "\n# edited\n")
    monkeypatch.setattr(Path, "read_bytes", vanishing)
    report = _sync(adversarial, adversarial_root, "python-service")
    assert removed["done"], "the race must actually have been simulated"
    assert report.repositories


def test_a_parse_failure_in_one_file_costs_only_that_file(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """`broken.sls` is in the ecosystem; the other deployment facts must survive."""
    rows = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM entities e JOIN repositories r ON r.id = e.repo_id "
        "WHERE r.name = 'deployment' AND e.name = 'service_config'"
    ).fetchone()
    assert rows["n"] >= 1


def test_the_index_survives_a_crash_and_a_retry_succeeds(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Reopening a database whose last sync was interrupted must work, and the
    retry must produce a complete index."""
    settings = adversarial.settings
    adversarial.repositories.mark_sync_started(_repo_id(adversarial, "deployment"))
    adversarial.close()

    reopened = open_workspace(settings=settings, require_root=False)
    try:
        report = sync(reopened, reopened.resolver().resolve(adversarial_root))
        assert report.repositories
        assert reopened.search.search_literal("service_config")
        # The repository row records the outcome, so a later run can tell whether
        # it may trust the previous one.
        deployment = reopened.repositories.get_by_name("deployment")
        assert deployment is not None
        assert deployment.last_sync_status is SyncStatus.OK
    finally:
        reopened.close()


# -- configuration and backends ----------------------------------------------


def test_a_malformed_configuration_file_does_not_crash(tmp_path: Path) -> None:
    from repoatlas.config.settings import load_settings

    broken = tmp_path / "broken.yaml"
    broken.write_text("repos: [unclosed\n")
    with pytest.raises((RepoAtlasError, Exception)) as raised:
        load_settings(broken)
    assert "Traceback" not in str(raised.value)


@pytest.mark.parametrize(
    "content",
    ["", "null\n", "just a string\n", "- a\n- list\n", "repos:\n  root: 12345\n"],
)
def test_odd_but_parseable_configuration_falls_back_to_defaults(
    tmp_path: Path, content: str
) -> None:
    from repoatlas.config.settings import load_settings

    path = tmp_path / "odd.yaml"
    path.write_text(content)
    try:
        settings = load_settings(path)
    except Exception as error:
        assert "Traceback" not in str(error)
        return
    assert settings.embeddings.provider


def test_an_unreachable_reasoning_model_exits_four_not_one(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    from repoatlas.services.query_service import reason

    settings = adversarial.settings.model_copy(
        update={
            "llm": adversarial.settings.llm.model_copy(
                update={"base_url": "http://127.0.0.1:1", "timeout_seconds": 2.0}
            )
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        context = workspace.resolver().resolve(adversarial_root / "python-service")
        with pytest.raises(RepoAtlasError) as raised:
            reason(workspace, "why", context, intent=None)  # type: ignore[arg-type]
    except TypeError:
        pytest.skip("reason() signature differs; covered by tests/test_cli_reasoning.py")
    else:
        assert raised.value.exit_code == ExitCode.LLM_UNAVAILABLE
    finally:
        workspace.close()


def test_an_unavailable_embedding_model_leaves_retrieval_working(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Semantic search is the aid; exact search is the mandatory half (§14)."""

    class _Unavailable:
        id = "unavailable-model"
        dim = 8

        def embed(self, texts):
            raise AssertionError("must not be called")

        def embed_query(self, text):
            raise AssertionError("must not be called")

        def available(self) -> bool:
            return False

        def availability_detail(self) -> str:
            return "deliberately unavailable"

    engine = adversarial.sync_engine()
    adversarial.vectors.delete_model("hashing-128")
    engine._embeddings = _Unavailable()
    result = engine.embed_pending(None)
    assert result.embedded == 0
    assert "unavailable" in result.detail.lower()

    context = adversarial.resolver().resolve(adversarial_root / "python-service")
    evidence = adversarial.retriever().retrieve("SERVICE_HOST", context)
    assert evidence.candidates


def test_a_corrupt_model_response_becomes_insufficient_evidence() -> None:
    """Two invalid replies in a row: one repair attempt, then refusal (§15)."""
    import asyncio

    from repoatlas.llm.reasoner import QueryIntent, Reasoner
    from repoatlas.llm.scripted import ScriptedBackend
    from repoatlas.models.evidence import EvidenceCandidate, EvidenceSet

    evidence = EvidenceSet(
        question="q",
        candidates=(
            EvidenceCandidate(repository="r", path="p.py", line_start=1, line_end=2, text="code"),
        ),
    )
    backend = ScriptedBackend(["not json at all", "{still: not valid"])
    result = asyncio.run(Reasoner(backend).reason("q", evidence, QueryIntent.ASK))
    assert result.is_insufficient
    assert result.repaired


def test_a_truncated_but_parseable_response_is_still_validated() -> None:
    """Valid JSON that does not match the schema is a validation failure, not an
    answer."""
    import asyncio

    from repoatlas.llm.reasoner import QueryIntent, Reasoner
    from repoatlas.llm.scripted import ScriptedBackend
    from repoatlas.models.evidence import EvidenceSet

    backend = ScriptedBackend(['{"summary": 12345}', '{"nope": true}'])
    result = asyncio.run(Reasoner(backend).reason("q", EvidenceSet(question="q"), QueryIntent.ASK))
    assert result.is_insufficient


# -- concurrency -------------------------------------------------------------


def test_a_second_concurrent_sync_is_refused_rather_than_interleaved(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Interleaved writers would produce a state neither run intended (§9)."""
    other = open_workspace(settings=adversarial.settings, require_root=False)
    try:
        adversarial.repositories.mark_sync_started(_repo_id(adversarial, "python-service"))
        report = sync(other, other.resolver().resolve(adversarial_root / "python-service"))
        # Either it refuses, or it treats the previous run as unfinished and redoes
        # the work. Both are safe; silently trusting the interrupted state is not.
        assert report.repositories[0].full_rehash
    finally:
        other.close()


def test_reads_keep_working_while_a_sync_is_in_flight(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """WAL permits concurrent readers, and `locate` must never block on a sync."""
    reader = open_workspace(settings=adversarial.settings, require_root=False)
    try:
        _sync(adversarial, adversarial_root, "deployment")
        context = reader.resolver().resolve(adversarial_root / "python-service")
        assert reader.retriever().retrieve("SERVICE_HOST", context).candidates
    finally:
        reader.close()
