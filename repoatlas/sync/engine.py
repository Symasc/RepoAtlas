"""Sync execution (ARCHITECTURE.md §6, §9).

Runs the pipeline for one repository — enumerate, diff, delete stale facts, parse,
persist, link, chunk, index, GC — and owns the **lifecycle of a whole run** in
:meth:`SyncEngine.run`.

The run boundary is where the synchronization invariant lives:

> A successful sync moves RepoAtlas from one complete logical knowledge state to
> another complete logical knowledge state.

File facts commit as they are produced, because holding a write transaction open
across a fifty-repository parse would block every other writer for the duration
and gain nothing. The unit is **one file, entirely**: its row, its entities, its
chunks, its edges, its standing claims and its dependency rows are written in a
single transaction by :meth:`SyncEngine._index_file`.

That is a correctness boundary, not a batching choice. The file row carries the
content hash the next run's planner compares, so committing it separately from the
facts derived from that content publishes a false claim — *this content is
indexed* — that nothing later contradicts, because the bytes really have not
changed. Every subsequent sync then skips the file, ``--full`` included, since
``--full`` forces a re-hash and the hash is unchanged. The commit is therefore the
completeness marker, and plain ``sync`` is the whole recovery path after an
interruption (ARCHITECTURE.md §9).

What must *not* commit early is the claim that those facts are current: the
cross-repository graph is rebuilt once, at the end, over all of them, so a
repository is marked synchronized only after that rebuild has committed — in the
same transaction (ARCHITECTURE.md §9).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repoatlas.config.settings import Settings
from repoatlas.containment import ContentStatus, FileContent, RepositoryBoundary
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.discovery.languages import detect_language, is_lockfile, is_manifest
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable
from repoatlas.gitstate.client import GitClient
from repoatlas.graph.keys import file_key, repository_key, symbol_key
from repoatlas.graph.linker import GLOBAL_PARSER, GlobalLinkResult, ReferenceLinker
from repoatlas.graph.py_requirements import REQUIREMENTS_FACTS, ROOT, reach, requirements_meta
from repoatlas.graph.uses_link import PENDING, UsesChangeLog, stored_scope
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.dependencies import DependencyRecord, DependencyReport
from repoatlas.models.parsing import (
    ENUMERATION_INCOMPLETE,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    Severity,
)
from repoatlas.models.reports import RepositorySyncReport, SyncReport
from repoatlas.models.repository import IndexedFile, Repository, repository_slug
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.parsers.requirements import REQUIREMENTS_FACTS_VERSION, requirements_facts
from repoatlas.storage.protocols import (
    ChunkRecord,
    EntityRecord,
    FileFacts,
    SyncCompletion,
    UnresolvedRefRecord,
)
from repoatlas.storage.sqlite.stores import (
    SqliteDependencyStore,
    SqliteFileStore,
    SqliteKnowledgeStore,
    SqliteRepositoryStore,
)
from repoatlas.storage.sqlite.vectors import SqliteVectorIndex
from repoatlas.sync.planner import decide_rehash, facts_version, plan_sync
from repoatlas.sync.scanner import (
    EnumerationUnavailable,
    ScannedFile,
    hash_bytes,
    hash_within,
    scan_repository,
)

#: Bytes above which a file is recorded but never read (V1_SPEC §17 zip-bomb guard).
#: How much of a file's head decides whether it is binary. Public because a
#: reader that has proven its bytes identical to the indexed bytes must reach
#: the same verdict the indexer reached, and a second constant would drift.
BINARY_SNIFF_BYTES = 8192


def _enumeration_diagnostics(warning_count: int) -> list[ParseDiagnostic]:
    """Say that enumeration was incomplete, without repeating what git said.

    ``git ls-files`` reports a directory it could not open as a *warning* and still
    exits 0, so the listing comes back short and successful-looking. Those warnings
    are the only evidence that "there is nothing under there" and "I could not
    look" are different claims here, and a sync that reported the second as the
    first would claim a completeness it never established (ARCHITECTURE.md §6).

    That fact is worth reporting. The sentences git wrote to say it are not.

    This takes a **count, not the warnings**, and the difference is the whole
    point: a projection that never receives the untrusted strings cannot publish
    them by a later edit. Earlier, this function was handed git's stderr lines and
    interpolated them, on the reasoning that git names a refused directory
    relative to the repository. That reasoning was wrong in the case that matters.
    Git names whatever it was pointed at, and ``.git/config`` --- repository
    state, not RepoAtlas's --- is part of what points it: a repo-local
    ``core.excludesFile`` naming an unreadable absolute path put that path,
    verbatim, on a public sync response, host directories and all. Anyone who can
    write to a tree could choose the contents of a RepoAtlas diagnostic.

    Nothing useful is lost with the text. The count decides only whether the
    repository has something to say; which directory could not be read is already
    reported by :attr:`SyncResponse.unreadable_paths`, which comes from
    RepoAtlas's own walk and is projected relative to the managed root.
    """
    if warning_count <= 0:
        return []
    return [ParseDiagnostic(severity=Severity.WARNING, message=ENUMERATION_INCOMPLETE)]


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """What one embedding pass did. Never a failure: semantic retrieval is an aid."""

    embedded: int = 0
    pending: int = 0
    orphans_removed: int = 0
    detail: str = ""

    @property
    def skipped(self) -> bool:
        return self.pending > 0 and self.embedded == 0


class SyncEngine:
    """Synchronizes repositories with the working tree."""

    def __init__(
        self,
        *,
        repositories: SqliteRepositoryStore,
        files: SqliteFileStore,
        knowledge: SqliteKnowledgeStore,
        dependencies: SqliteDependencyStore,
        registry: ParserRegistry,
        settings: Settings,
        vectors: SqliteVectorIndex | None = None,
        git: GitClient | None = None,
        dependency_registry: DependencyRegistry | None = None,
        embeddings: EmbeddingModel | None = None,
    ) -> None:
        self._repositories = repositories
        self._files = files
        self._knowledge = knowledge
        self._dependencies = dependencies
        self._vectors = vectors
        self._registry = registry
        self._dependency_registry = dependency_registry or DependencyRegistry()
        self._embeddings = embeddings
        self._settings = settings
        self._git = git or GitClient()
        self._linker = ReferenceLinker(knowledge, dependencies)
        #: What the run in progress changed that USES edges depend on; ``None`` outside one.
        self._uses: UsesChangeLog | None = None

    @property
    def facts_version(self) -> str:
        """See :func:`repoatlas.sync.planner.facts_version`."""
        return facts_version(self._registry, self._dependency_registry)

    def forget_if_missing(self, repo: Repository) -> bool:
        """Drop a repository whose directory no longer exists, and say whether it did.

        A repository's path is its natural key, so a repository that is no longer
        at its path is no longer that repository: a move is a removal plus a
        discovery, never a rename in place (ARCHITECTURE.md §3). Removing the row
        cascades away its files, entities, chunks and edges — including the
        repository graph node, which is otherwise exempt from orphan collection and
        would survive as a citable identity for a repository that is gone.

        Nothing extra is lost by doing this. A sync over a missing directory
        already scans nothing, so every file is classified deleted and every fact
        cascades away regardless; keeping the row only preserved a shell that still
        answered to a name.
        """
        if repo.id is None or repo.path.is_dir():
            return False
        if self._uses is not None:
            self._uses.repository_removing(repo.id)
        self._repositories.delete(repo.id)
        return True

    def ensure_repository_identity(self, repo: Repository) -> Repository:
        """Give the repository its canonical identity, assigning one if it lacks it.

        A database written before schema v5 has rows with no identity, and the
        identity is a function of the *configured root* — a value SQL cannot see,
        which is why the migration leaves the column empty rather than filling it
        with host-absolute paths. It is assigned here, before anything builds a key
        from it, and never rewritten afterwards (ARCHITECTURE.md §3).
        """
        if repo.slug:
            return repo
        if repo.id is None:
            raise ValueError("repository must be persisted before sync")
        slug = repository_slug(repo.path, self._settings.repos.root)
        self._repositories.assign_slug(repo.id, slug)
        return repo.model_copy(update={"slug": slug})

    def sync_repository(
        self, repo: Repository, *, full: bool = False
    ) -> tuple[RepositorySyncReport, SyncCompletion]:
        """Bring one repository's index in line with its current working tree.

        Returns the report *and* the completion the run will apply once the global
        graph has been rebuilt. The repository is left recorded as unfinished until
        then, deliberately: parsing having succeeded is not the sync having
        succeeded, and a run that dies here must be redone rather than trusted
        (ARCHITECTURE.md §9).
        """
        if repo.id is None:
            raise ValueError("repository must be persisted before sync")

        repo_id = repo.id
        # Identity is resolved before anything builds a key from it.
        repo = self.ensure_repository_identity(repo)
        repo_path = repo.path
        git_state = self._git.state(repo_path)

        rehash = decide_rehash(repo, git_state, force=full)

        managed = frozenset(
            r.path for r in self._repositories.list_all() if r.path.is_relative_to(repo_path)
        )
        # One boundary for the whole repository pass, so enumeration, hashing and
        # content reading all measure containment against the same canonical root
        # and the refusal count is aggregated once (ARCHITECTURE.md §12).
        boundary = RepositoryBoundary(repo_path)
        # Directories the fallback walk could not read. Only the filesystem
        # fallback can produce these — `git ls-files` answers from the index — and
        # they are the difference between "this repository has no files there" and
        # "RepoAtlas could not look", which a successful sync must not conflate
        # (ARCHITECTURE.md §6).
        incomplete: list[str] = []
        scanned = scan_repository(
            repo_path,
            git=self._git,
            settings=self._settings.discovery,
            managed_subrepos=managed,
            boundary=boundary,
            incomplete=incomplete,
        )
        known = self._files.list_states(repo_id)

        self._repositories.mark_sync_started(repo_id)

        plan = plan_sync(
            scanned,
            known,
            rehash=rehash,
            parser_version=self.facts_version,
            hash_of=lambda rel: hash_within(boundary, rel),
        )

        # Deletion first: a file's facts must never outlive the file.
        if self._uses is not None:
            self._uses.files_deleting(repo_id, plan.deleted_file_ids)
            if plan.new:
                self._uses.files_added(repo_id, [added.rel_path for added in plan.new])
        self._files.delete_many(plan.deleted_file_ids)

        diagnostics: list[ParseDiagnostic] = _enumeration_diagnostics(len(incomplete))
        symbols_total = 0
        edges_total = 0
        dependencies_total = 0
        indexed = 0
        languages: set[str] = set()
        manifests: set[str] = set()
        lockfiles: set[str] = set()

        # Detected metadata describes the whole working tree, not only the files
        # this run reindexed, so every scanned path contributes.
        for scanned_file in scanned:
            language = detect_language(scanned_file.rel_path)
            if language:
                languages.add(language)
            self._note_manifest(scanned_file.rel_path, manifests, lockfiles)

        for scanned_file in [*plan.new, *plan.changed]:
            outcome = self._index_file(repo, scanned_file.rel_path, scanned_file, boundary)
            # Stamped here, from the path this sync admitted, because this is the
            # only place that knows it: a parser is handed content and a name it
            # cannot vouch for, and a library that mentions a filename in its
            # error is naming whatever it was given --- on some paths an absolute
            # one. A diagnostic that says *which file* is worth far more than one
            # that quotes a parser, and this is where the trustworthy answer is.
            diagnostics.extend(
                diagnostic.model_copy(update={"path": scanned_file.rel_path})
                for diagnostic in outcome.diagnostics
            )
            symbols_total += outcome.symbols
            edges_total += outcome.edges
            dependencies_total += outcome.dependencies
            indexed += 1
            if outcome.language:
                languages.add(outcome.language)

        self._materialize_requirements(repo, boundary)
        self._knowledge.gc_orphan_entities()

        self._repositories.update_detected_metadata(
            repo_id,
            languages=sorted(languages),
            manifests=sorted(manifests),
            lockfiles=sorted(lockfiles),
        )

        completion = SyncCompletion(
            repo_id=repo_id,
            head_sha=git_state.head_sha,
            is_dirty=git_state.is_dirty,
            branch=git_state.branch,
        )

        report = RepositorySyncReport(
            repository=repo.name,
            branch=git_state.branch,
            is_dirty=git_state.is_dirty,
            unchanged=len(plan.unchanged),
            changed=len(plan.changed),
            new=len(plan.new),
            deleted=len(plan.deleted_file_ids),
            files_indexed=indexed,
            symbols=symbols_total,
            relationships=edges_total,
            dependencies=dependencies_total,
            full_rehash=plan.rehash.forced,
            rehash_reason=plan.rehash.reason,
            diagnostics=tuple(diagnostics),
        )
        return report, completion

    def _materialize_requirements(self, repo: Repository, boundary: RepositoryBoundary) -> None:
        """Record the requirements of every file the root ``requirements.txt`` reaches through
        ``-r``, and of no other (:mod:`repoatlas.graph.py_requirements`).

        A file is a requirements file only because another file includes it, so no parser can
        tell from the file alone: this runs once the repository's files are written, on every
        sync, and follows the includes through what the repository indexes. A reached file's
        requirements are read from its content and kept while that content is indexed --
        writing the file again drops them, and they are read again here -- and a file no longer
        reached loses them. What is recorded is a function of the working tree alone, exactly
        what a fresh index records.

        A file is read through the repository boundary, and only as the bytes the index holds
        for it: one changed since it was indexed, or that is not text, holds no requirements
        until a sync indexes it again.
        """
        assert repo.id is not None
        states = self._files.list_states(repo.id)
        stored = {
            path: meta
            for _, path, meta in self._knowledge.list_facts_of_kind(REQUIREMENTS_FACTS, repo.id)
        }
        loaded: dict[str, dict[str, Any] | None] = {}

        def load(path: str) -> dict[str, Any] | None:
            if path not in loaded:
                state = states[path]
                loaded[path] = self._requirements_of(
                    boundary, path, state.content_hash, stored.get(path)
                )
            return loaded[path]

        reached = reach(load, states.__contains__).files if ROOT in states else ()
        wanted = {
            found.path: requirements_meta(facts)
            for found in reached
            if (facts := loaded.get(found.path)) is not None
        }
        written = sorted(path for path, meta in wanted.items() if stored.get(path) != meta)
        dropped = sorted(set(stored) - set(wanted))
        if not written and not dropped:
            return
        claims: list[UnresolvedRefRecord] = []
        for path in written:
            file_id = states[path].file_id
            source = self._knowledge.find_file_entity(file_id, file_key(repo.slug, path))
            if source is None:
                continue
            claims.append(
                UnresolvedRefRecord(
                    repo_id=repo.id,
                    file_id=file_id,
                    src_id=source,
                    edge_type=EdgeType.CONFIGURES,
                    target_kind=EntityType.CONFIG,
                    target_key=REQUIREMENTS_FACTS,
                    line_start=1,
                    line_end=1,
                    meta_json=wanted[path],
                )
            )
        self._knowledge.replace_facts_of_kind(
            REQUIREMENTS_FACTS, [states[path].file_id for path in (*written, *dropped)], claims
        )

    def _requirements_of(
        self, boundary: RepositoryBoundary, path: str, content_hash: str, stored: str | None
    ) -> dict[str, Any] | None:
        """A reached file's requirements facts: the ones recorded while its content is
        unchanged, else read now, or ``None`` when its indexed bytes cannot be read as text."""
        if stored:
            facts = json.loads(json.loads(stored).get("facts") or "{}")
            if facts.get("v") == REQUIREMENTS_FACTS_VERSION:
                return facts
        raw = boundary.read_content(path, max_bytes=self._settings.indexing.max_file_bytes).data
        if raw is None or hash_bytes(raw) != content_hash or b"\0" in raw[:BINARY_SNIFF_BYTES]:
            return None
        try:
            return requirements_facts(path, raw.decode("utf-8"))
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _note_manifest(rel_path: str, manifests: set[str], lockfiles: set[str]) -> None:
        if is_manifest(rel_path):
            manifests.add(rel_path)
        if is_lockfile(rel_path):
            lockfiles.add(rel_path)

    def _index_file(
        self,
        repo: Repository,
        rel_path: str,
        scanned_file: ScannedFile,
        boundary: RepositoryBoundary,
    ) -> _FileOutcome:
        """Parse and persist one file. Never raises: failures become diagnostics.

        The read goes through the boundary rather than through ``repo.path /
        rel_path``. Enumeration already proved this candidate contained, and
        re-deriving the path here would re-ask a question that has an answer —
        badly, because a raw path can be redirected by an ancestor that changed
        since (ARCHITECTURE.md §12).
        """
        assert repo.id is not None
        language = detect_language(rel_path)
        cap = self._settings.indexing.max_file_bytes

        # The cap is applied by the reader, on the descriptor it reads from, so
        # a file larger than it is never allocated (ARCHITECTURE.md §12). What
        # comes back is at most `cap` bytes or a reason there are none.
        found = boundary.read_content(rel_path, max_bytes=cap)

        if found.status is ContentStatus.OVERSIZED:
            return self._index_oversized(repo, rel_path, scanned_file, boundary, found, language)

        raw = found.data
        if raw is None:
            # Unreadable, vanished, no longer inside the repository, or rewritten
            # mid-read. All of them mean the same thing to the indexer: there is
            # nothing to record, and the file's previous facts are replaced by none.
            return _FileOutcome(
                diagnostics=[
                    ParseDiagnostic(
                        severity=Severity.WARNING,
                        message=(
                            "changed while being read"
                            if found.status is ContentStatus.CHANGED
                            else "unreadable or not contained"
                        ),
                    )
                ]
            )

        content_hash = hash_bytes(raw)
        size = len(raw)
        mtime_ns = scanned_file.mtime_ns
        is_binary = b"\0" in raw[:BINARY_SNIFF_BYTES]

        parser = None if is_binary else self._registry.for_path(Path(rel_path))

        record = IndexedFile(
            rel_path=rel_path,
            size=size,
            mtime_ns=mtime_ns,
            content_hash=content_hash,
            language=language,
            parser=parser.name if parser else None,
            parser_version=self.facts_version,
            is_binary=is_binary,
            indexed_at=datetime.now(UTC),
        )

        if is_binary:
            # Recorded so deletion tracking stays correct, but never parsed.
            self._record_without_facts(repo, rel_path, record)
            return _FileOutcome(language=language)

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._record_without_facts(repo, rel_path, record)
            return _FileOutcome(
                language=language,
                diagnostics=[ParseDiagnostic(severity=Severity.WARNING, message="not valid UTF-8")],
            )

        # Everything below this line up to the transaction is a pure function of
        # `content`. Parsing, dependency extraction and chunking are the expensive
        # half and none of them touch the database, so they run *before* the write
        # lock is taken and the transaction stays as short as the writes it makes.
        parsed = (
            parser.parse(Path(rel_path), content)
            if parser
            else ParsedFile(language=language or "unknown", parser="none", parser_version="0")
        )
        dependencies, dependency_diagnostics = self._dependency_reports(rel_path, content)
        chunks = chunk_content(content, parsed.symbols, self._settings.indexing)

        # One transaction for every durable consequence of this file's content: the
        # row that claims the content is indexed, and every fact derived from it.
        # The commit *is* the completeness marker, which is what makes the file row
        # and its facts inseparable (ARCHITECTURE.md §9).
        with self._knowledge.transaction():
            file_id = self._files.upsert(repo.id, record)
            uses = self._uses.before(file_id) if self._uses is not None else None

            entities: list[EntityRecord] = [self._file_entity(repo, file_id, rel_path)]
            for symbol in parsed.symbols:
                entities.append(
                    EntityRecord(
                        type=symbol.kind,
                        key=symbol_key(
                            repo.slug, rel_path, symbol.qualified_name, symbol.line_start
                        ),
                        name=symbol.name,
                        qualified_name=symbol.qualified_name,
                        repo_id=repo.id,
                        file_id=file_id,
                        line_start=symbol.line_start,
                        line_end=symbol.line_end,
                        signature=symbol.signature,
                    )
                )

            chunk_records = tuple(
                ChunkRecord(
                    file_id=file_id,
                    repo_id=repo.id,
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    text=chunk.text,
                    token_estimate=chunk.token_estimate,
                    content_hash=chunk.content_hash,
                    span_index=chunk.span_index,
                    split_index=chunk.split_index,
                    split_total=chunk.split_total,
                )
                for chunk in chunks
            )

            self._knowledge.replace_file_facts(
                file_id, FileFacts(entities=tuple(entities), chunks=chunk_records)
            )

            link = self._linker.link_file(
                repo_id=repo.id,
                repo_slug=repo.slug,
                file_id=file_id,
                rel_path=rel_path,
                parsed=parsed,
            )

            dependency_count = self._write_dependencies(repo.id, file_id, dependencies)
            if self._uses is not None:
                self._uses.after(repo.id, file_id, uses)

        return _FileOutcome(
            language=parsed.language if parser else language,
            symbols=len(parsed.symbols),
            edges=link.edges,
            dependencies=dependency_count,
            diagnostics=[*parsed.diagnostics, *dependency_diagnostics],
        )

    def _record_without_facts(self, repo: Repository, rel_path: str, record: IndexedFile) -> None:
        """Record a file RepoAtlas will not derive facts from, atomically.

        Binary, undecodable and oversized files still need a row — deletion
        tracking depends on it — and still need whatever they previously asserted
        removed. Both happen in one transaction for the same reason the parsed path
        does: a row claiming the content is indexed must never outlive the removal
        of the facts it replaced.
        """
        assert repo.id is not None
        with self._knowledge.transaction():
            file_id = self._files.upsert(repo.id, record)
            uses = self._uses.before(file_id) if self._uses is not None else None
            self._knowledge.replace_file_facts(
                file_id, FileFacts(entities=(self._file_entity(repo, file_id, rel_path),))
            )
            if self._uses is not None:
                self._uses.after(repo.id, file_id, uses)

    def _index_oversized(
        self,
        repo: Repository,
        rel_path: str,
        scanned_file: ScannedFile,
        boundary: RepositoryBoundary,
        found: FileContent,
        language: str | None,
    ) -> _FileOutcome:
        """Record that an oversized file exists, without ever holding its content.

        RepoAtlas may legitimately know that a path exists and is too large —
        deletion tracking depends on the row, and an operator asking why a file
        is missing from the index deserves an answer. What it may not do is turn
        any of that file's bytes into a fact: no parser, no chunks, no
        embeddings, no evidence (V1_SPEC §17).

        The row still needs a **content** hash, because that hash is the sole
        authority on whether a file changed (ARCHITECTURE.md §9) and the planner
        compares this exact value against one it computes by streaming. Storing
        anything else here — a hash of the metadata, a constant — would make every
        forced rehash conclude the file had changed, forever. So the file is
        streamed once through :meth:`RepositoryBoundary.read_into`, in fixed
        blocks, with only the digest and an 8 KiB prefix retained: bounded memory
        for an unbounded file, which is the distinction this whole pass turns on.
        The prefix costs nothing extra — those bytes pass through anyway — and
        keeps ``is_binary`` as truthful for a huge file as for a small one.
        """
        assert repo.id is not None
        digest = hashlib.blake2b(digest_size=32)
        prefix = bytearray()

        def observe(block: bytes) -> None:
            digest.update(block)
            if len(prefix) < BINARY_SNIFF_BYTES:
                prefix.extend(block[: BINARY_SNIFF_BYTES - len(prefix)])

        if not boundary.read_into(rel_path, observe):
            # Vanished, refused, or rewritten mid-stream. Same answer as any other
            # unreadable file: record nothing, so its previous facts go with it.
            return _FileOutcome(
                diagnostics=[
                    ParseDiagnostic(
                        severity=Severity.WARNING, message="unreadable or not contained"
                    )
                ]
            )

        # `replace_file_facts` with only the file entity is what removes whatever
        # this path asserted when it was small enough to read: entities, edges,
        # unresolved references, chunks, FTS rows and the embeddings reachable
        # through them all cascade away (ARCHITECTURE.md §3). It commits together
        # with the row, so the removal and the claim that it happened are one fact.
        self._record_without_facts(
            repo,
            rel_path,
            IndexedFile(
                rel_path=rel_path,
                size=found.size if found.size is not None else scanned_file.size,
                mtime_ns=scanned_file.mtime_ns,
                content_hash=digest.hexdigest(),
                language=language,
                parser=None,
                parser_version=self.facts_version,
                is_binary=b"\0" in bytes(prefix),
                indexed_at=datetime.now(UTC),
            ),
        )
        return _FileOutcome(
            language=language,
            diagnostics=[
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message=(
                        f"skipped: {found.size} bytes exceeds the "
                        f"{self._settings.indexing.max_file_bytes}-byte content limit"
                    ),
                )
            ],
        )

    def _dependency_reports(
        self, rel_path: str, content: str
    ) -> tuple[list[tuple[str, DependencyReport]], list[ParseDiagnostic]]:
        """Parse one file for declared, resolved and provided packages.

        Runs for every indexed file, not only those the language registry claimed:
        a manifest such as ``package.json`` is parsed twice on purpose, once as
        configuration keys and once as dependencies, and the two answer different
        questions.

        Pure, and separate from the write for that reason: it is the expensive
        half, and the per-file transaction must not be held open across it
        (ARCHITECTURE.md §9).
        """
        path = Path(rel_path)
        reports: list[tuple[str, DependencyReport]] = []
        diagnostics: list[ParseDiagnostic] = []
        for parser in self._dependency_registry.for_path(path):
            report = parser.parse(path, content)
            diagnostics.extend(report.diagnostics)
            reports.append((parser.name, report))
        return reports, diagnostics

    def _write_dependencies(
        self, repo_id: int, file_id: int, reports: list[tuple[str, DependencyReport]]
    ) -> int:
        """Persist parsed dependency reports for one file. Caller owns the transaction."""
        if not reports:
            # Nothing to write, and nothing to clear either: `replace_file_facts`
            # ran earlier in this same transaction and already deleted this file's
            # dependency rows.
            return 0

        records: list[DependencyRecord] = []
        for parser_name, report in reports:
            for declared in report.declared:
                records.append(
                    DependencyRecord(
                        repo_id=repo_id,
                        file_id=file_id,
                        ecosystem=declared.ecosystem,
                        name=declared.name,
                        normalized_name=declared.normalized_name,
                        role="declared",
                        constraint=declared.constraint,
                        group=declared.group,
                        line_start=declared.line,
                        line_end=declared.line,
                        parser=parser_name,
                    )
                )
            for resolved in report.resolved:
                records.append(
                    DependencyRecord(
                        repo_id=repo_id,
                        file_id=file_id,
                        ecosystem=resolved.ecosystem,
                        name=resolved.name,
                        normalized_name=resolved.normalized_name,
                        role="resolved",
                        version=resolved.version,
                        line_start=resolved.line,
                        line_end=resolved.line,
                        parser=parser_name,
                    )
                )
            for provided in report.provides:
                records.append(
                    DependencyRecord(
                        repo_id=repo_id,
                        file_id=file_id,
                        ecosystem=provided.ecosystem,
                        name=provided.name,
                        normalized_name=provided.normalized_name,
                        role="provided",
                        version=provided.version,
                        line_start=provided.line,
                        line_end=provided.line,
                        parser=parser_name,
                    )
                )

        self._dependencies.replace_for_file(file_id, records)
        return len(records)

    def _enumeration_failed(self, repo: Repository) -> RepositorySyncReport:
        """Record that a repository's file set could not be established.

        Nothing of this repository is read, parsed or written — the refusal happens
        in :func:`~repoatlas.sync.scanner.scan_repository` before the first
        candidate is admitted, so there is no partial scan to undo (V1_SPEC §12).
        Facts from a *previous* successful sync are left exactly as they were:
        deleting them would destroy a known-good index because git is temporarily
        unreachable, and rewriting them is impossible without the file set. They
        stop being reported as current, which is the honest position.

        The diagnostic names the repository and what could not be done, and nothing
        else. Git configuration, environment and command output are deliberately
        not echoed (ARCHITECTURE.md §12).
        """
        assert repo.id is not None
        self._repositories.mark_sync_failed(repo.id)
        return RepositorySyncReport(
            repository=repo.name,
            enumeration_failed=True,
            diagnostics=(
                ParseDiagnostic(
                    severity=Severity.ERROR,
                    message=(
                        "git could not enumerate this repository, so RepoAtlas does "
                        "not know which files belong to it. Nothing was indexed: a "
                        "filesystem scan would admit files git excludes, and "
                        "indexing a different set than the one reported is the one "
                        "thing this must not do. Check that `git` is installed and "
                        "on PATH and that the repository's git metadata is readable."
                    ),
                ),
            ),
        )

    def run(
        self,
        repositories: Sequence[Repository],
        *,
        full: bool = False,
        scope: str = "global",
        embed_repo_ids: Sequence[int] | None = None,
    ) -> SyncReport:
        """One synchronization run, from working tree to complete knowledge state.

        This method owns the run's transaction boundary, and the ordering is the
        whole point:

        1. every in-scope repository is parsed and its file facts committed, with
           its status left ``interrupted`` and its completion held back;
        2. the replacement global graph is computed, touching nothing;
        3. **one transaction** swaps the graph in and marks every repository of
           this run synchronized — so the graph and the currency claim it justifies
           become visible together;
        4. embedding runs last, over what changed.

        A failure before step 3 leaves the previous complete graph and every
        in-scope repository recorded as unfinished, which is what makes the next
        run redo the work instead of trusting it (ARCHITECTURE.md §9).

        The caller must already hold the writer lock; see
        :func:`repoatlas.sync.lock.writer_lock`.
        """
        # USES edges are kept between syncs (repoatlas.graph.uses_link). Which of them
        # this run must replace is gathered as files are written, unless the state they
        # were left in is not known complete; and until this run's link commit they are
        # recorded as not known complete, so a run that dies before it leaves the next
        # one resolving every use.
        stored = stored_scope(self._knowledge)
        self._uses = UsesChangeLog(
            self._knowledge,
            self._dependencies,
            producer=GLOBAL_PARSER,
            everything=stored.reason if stored is not None else None,
        )
        self._knowledge.set_uses_links_state(PENDING)
        try:
            return self._run(repositories, full=full, scope=scope, embed_repo_ids=embed_repo_ids)
        finally:
            self._uses = None

    def _run(
        self,
        repositories: Sequence[Repository],
        *,
        full: bool,
        scope: str,
        embed_repo_ids: Sequence[int] | None,
    ) -> SyncReport:
        reports: list[RepositorySyncReport] = []
        completions: list[SyncCompletion] = []

        for repo in repositories:
            # Scope decides which repositories a run may forget, exactly as it
            # decides which it may index: a repository-scoped run never touches
            # another row.
            if self.forget_if_missing(repo):
                continue
            self.ensure_repository_entity(repo)
            try:
                report, completion = self.sync_repository(repo, full=full)
            except EnumerationUnavailable:
                # One repository RepoAtlas cannot establish the file set for does
                # not invalidate the others: healthy repositories in the same run
                # still sync, keep their facts and are marked current, and this one
                # is recorded `failed` with a diagnostic saying so. Withholding its
                # completion is what keeps `status` from calling it current, and
                # the same `failed` status forces a full rehash if it ever becomes
                # syncable again (V1_SPEC §12).
                reports.append(self._enumeration_failed(repo))
                continue
            reports.append(report)
            completions.append(completion)

        # Computed outside the transaction: this is the expensive half, and none
        # of it touches the graph it is going to replace.
        plan = self._linker.prepare_global(self._uses)

        with self._knowledge.transaction():
            link = self._linker.commit_global(plan)
            self._repositories.mark_many_synced(completions)

        # Embedding runs last and only over what changed. It is the one stage
        # allowed to be unavailable without failing the sync (V1_SPEC §14), so it
        # sits outside the transaction that decides whether the run succeeded.
        embedding = self.embed_pending(embed_repo_ids)

        return SyncReport(
            repositories=tuple(reports),
            scope=scope,
            linked_references=link.resolved,
            unresolved_references=link.unresolved,
            cross_repository_edges=link.cross_repository,
            embedded_chunks=embedding.embedded,
            pending_embeddings=embedding.pending,
            embedding_detail=embedding.detail,
            link_trace=link.trace,
        )

    def link_global(self) -> GlobalLinkResult:
        """Resolve every standing reference against everything now indexed.

        Runs once at the end of a sync run, including a single-repository sync,
        because a change in one repository can create or invalidate a
        cross-repository edge in another (ARCHITECTURE.md §6). Prepare and swap
        are separate phases; see :class:`~repoatlas.graph.linker.ReferenceLinker`.
        """
        return self._linker.link_global()

    def embed_pending(self, repo_ids: Sequence[int] | None = None) -> EmbeddingResult:
        """Embed chunk text that has no vector for the configured model.

        This is the whole of "never re-embed unchanged data" (V1_SPEC §20).
        Vectors are keyed by chunk content hash, so a chunk whose text survived a
        reparse byte-for-byte already has one and is never seen here — even though
        its row was deleted and reinserted with a new id.

        Availability is checked *before* any work is queued, so a missing model
        produces one clear message rather than failing part-way through.
        """
        if self._vectors is None or self._embeddings is None:
            return EmbeddingResult(detail="embeddings not configured")

        pending = self._vectors.missing_hashes(self._embeddings.id, repo_ids)
        orphans = self._vectors.gc_orphan_vectors()
        if not pending:
            return EmbeddingResult(orphans_removed=orphans, detail="up to date")

        if not self._embeddings.available():
            # Not fatal: exact, symbol and graph retrieval are unaffected, and the
            # next sync retries. Semantic retrieval simply stays off.
            return EmbeddingResult(
                pending=len(pending),
                orphans_removed=orphans,
                detail=self._embeddings.availability_detail(),
            )

        embedded = 0
        batch = max(1, self._settings.embeddings.batch_size)
        for start in range(0, len(pending), batch):
            window = pending[start : start + batch]
            pairs = self._vectors.texts_for_hashes(window)
            if not pairs:
                continue
            try:
                vectors = self._embeddings.embed([text for _, text in pairs])
            except EmbeddingUnavailable as exc:
                return EmbeddingResult(
                    embedded=embedded,
                    pending=len(pending) - embedded,
                    orphans_removed=orphans,
                    detail=str(exc),
                )
            embedded += self._vectors.upsert_many(
                self._embeddings.id,
                self._embeddings.dim,
                [
                    (content_hash, vector)
                    for (content_hash, _), vector in zip(pairs, vectors, strict=True)
                ],
            )

        return EmbeddingResult(
            embedded=embedded,
            orphans_removed=orphans,
            detail=f"model {self._embeddings.id}",
        )

    @staticmethod
    def _file_entity(repo: Repository, file_id: int, rel_path: str) -> EntityRecord:
        return EntityRecord(
            type=EntityType.FILE,
            key=file_key(repo.slug, rel_path),
            name=Path(rel_path).name,
            qualified_name=rel_path,
            repo_id=repo.id,
            file_id=file_id,
        )

    def ensure_repository_entity(self, repo: Repository) -> int:
        """Intern the repository node itself, so the graph has a root per repo.

        Keyed by identity rather than by name. Interned entities are unique by
        key, so ``repo:common`` made two managed repositories called ``common``
        into a single graph node — and a dependency edge to one of them was an
        edge to whichever had been indexed first (V1_SPEC §9).
        """
        identified = self.ensure_repository_identity(repo)
        return self._knowledge.intern_entity(
            EntityRecord(
                type=EntityType.REPOSITORY,
                key=repository_key(identified.slug),
                name=identified.name,
                qualified_name=identified.slug,
                repo_id=identified.id,
            )
        )


class _FileOutcome:
    __slots__ = ("dependencies", "diagnostics", "edges", "language", "symbols")

    def __init__(
        self,
        *,
        language: str | None = None,
        symbols: int = 0,
        edges: int = 0,
        dependencies: int = 0,
        diagnostics: list[ParseDiagnostic] | None = None,
    ) -> None:
        self.language = language
        self.symbols = symbols
        self.edges = edges
        self.dependencies = dependencies
        self.diagnostics = diagnostics or []
