"""The ``impact`` capability: seed resolution (and, built on it, the change-impact walk).

A seed names what changes. It has to name exactly one thing, because everything
after it is a statement about that one thing: a name two declarations share, a path
two repositories hold, is refused with the candidates listed, and nothing is followed.
The spellings accepted are the ones the other tools already hand back or a person
would write, tried in a fixed order so the same seed always means the same thing:

1. an exact entity key;
2. a repository-relative file path;
3. a declaration's name or qualified name, a configuration key's qualified name, or
   the ``env:`` / ``module:`` key a bare name spells. These rank alike: a bare
   ``DATABASE_URL`` that is both an environment variable and a constant's name is two
   different things, and the seed is refused with both listed rather than resolved
   to whichever spelling is tried first;
4. a repository-relative directory: every file beneath it changes together.

Scope is the context's, exactly as ``relationships`` applies it
(:func:`repoatlas.capabilities.service._resolvable_in`): an interned entity -- an
environment variable, a package, an HTTP operation -- belongs to no repository and is
in every scope; anything else must belong to a repository the call named.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from repoatlas.graph.keys import file_key
from repoatlas.graph.units import package_directories, unit_of
from repoatlas.models.impact import (
    MAX_SEED_CANDIDATES,
    Granularity,
    ImpactSeed,
    SeedBasis,
    SeedCandidate,
    SeedStatus,
)
from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import EntityIdentity, KnowledgeStore, SearchIndex

#: The traversal policy's name and version, reported with every answer.
POLICY = "impact/1"

#: Entity types a change is a *key* change for: they name a value, not code.
_KEYS = frozenset(
    {
        EntityType.CONFIG,
        EntityType.ENV_VAR,
        EntityType.PACKAGE,
        EntityType.COMMAND,
        EntityType.HTTP_ROUTE,
        EntityType.HTTP_DEPLOYMENT,
        EntityType.HTTP_ENDPOINT,
    }
)

#: How many exact name matches are read to count an ambiguous seed's candidates.
_LOOKUP_LIMIT = 10_000

NOT_INDEXED = "nothing indexed is keyed, located or named by it"
OUT_OF_SCOPE = "it names something only outside the repositories this call is scoped to"


@dataclass(frozen=True, slots=True)
class Seed:
    """A seed resolved to one thing: an entity, or a directory of one repository."""

    model: ImpactSeed
    identity: EntityIdentity | None = None
    #: For a directory: its repository and path.
    directory: tuple[str, str] | None = None


def in_scope(identity: EntityIdentity, slugs: frozenset[str]) -> bool:
    return identity.repository_slug is None or identity.repository_slug in slugs


def _lines(identity: EntityIdentity) -> tuple[int, int] | None:
    if identity.line_start is None:
        return None
    return (identity.line_start, identity.line_end or identity.line_start)


def _candidate_order(identity: EntityIdentity) -> tuple[str, str, int, str]:
    return (
        identity.repository_slug or "",
        identity.path or "",
        identity.line_start or 0,
        identity.key,
    )


class SeedResolver:
    """Resolves one seed against the stores, within one context's repositories."""

    def __init__(
        self,
        knowledge: KnowledgeStore,
        search: SearchIndex,
        repositories: Mapping[str, int],
    ) -> None:
        self._knowledge = knowledge
        self._search = search
        #: Repository slug -> repository id, for the repositories in scope.
        self._repositories = dict(repositories)
        self._slugs = frozenset(repositories)
        self._directories: dict[str, tuple[str, ...]] | None = None

    def resolve(self, term: str) -> Seed:
        keyed = self._by_key([term])
        if keyed:
            return self._one(term, keyed, lambda _: SeedBasis.KEY)
        exact = self._by_name(term)
        paths = [i for i in exact if i.type is EntityType.FILE and i.qualified_name == term]
        if paths:
            return self._one(term, paths, lambda _: SeedBasis.PATH)
        spelled = (
            {}
            if ":" in term
            else {i.entity_id: i for i in self._by_key([f"env:{term}", f"module:{term}"])}
        )
        named = [
            i
            for i in exact
            if i.type is not EntityType.FILE
            and (i.entity_id in spelled or term in (i.name, i.qualified_name))
        ]
        both = _unique([*spelled.values(), *named])
        if both:
            return self._one(
                term,
                both,
                lambda i: SeedBasis.KEY if i.entity_id in spelled else SeedBasis.NAME,
            )
        directory = self._by_directory(term)
        if directory is not None:
            return directory
        return Seed(
            ImpactSeed(
                status=SeedStatus.UNRESOLVED,
                term=term,
                reason=OUT_OF_SCOPE if self._anywhere(term) else NOT_INDEXED,
            )
        )

    # -- lookups ---------------------------------------------------------------

    def _by_key(self, keys: Sequence[str]) -> list[EntityIdentity]:
        found = self._knowledge.get_entities_by_key(keys)
        return _unique([i for i in found if in_scope(i, self._slugs)])

    def _by_name(self, term: str) -> list[EntityIdentity]:
        hits = self._search.find_entities_by_name(
            [term], sorted(self._repositories.values()), _LOOKUP_LIMIT
        )
        identities = self._knowledge.keys_for_entities(sorted({hit.entity_id for hit in hits}))
        return _unique([i for i in identities.values() if in_scope(i, self._slugs)])

    def _by_directory(self, term: str) -> Seed | None:
        path = term.strip("/")
        if not path or path == ".":
            return None
        found = [
            (slug, count)
            for slug in sorted(self._slugs)
            if (count := self._knowledge.count_keys_with_prefix(file_key(slug, path) + "/")) > 0
        ]
        if len(found) > 1:
            return Seed(
                ImpactSeed(
                    status=SeedStatus.AMBIGUOUS,
                    term=term,
                    candidates=tuple(
                        SeedCandidate(entity=f"{slug}:{path}/", repository=slug, path=path)
                        for slug, _ in found[:MAX_SEED_CANDIDATES]
                    ),
                    total=len(found),
                )
            )
        if not found:
            return None
        slug, count = found[0]
        return Seed(
            ImpactSeed(
                status=SeedStatus.RESOLVED,
                term=term,
                entity=f"{slug}:{path}/",
                repository=slug,
                path=path,
                unit=self._unit(f"{slug}:{path}/", slug, path + "/"),
                basis=SeedBasis.DIRECTORY,
                granularity=Granularity.SUBTREE,
                members=count,
            ),
            directory=(slug, path),
        )

    def _anywhere(self, term: str) -> bool:
        """Whether the seed names something in a repository outside the scope."""
        spellings = [term] if ":" in term else [term, f"env:{term}", f"module:{term}"]
        if self._knowledge.get_entities_by_key(spellings):
            return True
        return bool(self._search.find_entities_by_name([term], None, 1))

    # -- one answer -------------------------------------------------------------

    def _one(
        self,
        term: str,
        found: Sequence[EntityIdentity],
        basis: Callable[[EntityIdentity], SeedBasis],
    ) -> Seed:
        ordered = sorted(found, key=_candidate_order)
        if len(ordered) > 1:
            return Seed(
                ImpactSeed(
                    status=SeedStatus.AMBIGUOUS,
                    term=term,
                    candidates=tuple(
                        SeedCandidate(
                            entity=i.key,
                            type=i.type,
                            repository=i.repository_slug,
                            path=i.path,
                            lines=_lines(i),
                        )
                        for i in ordered[:MAX_SEED_CANDIDATES]
                    ),
                    total=len(ordered),
                )
            )
        identity = ordered[0]
        granularity = _granularity(identity)
        return Seed(
            ImpactSeed(
                status=SeedStatus.RESOLVED,
                term=term,
                entity=identity.key,
                type=identity.type,
                repository=identity.repository_slug,
                path=identity.path,
                lines=_lines(identity),
                unit=self._unit(identity.key, identity.repository_slug, identity.path),
                basis=basis(identity),
                granularity=granularity,
                members=self._members(identity, granularity),
            ),
            identity=identity,
        )

    def _members(self, identity: EntityIdentity, granularity: Granularity) -> int | None:
        """A file's declarations; a declaration's or key's own members; a repository's files."""
        slug, path = identity.repository_slug, identity.path
        if identity.type is EntityType.REPOSITORY and identity.qualified_name:
            return self._knowledge.count_keys_with_prefix(file_key(identity.qualified_name, ""))
        if slug is None or path is None:
            return None
        if granularity is Granularity.FILE:
            prefix = f"sym:{slug}:{path}:"
            return self._knowledge.count_keys_with_prefix(prefix, exclude_types=[EntityType.MODULE])
        if not identity.qualified_name:
            return None
        count = self._knowledge.count_keys_with_prefix(
            f"sym:{slug}:{path}:{identity.qualified_name}."
        )
        return count or None

    def unit_of(self, key: str, slug: str | None, path: str | None) -> str:
        return self._unit(key, slug, path)

    def _unit(self, key: str, slug: str | None, path: str | None) -> str:
        if self._directories is None:
            manifests = self._knowledge.provided_manifests(sorted(self._slugs))
            self._directories = {
                repo: package_directories(paths) for repo, paths in manifests.items()
            }
        return unit_of(key, slug, path, self._directories)


def _granularity(identity: EntityIdentity) -> Granularity:
    if identity.type is EntityType.FILE:
        return Granularity.FILE
    if identity.type is EntityType.REPOSITORY:
        return Granularity.SUBTREE
    if identity.type in _KEYS:
        return Granularity.KEY
    return Granularity.SYMBOL


def _unique(found: Sequence[EntityIdentity]) -> list[EntityIdentity]:
    """One identity per entity row, in a fixed order."""
    by_id = {identity.entity_id: identity for identity in found}
    return sorted(by_id.values(), key=_candidate_order)
