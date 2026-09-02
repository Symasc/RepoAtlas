"""Sync execution (ARCHITECTURE.md §6, §9).

Runs the pipeline for one repository: enumerate, diff, delete stale facts, parse,
persist, link, chunk, index, GC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.discovery.languages import detect_language, is_lockfile, is_manifest
from repoatlas.gitstate.client import GitClient
from repoatlas.graph.keys import file_key, repository_key, symbol_key
from repoatlas.graph.linker import GlobalLinkResult, ReferenceLinker
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import EntityType, ParsedFile, ParseDiagnostic, Severity
from repoatlas.models.reports import RepositorySyncReport
from repoatlas.models.repository import IndexedFile, Repository
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.storage.protocols import ChunkRecord, EntityRecord, FileFacts
from repoatlas.storage.sqlite.stores import (
    SqliteDependencyStore,
    SqliteFileStore,
    SqliteKnowledgeStore,
    SqliteRepositoryStore,
)
from repoatlas.sync.planner import decide_rehash, plan_sync
from repoatlas.sync.scanner import ScannedFile, hash_bytes, hash_file, scan_repository

#: Bytes above which a file is recorded but never read (V1_SPEC §17 zip-bomb guard).
_BINARY_SNIFF_BYTES = 8192


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
        git: GitClient | None = None,
        dependency_registry: DependencyRegistry | None = None,
    ) -> None:
        self._repositories = repositories
        self._files = files
        self._knowledge = knowledge
        self._dependencies = dependencies
        self._registry = registry
        self._dependency_registry = dependency_registry or DependencyRegistry()
        self._settings = settings
        self._git = git or GitClient()
        self._linker = ReferenceLinker(knowledge, dependencies)

    @property
    def facts_version(self) -> str:
        """Version of everything that turns a file into facts.

        Both registries contribute, because a change to either makes stored facts
        stale even though the file on disk is untouched (V1_SPEC §12). It is stored
        per file, so only files with a handler are reparsed.
        """
        return f"{self._registry.version}||{self._dependency_registry.version}"

    def sync_repository(self, repo: Repository, *, full: bool = False) -> RepositorySyncReport:
        """Bring one repository's index in line with its current working tree."""
        if repo.id is None:
            raise ValueError("repository must be persisted before sync")

        repo_path = repo.path
        git_state = self._git.state(repo_path)

        rehash = decide_rehash(repo, git_state, force=full)

        managed = frozenset(
            r.path for r in self._repositories.list_all() if r.path.is_relative_to(repo_path)
        )
        scanned = scan_repository(
            repo_path,
            git=self._git,
            settings=self._settings.discovery,
            managed_subrepos=managed,
        )
        known = self._files.list_states(repo.id)

        self._repositories.mark_sync_started(repo.id)

        plan = plan_sync(
            scanned,
            known,
            rehash=rehash,
            parser_version=self.facts_version,
            hash_of=lambda rel: hash_file(repo_path / rel),
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
            outcome = self._index_file(repo, scanned_file.rel_path, scanned_file)
            diagnostics.extend(outcome.diagnostics)
            symbols_total += outcome.symbols
            edges_total += outcome.edges
            dependencies_total += outcome.dependencies
            indexed += 1
            if outcome.language:
                languages.add(outcome.language)

        self._knowledge.gc_orphan_entities()

        self._repositories.update_detected_metadata(
            repo.id,
            languages=sorted(languages),
            manifests=sorted(manifests),
            lockfiles=sorted(lockfiles),
        )
        self._repositories.mark_sync_finished(
            repo.id,
            head_sha=git_state.head_sha,
            is_dirty=git_state.is_dirty,
            branch=git_state.branch,
        )

        return RepositorySyncReport(
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

    @staticmethod
    def _note_manifest(rel_path: str, manifests: set[str], lockfiles: set[str]) -> None:
        if is_manifest(rel_path):
            manifests.add(rel_path)
        if is_lockfile(rel_path):
            lockfiles.add(rel_path)

    def _index_file(
        self, repo: Repository, rel_path: str, scanned_file: ScannedFile
    ) -> _FileOutcome:
        """Parse and persist one file. Never raises: failures become diagnostics."""
        assert repo.id is not None
        absolute = repo.path / rel_path
        language = detect_language(rel_path)

        try:
            raw = absolute.read_bytes()
        except OSError as exc:
            return _FileOutcome(
                diagnostics=[
                    ParseDiagnostic(severity=Severity.WARNING, message=f"unreadable: {exc}")
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
                    key=symbol_key(repo.name, rel_path, symbol.qualified_name, symbol.line_start),
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
            )
            for chunk in chunks
        )

        self._knowledge.replace_file_facts(
            file_id, FileFacts(entities=tuple(entities), chunks=chunk_records)
        )

        link = self._linker.link_file(
            repo_id=repo.id,
            repo_name=repo.name,
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

    def link_global(self) -> GlobalLinkResult:
        """Resolve every standing reference against everything now indexed.

        Runs once at the end of a sync run, including a single-repository sync,
        because a change in one repository can create or invalidate a
        cross-repository edge in another (ARCHITECTURE.md §6).
        """
        result = self._linker.link_global()
        self._knowledge.gc_orphan_entities()
        return result

    @staticmethod
    def _file_entity(repo: Repository, file_id: int, rel_path: str) -> EntityRecord:
        return EntityRecord(
            type=EntityType.FILE,
            key=file_key(repo.name, rel_path),
            name=Path(rel_path).name,
            qualified_name=rel_path,
            repo_id=repo.id,
            file_id=file_id,
        )

    def ensure_repository_entity(self, repo: Repository) -> int:
        """Intern the repository node itself, so the graph has a root per repo."""
        return self._knowledge.intern_entity(
            EntityRecord(
                type=EntityType.REPOSITORY,
                key=repository_key(repo.name),
                name=repo.name,
                qualified_name=str(repo.path),
                repo_id=repo.id,
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
