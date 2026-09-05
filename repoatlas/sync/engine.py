"""Sync execution (ARCHITECTURE.md §6, §9).

Runs the pipeline for one repository — enumerate, diff, delete stale facts, parse,
persist, link, chunk, index, GC — and owns the **lifecycle of a whole run** in
:meth:`SyncEngine.run`.

The run boundary is where the synchronization invariant lives:

> A successful sync moves RepoAtlas from one complete logical knowledge state to
> another complete logical knowledge state.

Per-repository file facts commit as they are produced, because holding a write
transaction open across a fifty-repository parse would block every other writer
for the duration and gain nothing. What must *not* commit early is the claim that
those facts are current: the cross-repository graph is rebuilt once, at the end,
over all of them, so a repository is marked synchronized only after that rebuild
has committed — in the same transaction (ARCHITECTURE.md §9).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.containment import RepositoryBoundary
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.discovery.languages import detect_language, is_lockfile, is_manifest
from repoatlas.embeddings.protocol import EmbeddingModel, EmbeddingUnavailable
from repoatlas.gitstate.client import GitClient
from repoatlas.graph.keys import file_key, repository_key, symbol_key
from repoatlas.graph.linker import GlobalLinkResult, ReferenceLinker
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import EntityType, ParsedFile, ParseDiagnostic, Severity
from repoatlas.models.reports import RepositorySyncReport, SyncReport
from repoatlas.models.repository import IndexedFile, Repository, repository_slug
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.storage.protocols import ChunkRecord, EntityRecord, FileFacts, SyncCompletion
from repoatlas.storage.sqlite.stores import (
    SqliteDependencyStore,
    SqliteFileStore,
    SqliteKnowledgeStore,
    SqliteRepositoryStore,
)
from repoatlas.storage.sqlite.vectors import SqliteVectorIndex
from repoatlas.sync.planner import decide_rehash, facts_version, plan_sync
from repoatlas.sync.scanner import ScannedFile, hash_bytes, hash_within, scan_repository

#: Bytes above which a file is recorded but never read (V1_SPEC §17 zip-bomb guard).
_BINARY_SNIFF_BYTES = 8192


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
        scanned = scan_repository(
            repo_path,
            git=self._git,
            settings=self._settings.discovery,
            managed_subrepos=managed,
            boundary=boundary,
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
        self._files.delete_many(plan.deleted_file_ids)

        diagnostics: list[ParseDiagnostic] = []
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
            diagnostics.extend(outcome.diagnostics)
            symbols_total += outcome.symbols
            edges_total += outcome.edges
            dependencies_total += outcome.dependencies
            indexed += 1
            if outcome.language:
                languages.add(outcome.language)

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

        raw = boundary.read_bytes(rel_path)
        if raw is None:
            # Unreadable, vanished, or no longer inside the repository. All three
            # mean the same thing to the indexer: there is nothing to record, and
            # the file's previous facts are replaced by none.
            return _FileOutcome(
                diagnostics=[
                    ParseDiagnostic(
                        severity=Severity.WARNING,
                        message="unreadable or not contained",
                    )
                ]
            )

        content_hash = hash_bytes(raw)
        size = len(raw)
        mtime_ns = scanned_file.mtime_ns
        is_binary = b"\0" in raw[:_BINARY_SNIFF_BYTES]
        oversized = size > self._settings.indexing.max_file_bytes

        parser = None if (is_binary or oversized) else self._registry.for_path(Path(rel_path))

        file_id = self._files.upsert(
            repo.id,
            IndexedFile(
                rel_path=rel_path,
                size=size,
                mtime_ns=mtime_ns,
                content_hash=content_hash,
                language=language,
                parser=parser.name if parser else None,
                parser_version=self.facts_version,
                is_binary=is_binary,
                indexed_at=datetime.now(UTC),
            ),
        )

        if is_binary or oversized:
            # Recorded so deletion tracking stays correct, but never read further.
            self._knowledge.replace_file_facts(
                file_id,
                FileFacts(entities=(self._file_entity(repo, file_id, rel_path),)),
            )
            return _FileOutcome(language=language)

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._knowledge.replace_file_facts(
                file_id,
                FileFacts(entities=(self._file_entity(repo, file_id, rel_path),)),
            )
            return _FileOutcome(
                language=language,
                diagnostics=[ParseDiagnostic(severity=Severity.WARNING, message="not valid UTF-8")],
            )

        parsed = (
            parser.parse(Path(rel_path), content)
            if parser
            else ParsedFile(language=language or "unknown", parser="none", parser_version="0")
        )

        entities: list[EntityRecord] = [self._file_entity(repo, file_id, rel_path)]
        for symbol in parsed.symbols:
            entities.append(
                EntityRecord(
                    type=symbol.kind,
                    key=symbol_key(repo.slug, rel_path, symbol.qualified_name, symbol.line_start),
                    name=symbol.name,
                    qualified_name=symbol.qualified_name,
                    repo_id=repo.id,
                    file_id=file_id,
                    line_start=symbol.line_start,
                    line_end=symbol.line_end,
                    signature=symbol.signature,
                )
            )

        chunks = chunk_content(content, parsed.symbols, self._settings.indexing)
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

        dependency_outcome = self._index_dependencies(repo.id, file_id, rel_path, content)

        return _FileOutcome(
            language=parsed.language if parser else language,
            symbols=len(parsed.symbols),
            edges=link.edges,
            dependencies=dependency_outcome[0],
            diagnostics=[*parsed.diagnostics, *dependency_outcome[1]],
        )

    def _index_dependencies(
        self, repo_id: int, file_id: int, rel_path: str, content: str
    ) -> tuple[int, list[ParseDiagnostic]]:
        """Extract declared, resolved and provided packages from one file.

        Runs for every indexed file, not only those the language registry claimed:
        a manifest such as ``package.json`` is parsed twice on purpose, once as
        configuration keys and once as dependencies, and the two answer different
        questions.
        """
        path = Path(rel_path)
        parsers = self._dependency_registry.for_path(path)
        if not parsers:
            return 0, []

        records: list[DependencyRecord] = []
        diagnostics: list[ParseDiagnostic] = []

        for parser in parsers:
            report = parser.parse(path, content)
            diagnostics.extend(report.diagnostics)
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
                        parser=parser.name,
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
                        parser=parser.name,
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
                        parser=parser.name,
                    )
                )

        self._dependencies.replace_for_file(file_id, records)
        return len(records), diagnostics

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
        reports: list[RepositorySyncReport] = []
        completions: list[SyncCompletion] = []

        for repo in repositories:
            # Scope decides which repositories a run may forget, exactly as it
            # decides which it may index: a repository-scoped run never touches
            # another row.
            if self.forget_if_missing(repo):
                continue
            self.ensure_repository_entity(repo)
            report, completion = self.sync_repository(repo, full=full)
            reports.append(report)
            completions.append(completion)

        # Computed outside the transaction: this is the expensive half, and none
        # of it touches the graph it is going to replace.
        plan = self._linker.prepare_global()

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
