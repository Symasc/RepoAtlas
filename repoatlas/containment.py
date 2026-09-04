"""The filesystem containment boundary (V1_SPEC §17, ARCHITECTURE.md §12).

One module, one question: *is this the file the repository says it is?*

It sits beside :mod:`repoatlas.security` and below every layer that must obey it,
for the same reason that one does — enumeration is not its only caller. The
indexer reads file content, and so does the git working-tree comparison, which
reads for an entirely different purpose and must be held to the same boundary. A
second copy of the rule in either place would be a second policy.

**The invariant.**

    Before RepoAtlas opens repository file content, the actual filesystem target
    must be proven to reside within the canonical repository root.

**Why a path check is not enough.** Git's index records *paths*, not objects, and
the two can drift apart completely::

    git add sub/passwd          # sub is a real directory
    rm -rf sub && ln -s /etc sub

``git ls-files --cached`` still reports ``sub/passwd``. The object that path now
names is ``/etc/passwd`` — an ordinary regular file, so ``Path.is_symlink()`` is
``False`` and ``Path.is_file()`` is ``True``. Checking the final component admits
it, and foreign content is then read, chunked, embedded and cited under a
repository path that does not contain it. A tracked Git path is not proof that
the corresponding working-tree object lives inside the repository.

**The policy: no component below the root may be a symlink.** Stated as a rule
about *routes* rather than about destinations, and that is deliberate:

* it is what RepoAtlas already did everywhere it looked. The scanner skipped
  final-component symlinks and the ``os.walk`` fallback skipped symlinked
  directories; the git enumeration path was the one place that never asked;
* it is decidable without trusting a resolved string. ``resolve()`` then
  ``is_relative_to()`` compares a *snapshot* of the filesystem taken before the
  read, which a concurrent rename can invalidate between the check and the open;
* it refuses a link that leaves the root and returns. Such a link resolves inside
  the repository, so a destination test admits it — but its route is one the
  repository controls and can re-aim, and the file it reaches is already
  reachable by its real path. Nothing is lost by refusing it.

**How containment is enforced.** Not by comparing strings. The boundary opens the
resolved root once and then walks the relative path one component at a time with
``openat(..., O_NOFOLLOW)``, so:

* a symlink anywhere in the chain fails the open rather than being followed;
* each directory file descriptor pins an inode, so an ancestor swapped after it
  was checked cannot redirect the read — the object opened is the object that was
  checked, which is what closes the check/use race;
* ``..`` and absolute paths cannot appear, because they are rejected before the
  walk begins rather than resolved away.

Refusal is silent and counted. The count reaches ``--verbose``; the paths do not.
A hostile repository can name any absolute host path it likes, and printing one
back would turn a containment control into a filesystem-disclosure feature.

**What this does not do.** It is a boundary on *reading*, not a statement about
identity. A repository reached through a symlinked root keeps the identity
``repository_slug`` gives it (ARCHITECTURE.md §3); only the containment root is
canonicalized. The two are different questions and are answered separately.
"""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

#: ``O_NOFOLLOW`` makes an open fail rather than traverse a symlink, and
#: ``O_DIRECTORY`` does the same for an intermediate component that is not a
#: directory. Both are POSIX; the ``getattr`` keeps import working on a platform
#: that lacks them, where :func:`_supported` then declines the fast path.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
#: A FIFO opened for reading blocks until a writer appears, which would hang a
#: sync on a repository that contains one. Opening non-blocking and checking the
#: mode afterwards is the standard way to avoid that; for a regular file the flag
#: has no effect.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_READ_CHUNK_BYTES = 1 << 20


def _supported() -> bool:
    """Whether the kernel offers the primitives this boundary is built on.

    Answered by **doing** it rather than by consulting ``os.supports_dir_fd``.
    That set holds the original ``os.open`` object, so membership is an identity
    test: anything that wraps ``os.open`` — a profiler, an audit hook, a sandbox,
    a test harness watching file access — makes the check say "unsupported" and
    silently downgrades this boundary to its weaker, racier fallback. A security
    control that quietly changes implementation because something decorated a
    builtin is a control nobody can reason about, and the downgrade is invisible
    at exactly the moment someone is instrumenting to look for it.

    A functional probe cannot be fooled that way: it asks the platform whether a
    descriptor-relative open works, and any wrapper that preserves semantics
    passes it.

    The answer is deliberately **not** cached. It costs two syscalls per
    boundary — a boundary is built once per repository per operation, so the
    total is invisible — and a cached capability decision is a decision that can
    go stale, taken once under whatever conditions happened to hold the first
    time. For a security control that chooses between a race-free implementation
    and a racy one, asking again is worth two syscalls.
    """
    if not (_O_NOFOLLOW and _O_DIRECTORY):
        return False
    try:
        anchor = os.open(os.sep, os.O_RDONLY | _O_DIRECTORY)
    except OSError:  # pragma: no cover - a platform with no openable root
        return False
    try:
        probe = os.open(".", os.O_RDONLY | _O_DIRECTORY, dir_fd=anchor)
    except (OSError, TypeError, ValueError, NotImplementedError):  # pragma: no cover
        return False
    else:
        os.close(probe)
        return True
    finally:
        os.close(anchor)


def _components(rel_path: str) -> tuple[str, ...] | None:
    """The path split into components, or ``None`` if it may not be walked.

    Git never emits an absolute path, a ``..`` component or an embedded NUL, so
    this is defence against a future caller rather than against git. A
    containment primitive that trusts its input is not a containment primitive.
    """
    if not rel_path or rel_path.startswith("/") or "\0" in rel_path:
        return None
    parts = tuple(part for part in PurePosixPath(rel_path).parts if part != ".")
    if not parts or any(part in ("..", "/", "") for part in parts):
        return None
    return parts


class RepositoryBoundary:
    """Reads files belonging to one repository, and refuses everything else.

    Construct one per repository per operation. It holds no open descriptors
    between calls, so it is cheap to create and safe to discard.
    """

    __slots__ = ("_refused", "_root", "_usable")

    def __init__(self, repo_path: Path) -> None:
        #: The *resolved* root. A repository reached through a symlinked path is
        #: an ordinary deployment — a linked home directory, a mounted workspace
        #: — and every file inside it must stay indexable, so containment is
        #: measured against where the root really is rather than how it was
        #: spelled.
        self._root = repo_path.expanduser().resolve()
        self._refused = 0
        self._usable = _supported()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def refused(self) -> int:
        """How many candidates were refused. Never *which* — see the module docstring."""
        return self._refused

    # -- the walk ---------------------------------------------------------- #

    @contextmanager
    def _descend(self, parts: tuple[str, ...]) -> Iterator[int | None]:
        """Yield a descriptor for the directory holding ``parts[-1]``.

        ``None`` means the chain could not be walked without following a symlink,
        which is the same answer as "not contained": the caller refuses either
        way, and distinguishing them would only invite a caller to treat one as
        recoverable.
        """
        try:
            handle = os.open(self._root, os.O_RDONLY | _O_DIRECTORY)
        except OSError:
            yield None
            return
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(
                        part,
                        os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                        dir_fd=handle,
                    )
                except OSError:
                    yield None
                    return
                os.close(handle)
                handle = nxt
            yield handle
        finally:
            os.close(handle)

    def _reject(self) -> None:
        self._refused += 1

    # -- the operations ---------------------------------------------------- #

    def stat(self, rel_path: str) -> os.stat_result | None:
        """Metadata for a contained regular file, or ``None``.

        ``None`` covers every reason a candidate is unusable — refused by the
        boundary, missing, a symlink, a directory, a device, a FIFO — because the
        caller's response to all of them is the same: do not index it.
        """
        parts = _components(rel_path)
        if parts is None:
            self._reject()
            return None
        if not self._usable:
            return self._fallback_stat(parts)
        with self._descend(parts) as handle:
            if handle is None:
                self._reject()
                return None
            try:
                info = os.stat(parts[-1], dir_fd=handle, follow_symlinks=False)
            except OSError:
                return None
        if not stat_module.S_ISREG(info.st_mode):
            # A symlink lands here too, which is the intended answer: the final
            # component is held to the same rule as every component above it.
            return None
        return info

    @contextmanager
    def open(self, rel_path: str) -> Iterator[int | None]:
        """Yield a read-only descriptor for a contained regular file, or ``None``.

        The descriptor is the boundary's product. Handing back a path would
        reopen it and reintroduce the race this class exists to close.
        """
        parts = _components(rel_path)
        if parts is None:
            self._reject()
            yield None
            return
        if not self._usable:
            with self._fallback_open(parts) as handle:
                yield handle
            return
        with self._descend(parts) as directory:
            if directory is None:
                self._reject()
                yield None
                return
            try:
                handle = os.open(
                    parts[-1],
                    os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK,
                    dir_fd=directory,
                )
            except OSError:
                yield None
                return
            try:
                if not stat_module.S_ISREG(os.fstat(handle).st_mode):
                    yield None
                    return
                yield handle
            finally:
                os.close(handle)

    def read_bytes(self, rel_path: str) -> bytes | None:
        """The file's bytes, or ``None`` if it is not a contained regular file."""
        with self.open(rel_path) as handle:
            if handle is None:
                return None
            try:
                return _read_all(handle)
            except OSError:
                return None

    def read_into(self, rel_path: str, update: object) -> bool:
        """Stream the file through ``update`` (a hash object's method).

        Streaming rather than returning bytes, because the callers that hash do
        not need the content in memory and one of them runs over every tracked
        file in the repository.
        """
        callback = update
        if not callable(callback):  # pragma: no cover - programming error
            raise TypeError("update must be callable")
        with self.open(rel_path) as handle:
            if handle is None:
                return False
            try:
                while block := os.read(handle, _READ_CHUNK_BYTES):
                    callback(block)
            except OSError:
                return False
        return True

    def readlink(self, rel_path: str) -> str | None:
        """The target *string* of a contained symlink, or ``None``.

        Reading a link's value is not reading what it points at, and git stores
        exactly this string as the blob for mode ``120000`` — so the working-tree
        comparison can hash a tracked symlink without the boundary being crossed.
        The link itself must still sit under a chain of real directories.
        """
        parts = _components(rel_path)
        if parts is None:
            self._reject()
            return None
        if not self._usable:
            candidate = self._fallback_path(parts)
            if candidate is None:
                return None
            try:
                # `Path.readlink` cannot take a directory descriptor, and the
                # contained branch below needs one; using the same call in both
                # keeps one readlink semantics rather than two. (PTH115)
                return os.readlink(candidate)  # noqa: PTH115
            except OSError:
                return None
        with self._descend(parts) as handle:
            if handle is None:
                self._reject()
                return None
            try:
                return os.readlink(parts[-1], dir_fd=handle)
            except OSError:
                return None

    def contains(self, path: Path) -> bool:
        """Whether an already-resolved absolute path lies inside this root.

        For callers comparing *directories* — nested-repository ownership — where
        no file is opened and there is nothing to race against.
        """
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return False
        return resolved == self._root or resolved.is_relative_to(self._root)

    # -- the portable fallback --------------------------------------------- #
    #
    # Reached only where the kernel lacks `openat` with `O_NOFOLLOW`. It answers
    # the same question with `lstat` per component, which is correct at the
    # moment it runs and — unlike the descriptor walk — can be invalidated by a
    # concurrent rename. Documented as such rather than silently weaker.

    def _fallback_path(self, parts: tuple[str, ...]) -> Path | None:
        current = self._root
        for part in parts:
            current = current / part
            try:
                if current.is_symlink():
                    self._reject()
                    return None
            except OSError:
                return None
        return current

    def _fallback_stat(self, parts: tuple[str, ...]) -> os.stat_result | None:
        candidate = self._fallback_path(parts)
        if candidate is None:
            return None
        try:
            info = candidate.stat(follow_symlinks=False)
        except OSError:
            return None
        return info if stat_module.S_ISREG(info.st_mode) else None

    @contextmanager
    def _fallback_open(self, parts: tuple[str, ...]) -> Iterator[int | None]:
        candidate = self._fallback_path(parts)
        if candidate is None:
            yield None
            return
        try:
            handle = os.open(candidate, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
        except OSError:
            yield None
            return
        try:
            if not stat_module.S_ISREG(os.fstat(handle).st_mode):
                yield None
                return
            yield handle
        finally:
            os.close(handle)


def _read_all(handle: int) -> bytes:
    blocks: list[bytes] = []
    while block := os.read(handle, _READ_CHUNK_BYTES):
        blocks.append(block)
    return b"".join(blocks)
