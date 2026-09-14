"""What inspection refuses to disclose, and how identically it refuses (Slice E).

The rule this file exists to hold:

    Inspection may disclose only content belonging to a file already admitted
    into the RepoAtlas index.

The admission gate is a ``files`` row, and it is consulted **before** any
repository file is opened. That ordering is the whole control: a check performed
after a read is not a boundary, it is a filter on something already in memory.

Every refusal is the same refusal. A secret, an ignored path, a file owned by a
nested repository, a source file added since the last sync, a path that tries to
climb out of the repository, and a name that never existed all produce
``FILE_NOT_INDEXED`` with nothing to tell them apart. That is not a simplification
of a richer truth — V1's admission boundary decided these files are not
RepoAtlas's to talk about, and a capability that answered "that one exists but is
excluded" would convert a containment control into a way to probe the filesystem
one path at a time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.errors import FileNotIndexedError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.services.workspace import Workspace

SERVICE = "python-service"
PLANTED_VALUE = "super-secret-value-that-must-not-be-indexed"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _repo(slug: str = SERVICE) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _inspect_path(workspace: Workspace, path: str, slug: str = SERVICE) -> object:
    return _service(workspace).inspect(
        InspectRequest(context=_repo(slug), target=InspectTarget(path=path))
    )


# ---------------------------------------------------------------------------
# One refusal for every unadmitted path
# ---------------------------------------------------------------------------


def test_a_secret_file_that_physically_exists_is_not_indexed_and_not_readable(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 5. The file is right there on disk, and inspection cannot reach it."""
    secret = workspace_root / SERVICE / ".env"
    assert secret.exists()
    assert PLANTED_VALUE in secret.read_text(encoding="utf-8")

    with pytest.raises(FileNotIndexedError) as raised:
        _inspect_path(initialized, ".env")

    # The refusal says nothing about the file: not that it exists, not its size,
    # not one byte of it.
    message = f"{raised.value.message} {raised.value.detail}"
    assert PLANTED_VALUE not in message
    assert ".env" not in message


def test_an_ignored_path_is_refused_exactly_like_a_missing_one(
    initialized: Workspace, workspace_root: Path
) -> None:
    """``generated/`` is excluded by ``.repoatlasignore`` and exists on disk."""
    ignored = workspace_root / SERVICE / "generated" / "ignored.py"
    assert ignored.exists()

    excluded = pytest.raises(FileNotIndexedError)
    with excluded as from_ignore:
        _inspect_path(initialized, "generated/ignored.py")
    with pytest.raises(FileNotIndexedError) as from_nothing:
        _inspect_path(initialized, "generated/never-existed.py")

    assert type(from_ignore.value) is type(from_nothing.value)
    assert from_ignore.value.message == from_nothing.value.message
    assert from_ignore.value.detail == from_nothing.value.detail


def test_a_file_added_since_the_last_sync_is_not_indexed(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 6. An ordinary, innocent source file. Still not RepoAtlas's to show.

    Nothing about it is suspicious, which is the point: admission is decided by
    the index rather than by how the path looks.
    """
    added = workspace_root / SERVICE / "src" / "brand_new.py"
    added.write_text("VALUE = 'written after the last sync'\n", encoding="utf-8")

    with pytest.raises(FileNotIndexedError):
        _inspect_path(initialized, "src/brand_new.py")


def test_a_file_owned_by_a_nested_repository_is_not_the_outer_repository_to_show(
    initialized: Workspace,
) -> None:
    """Ownership follows the index, not the directory tree.

    ``tools/nested-inner/inner.py`` sits inside ``nested-outer`` on disk and
    belongs to ``nested-inner`` in the index. Asking the outer repository for it
    is asking for a file it does not own.
    """
    with pytest.raises(FileNotIndexedError):
        _inspect_path(initialized, "tools/nested-inner/inner.py", slug="nested-outer")

    owned = _service(initialized).inspect(
        InspectRequest(
            context=_repo("nested-outer/tools/nested-inner"),
            target=InspectTarget(path="inner.py"),
        )
    )
    assert owned.file.repository_slug == "nested-outer/tools/nested-inner"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "../.env",
        "src/../../.env",
        "..",
        "src/..",
        # These two collapse *lexically* onto a file that really is indexed, which
        # is what makes normalizing `..` away rather than refusing it unsafe: a
        # path component can be a symlink, so the collapsed path and the real one
        # are not the same file.
        "src/deep/../config.py",
        "src/../src/config.py",
    ],
)
def test_a_traversal_path_never_reaches_a_repository(initialized: Workspace, hostile: str) -> None:
    """Refused twice over, and the second refusal is the one that matters.

    The request model rejects the shape, so a malformed request is reported as
    malformed rather than silently becoming a lookup miss. And the service refuses
    it again when the model is bypassed, because ``files.rel_path`` never holds a
    path with a ``..`` segment — so there is nothing for such a path to resolve to
    even if it arrives.
    """
    with pytest.raises(ValueError):
        InspectTarget(path=hostile)

    bypassed = InspectTarget.model_construct(path=hostile)
    with pytest.raises(FileNotIndexedError):
        _service(initialized).inspect(
            InspectRequest.model_construct(context=_repo(), target=bypassed, context_lines=0)
        )


@pytest.mark.parametrize(
    "hostile", ["/etc/passwd", "/var/lib/repoatlas/x", "C:/Windows/system.ini"]
)
def test_an_absolute_host_path_never_reaches_a_repository(
    initialized: Workspace, hostile: str
) -> None:
    with pytest.raises(ValueError):
        InspectTarget(path=hostile)

    bypassed = InspectTarget.model_construct(path=hostile)
    with pytest.raises(FileNotIndexedError):
        _service(initialized).inspect(
            InspectRequest.model_construct(context=_repo(), target=bypassed, context_lines=0)
        )


def test_a_null_byte_in_a_path_is_refused() -> None:
    """A path that could truncate inside a syscall never becomes one."""
    with pytest.raises(ValueError):
        InspectTarget(path="src/config.py\x00.txt")


def test_a_symlink_that_leaves_the_repository_discloses_nothing(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Two independent reasons this fails, and both are load-bearing.

    The link has no ``files`` row, so the admission gate refuses it before any
    read. And even with a row, the containment boundary walks the path one
    component at a time with ``O_NOFOLLOW``, so the open itself would fail rather
    than follow the link out of the repository.
    """
    outside = workspace_root.parent / "outside-secret.txt"
    outside.write_text("a value from outside every repository\n", encoding="utf-8")
    link = workspace_root / SERVICE / "src" / "escape.py"
    link.symlink_to(outside)

    with pytest.raises(FileNotIndexedError):
        _inspect_path(initialized, "src/escape.py")


def test_a_symlinked_directory_component_discloses_nothing(
    initialized: Workspace, workspace_root: Path
) -> None:
    """A tracked path is not proof that the object it names is inside the tree."""
    outside = workspace_root.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    (outside / "config.py").write_text("STOLEN = True\n", encoding="utf-8")
    (workspace_root / SERVICE / "src" / "linked").symlink_to(outside)

    with pytest.raises(FileNotIndexedError):
        _inspect_path(initialized, "src/linked/config.py")


# ---------------------------------------------------------------------------
# The gate runs before the read
# ---------------------------------------------------------------------------


def test_no_repository_file_is_opened_before_the_files_row_is_found(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering *is* the boundary.

    A containment check that happens after the bytes are in memory is a filter on
    something already read. Asserted by making every content read explode: an
    unindexed path must be refused without one ever being attempted.
    """
    from repoatlas.containment import RepositoryBoundary

    def explode(*_: object, **__: object) -> object:
        raise AssertionError("a repository file was opened before admission")

    monkeypatch.setattr(RepositoryBoundary, "read_content", explode)
    monkeypatch.setattr(RepositoryBoundary, "read_into", explode)
    monkeypatch.setattr(RepositoryBoundary, "open", explode)
    monkeypatch.setattr(RepositoryBoundary, "stat", explode)

    for path in (".env", "generated/ignored.py", "does/not/exist.py"):
        with pytest.raises(FileNotIndexedError):
            _inspect_path(initialized, path)


def test_a_secret_cannot_be_reached_through_any_target_form(initialized: Workspace) -> None:
    """The gate is on the file, not on the spelling of the request."""
    service = _service(initialized)
    for path in (".env", "./.env", ".//.env"):
        with pytest.raises(FileNotIndexedError):
            service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=path)))


def test_no_secret_value_survives_anywhere_in_a_successful_response(
    initialized: Workspace,
) -> None:
    """Inspection reads admitted rows only, so exclusion holds by construction.

    Nothing in this capability re-reads the filesystem for anything but a file
    that already has a row, which is why this is a property of the index rather
    than a filter that could be forgotten here.
    """
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path="src/config.py"))
    )
    assert PLANTED_VALUE not in response.model_dump_json()


def test_inspection_never_shells_out(initialized: Workspace) -> None:
    """Files are read through the containment boundary, never through a process."""
    for module in ("repoatlas/capabilities/service.py", "repoatlas/capabilities/inspection.py"):
        source = Path(module).read_text(encoding="utf-8")
        for marker in ("subprocess", "os.system", "os.popen", "shell=True"):
            assert marker not in source, f"{module} names {marker}"
