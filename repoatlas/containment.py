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

**The leaf is held to two further conditions.** It must be a regular file, and it
must have exactly one link. A hardlink is not a route out — the inode really is
linked into the tree, so the walk never leaves it — but the same bytes are then
reachable under a name RepoAtlas cannot see and did not authorize, which is the
question containment exists to answer. This was measured before it was adopted:
across three real repositories, 0 of 3,288 indexable files had ``st_nlink > 1``,
while all 82,293 hardlinks present were inside ``.venv`` and ``node_modules`` —
directories the exclusion set removes long before the boundary sees them.

**Content is verified to have held still.** The ancestor race is closed by the
descriptor walk, but a regular file may still be rewritten while it is being
read. Both readers ``fstat`` the open descriptor before and after and compare
``(st_dev, st_ino, st_size, st_mtime_ns)``; a change means the bytes in hand are
a splice of two versions that never existed on disk, and they are refused rather
than hashed. This does not prevent the write — nothing can — but it stops the
result from being stored as though it were a file state.

**Content is read under a ceiling, and the ceiling is not negotiable.** A
repository decides how large its files are, so a size limit that is applied
*after* the bytes arrive is not a limit at all: it reports that a 600 MB file was
too big, having already allocated 600 MB to find out. :meth:`read_content`
therefore takes ``max_bytes`` as a required keyword and enforces it twice —

* a ``fstat`` on the descriptor about to be read answers "too large" for nothing,
  so a five-gigabyte sparse file costs one syscall and zero content bytes;
* the read itself stops after ``max_bytes + 1`` bytes whatever that ``fstat``
  said, because the file can grow between the two calls, ``st_size`` can be stale,
  and a repository can arrange either on purpose.

Neither bound alone is enough. The first is only a hint about a mutable object;
the second alone would read every huge file to its cap before rejecting it. And
crossing the ceiling *is* the answer — the buffer is discarded, so no caller ever
receives a prefix of a file that kept going. A prefix is not the file, and facts
derived from one would be true of nothing.

There is deliberately **no unbounded read of repository content** on this class.
Asking for bytes means naming a limit, so no caller can omit one by accident, and
a future caller cannot pick the wrong primitive from two. Hashing is the one
thing that legitimately consumes a whole huge file, and :meth:`read_into` does it
in fixed blocks without ever holding the file: bounded *memory* for unbounded
*content*, which is a different guarantee and has its own method to say so.

**There is no fallback.** Where ``openat``/``O_NOFOLLOW`` are unavailable,
containment cannot be proven and nothing is admitted. An earlier revision
degraded to an ``lstat``-per-component walk, which is correct at the instant it
runs but can be invalidated by a concurrent rename — a security control quietly
becoming racy on some platforms is worse than one that plainly does nothing, and
an empty index is an obvious failure where a racy one is not.

**What this does not do.** It is a boundary on *reading*, not a statement about
identity. A repository reached through a symlinked root keeps the identity
``repository_slug`` gives it (ARCHITECTURE.md §3); only the containment root is
canonicalized. The two are different questions and are answered separately.
"""

from __future__ import annotations

import enum
import os
import stat as stat_module
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: ``O_NOFOLLOW`` makes an open fail rather than traverse a symlink, and
#: ``O_DIRECTORY`` does the same for an intermediate component that is not a
#: directory. Both are POSIX; the ``getattr`` keeps import working on a platform
#: that lacks them, where :func:`_supported` then declines and the boundary
#: admits nothing.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
#: A FIFO opened for reading blocks until a writer appears, which would hang a
#: sync on a repository that contains one. Opening non-blocking and checking the
#: mode afterwards is the standard way to avoid that; for a regular file the flag
#: has no effect.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_READ_CHUNK_BYTES = 1 << 20


class ContentStatus(enum.Enum):
    """Why :meth:`RepositoryBoundary.read_content` did or did not yield bytes."""

    #: The whole file, within the caller's limit, unchanged across the read.
    OK = "ok"
    #: Larger than the caller's limit. No bytes are returned and none are kept.
    OVERSIZED = "oversized"
    #: Not a contained regular file, or unreadable. Indistinguishable on purpose.
    REFUSED = "refused"
    #: The file was rewritten while it was being read.
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class FileContent:
    """The outcome of asking the boundary for a file's bytes.

    A result object rather than ``bytes | None`` because "too large" and "not
    allowed" are different answers that callers act on differently, and because a
    caller that has to name a limit to get bytes at all cannot forget to.
    """

    status: ContentStatus
    #: Present only when :attr:`status` is ``OK``.
    data: bytes | None = None
    #: For ``OVERSIZED``, what was observed: the file's own size when that alone
    #: decided it, or ``limit + 1`` when the ceiling stopped a growing read.
    size: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is ContentStatus.OK


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

    def _admit(self, rel_path: str) -> tuple[str, ...] | None:
        """Split a candidate into components, or refuse it.

        Also the single place the capability check is enforced. When the
        descriptor walk is unavailable there is no weaker path to fall back to:
        containment cannot be proven, so nothing is admitted. Failing closed
        makes an unsupported kernel an obvious empty index rather than a
        silently racy one.
        """
        if not self._usable:
            self._reject()
            return None
        parts = _components(rel_path)
        if parts is None:
            self._reject()
            return None
        return parts

    def _leaf_ok(self, info: os.stat_result) -> bool:
        """Whether the object the walk arrived at may be read.

        Two conditions, both refusals rather than errors:

        * it must be a **regular file**. A symlink lands here too, which is the
          intended answer: the final component is held to the same rule as every
          component above it.
        * it must have **exactly one link**. A hardlink is not a route out of the
          tree — the inode really is linked into it, so the walk never leaves —
          but it does mean the same bytes are reachable under a name RepoAtlas
          cannot see and did not authorize, which is the thing containment
          exists to decide. Measured before adopting: across three real
          repositories, 0 of 3,288 indexable files had ``st_nlink > 1``; all
          82,293 hardlinks on that machine were inside ``.venv`` and
          ``node_modules``, which the exclusion set removes long before the
          boundary sees them. The cost is therefore close to zero and the
          failure mode is fail-closed.
        """
        if not stat_module.S_ISREG(info.st_mode):
            return False
        if info.st_nlink != 1:
            self._reject()
            return False
        return True

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
        """What must not change while a file is being read."""
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    # -- the operations ---------------------------------------------------- #

    def stat(self, rel_path: str) -> os.stat_result | None:
        """Metadata for a contained regular file, or ``None``.

        ``None`` covers every reason a candidate is unusable — refused by the
        boundary, missing, a symlink, a directory, a device, a FIFO — because the
        caller's response to all of them is the same: do not index it.
        """
        parts = self._admit(rel_path)
        if parts is None:
            return None
        with self._descend(parts) as handle:
            if handle is None:
                self._reject()
                return None
            try:
                info = os.stat(parts[-1], dir_fd=handle, follow_symlinks=False)
            except OSError:
                return None
        return info if self._leaf_ok(info) else None

    @contextmanager
    def open(self, rel_path: str) -> Iterator[int | None]:
        """Yield a read-only descriptor for a contained regular file, or ``None``.

        The descriptor is the boundary's product. Handing back a path would
        reopen it and reintroduce the race this class exists to close.
        """
        parts = self._admit(rel_path)
        if parts is None:
            yield None
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
                if not self._leaf_ok(os.fstat(handle)):
                    yield None
                    return
                yield handle
            finally:
                os.close(handle)

    def read_content(self, rel_path: str, *, max_bytes: int) -> FileContent:
        """At most ``max_bytes`` bytes of a contained regular file.

        The only way to obtain repository content, and it cannot be asked without
        naming a limit. That is the point: an unbounded whole-file read of
        repository-controlled content is not a primitive this class offers, so no
        caller can reach for one by accident (V1_SPEC §17).

        ``max_bytes`` is an **inclusive** maximum. A file of exactly that many
        bytes is content; one byte more is :attr:`ContentStatus.OVERSIZED`.

        Two independent bounds, because either alone is defeatable:

        * the **pre-read fstat**, taken on the descriptor that will be read rather
          than on the path, decides oversize without reading anything at all. A
          five-gigabyte sparse file costs one ``fstat`` and zero content bytes;
        * the **hard ceiling** stops after ``max_bytes + 1`` bytes however large
          the file turns out to be. The fstat is a hint, not a guarantee — the
          file can grow between the two, ``st_size`` can be stale on some
          filesystems, and a repository can arrange both deliberately — so the
          read is bounded whether or not the size agreed.

        Reaching the ceiling is itself the oversize answer: the buffer is dropped
        and no caller sees the first ``max_bytes`` bytes, because a prefix of a
        file that kept going is not the file and must not become facts about it.

        The post-read identity check from the containment work is retained and
        runs last, on the same descriptor, so bytes spliced from two versions of a
        file are still refused rather than stored.
        """
        if max_bytes < 0:  # pragma: no cover - configuration is validated
            raise ValueError("max_bytes must not be negative")
        with self.open(rel_path) as handle:
            if handle is None:
                return FileContent(ContentStatus.REFUSED)
            try:
                before = os.fstat(handle)
                if before.st_size > max_bytes:
                    # Decided from metadata: nothing is read, so nothing is held.
                    return FileContent(ContentStatus.OVERSIZED, size=before.st_size)
                data = _read_bounded(handle, max_bytes + 1)
                if len(data) > max_bytes:
                    # st_size said otherwise. The file grew, or lied; either way
                    # the ceiling held and the partial buffer is discarded.
                    return FileContent(ContentStatus.OVERSIZED, size=len(data))
                if self._identity(os.fstat(handle)) != self._identity(before):
                    # The file changed underneath the read. What we hold is a
                    # splice of two versions that never existed on disk, and
                    # hashing it would store a content hash for bytes no file
                    # ever had. Refuse; the next sync sees the settled file.
                    self._reject()
                    return FileContent(ContentStatus.CHANGED)
            except OSError:
                return FileContent(ContentStatus.REFUSED)
            return FileContent(ContentStatus.OK, data=data)

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
                before = self._identity(os.fstat(handle))
                while block := os.read(handle, _READ_CHUNK_BYTES):
                    callback(block)
                if self._identity(os.fstat(handle)) != before:
                    self._reject()
                    return False
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
        parts = self._admit(rel_path)
        if parts is None:
            return None
        with self._descend(parts) as handle:
            if handle is None:
                self._reject()
                return None
            try:
                return os.readlink(parts[-1], dir_fd=handle)
            except OSError:
                return None


def _read_bounded(handle: int, limit: int) -> bytes:
    """At most ``limit`` bytes from ``handle``.

    Deliberately not ``os.read(handle, limit)`` in one call: that would ask the
    kernel for a single allocation the size of the limit even for a one-byte
    file. Each individual read is capped at the block size as well, so the peak
    is the bytes actually kept plus one block — never a buffer sized from an
    untrusted ``st_size``.
    """
    blocks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        block = os.read(handle, min(remaining, _READ_CHUNK_BYTES))
        if not block:
            break
        blocks.append(block)
        remaining -= len(block)
    return b"".join(blocks)
