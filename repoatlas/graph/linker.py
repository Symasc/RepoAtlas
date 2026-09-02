"""Reference resolution (ARCHITECTURE.md §5, §6).

Parsers emit unresolved references; this stage turns them into graph edges. All
target-matching rules live here, so the "exact match only, never fuzzy" rule of
V1_SPEC §9 is enforced in one auditable place rather than in every parser.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.graph.keys import file_key, global_key, symbol_key
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedSymbol
from repoatlas.storage.protocols import EdgeRecord, EntityRecord
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

#: Reference kinds interned as global entities. Anything else needs a resolvable
#: local target and is otherwise left unresolved rather than invented.
_INTERNED_KINDS = frozenset({EntityType.ENV_VAR, EntityType.MODULE, EntityType.COMMAND})


@dataclass(frozen=True, slots=True)
class LinkResult:
    edges: int
    unresolved: int


class ReferenceLinker:
    """Resolves one file's references into edges."""

    def __init__(self, knowledge: SqliteKnowledgeStore) -> None:
        self._knowledge = knowledge

    def link_file(
        self,
        *,
        repo_id: int,
        repo_name: str,
        file_id: int,
        rel_path: str,
        parsed: ParsedFile,
    ) -> LinkResult:
        """Create every edge this file asserts."""
        file_entity_id = self._knowledge.find_file_entity(file_id, file_key(repo_name, rel_path))
        if file_entity_id is None:
            return LinkResult(edges=0, unresolved=0)

        symbol_ids = self._symbol_ids(file_id, repo_name, rel_path, parsed.symbols)

        edges: list[EdgeRecord] = [
            EdgeRecord(
                src_id=file_entity_id,
                dst_id=entity_id,
                type=EdgeType.DEFINES,
                repo_id=repo_id,
                file_id=file_id,
                parser=parsed.parser,
            )
            for entity_id in symbol_ids.values()
        ]

        unresolved = 0
        for reference in parsed.references:
            source_id = symbol_ids.get(reference.source_symbol or "", file_entity_id)

            if reference.target_kind in _INTERNED_KINDS:
                target_id = self._knowledge.intern_entity(
                    EntityRecord(
                        type=reference.target_kind,
                        key=global_key(reference.target_kind, reference.target_key),
                        name=reference.target_key,
                        qualified_name=reference.target_key,
                    )
                )
            else:
                # A symbol reference resolves only against symbols defined in this
                # file. Cross-file resolution arrives with the import graph in
                # Slice C; until then it stays unresolved rather than guessed.
                target_id = symbol_ids.get(reference.target_key, 0)
                if not target_id:
                    unresolved += 1
                    continue

            edges.append(
                EdgeRecord(
                    src_id=source_id,
                    dst_id=target_id,
                    type=reference.edge_type,
                    confidence=reference.confidence,
                    repo_id=repo_id,
                    file_id=file_id,
                    line_start=reference.line_start,
                    line_end=reference.line_end,
                    parser=parsed.parser,
                )
            )

        self._knowledge.add_edges(edges)
        return LinkResult(edges=len(edges), unresolved=unresolved)

    def _symbol_ids(
        self,
        file_id: int,
        repo_name: str,
        rel_path: str,
        symbols: tuple[ParsedSymbol, ...],
    ) -> dict[str, int]:
        """Map qualified symbol name to its persisted entity id."""
        mapping: dict[str, int] = {}
        for symbol in symbols:
            key = symbol_key(repo_name, rel_path, symbol.qualified_name, symbol.line_start)
            entity_id = self._knowledge.find_file_entity(file_id, key)
            if entity_id is not None:
                mapping[symbol.qualified_name] = entity_id
        return mapping
