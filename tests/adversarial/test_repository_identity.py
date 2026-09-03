"""Repository identity integrity (V1_SPEC §5, §9, §11, ARCHITECTURE.md §3, §7, §10).

The invariant under test, in one sentence:

    A repository's human-readable name is not its identity.

Two managed repositories may legitimately share a directory basename —
``teamA/common`` and ``teamB/common`` are different repositories that happen to
be called the same thing. Before this suite existed, that similarity was enough
to make them one thing to almost every subsystem: a single interned ``repo:common``
graph node, colliding ``file:common:index.ts`` and ``sym:common:index.ts:...``
keys, one merged evidence candidate, one grounding span-map entry, and two
indistinguishable lines of output.

Every test below asserts that some subsystem distinguishes the two by identity
rather than by appearance, and several assert the converse — that strengthening
repository identity did not make globally shared entities repository-local, and
did not turn an honest refusal into an arbitrary choice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.errors import ContextResolutionError
from repoatlas.llm.grounding import ground_answer
from repoatlas.models.context import ContextScope
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)
from repoatlas.rendering.text import render_evidence, render_repos
from repoatlas.retrieval.ranking import deduplicate, fuse
from repoatlas.services.init_service import initialize
from repoatlas.services.query_service import locate
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, init_git_repository, reindex_from_scratch

pytestmark = pytest.mark.adversarial


# --- fixtures -----------------------------------------------------------------


INDEX_TS = """export const SHARED_NAME = "shared";

export function {unique}(): string {{
  return SHARED_NAME;
}}
"""


def package_json(name: str, dependencies: dict[str, str] | None = None) -> str:
    deps = ""
    if dependencies:
        entries = ", ".join(f'"{k}": "{v}"' for k, v in dependencies.items())
        deps = f',\n  "dependencies": {{{entries}}}'
    return f'{{\n  "name": "{name}",\n  "version": "1.0.0"{deps}\n}}\n'


def make_repo(root: Path, relative: str, files: dict[str, str]) -> Path:
    repo = root / relative
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def build_duplicate_tree(root: Path) -> None:
    """``teamA/common`` and ``teamB/common``, plus a consumer of one of them."""
    make_repo(
        root,
        "teamA/common",
        {
            "index.ts": INDEX_TS.format(unique="TEAM_A_ONLY"),
            "package.json": package_json("@company/shared"),
        },
    )
    make_repo(
        root,
        "teamB/common",
        {
            "index.ts": INDEX_TS.format(unique="TEAM_B_ONLY"),
            "package.json": package_json("@company/teamb-only"),
        },
    )
    make_repo(
        root,
        "svc",
        {
            "app.ts": 'import { SHARED_NAME } from "@company/shared";\n',
            "package.json": package_json("@company/svc", {"@company/shared": "^1.0.0"}),
        },
    )


def make_settings(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


def open_indexed(tmp_path: Path, root: Path, tag: str = "state") -> Workspace:
    space = open_workspace(settings=make_settings(tmp_path, tag), require_root=False)
    initialize(space, root, config_path=tmp_path / f"{tag}-config.yaml")
    return space


@pytest.fixture
def duplicates(tmp_path: Path) -> Workspace:
    root = tmp_path / "root"
    root.mkdir()
    build_duplicate_tree(root)
    return open_indexed(tmp_path, root)


def rows(space: Workspace, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    return [tuple(row) for row in space.database.execute(sql, params).fetchall()]


def repo_at(space: Workspace, relative: str):
    root = space.settings.root
    assert root is not None
    stored = space.repositories.get_by_path(root / relative)
    assert stored is not None, f"{relative} is not managed"
    return stored


def locate_from(space: Workspace, question: str, cwd: Path | None = None) -> EvidenceSet:
    root = space.settings.root
    assert root is not None
    return locate(space, question, space.resolver().resolve(cwd or root))


def sync_root(space: Workspace) -> None:
    root = space.settings.root
    assert root is not None
    sync(space, space.resolver().resolve(root))


# --- 1. repository identity ---------------------------------------------------


def test_two_repositories_with_one_basename_have_different_identities(
    duplicates: Workspace,
) -> None:
    """The display name is shared; the identity must not be."""
    team_a = repo_at(duplicates, "teamA/common")
    team_b = repo_at(duplicates, "teamB/common")

    assert team_a.name == team_b.name == "common"
    assert team_a.slug != team_b.slug
    assert {team_a.slug, team_b.slug} == {"teamA/common", "teamB/common"}


def test_repository_identity_is_stored_and_unique(duplicates: Workspace) -> None:
    slugs = [row[0] for row in rows(duplicates, "SELECT slug FROM repositories")]

    assert len(slugs) == len(set(slugs)) == 3
    assert all(slug for slug in slugs), "every managed repository needs an identity"


def test_repository_identity_does_not_depend_on_discovery_order(tmp_path: Path) -> None:
    """No `common#1`/`common#2`: adding a repository must not renumber another."""
    root = tmp_path / "root"
    root.mkdir()
    build_duplicate_tree(root)
    first = open_indexed(tmp_path, root, tag="first")
    before = {row[0]: row[1] for row in rows(first, "SELECT path, slug FROM repositories")}
    first.close()

    make_repo(root, "aaa/common", {"index.ts": "export const AAA = 1;\n"})
    second = open_indexed(tmp_path, root, tag="second")
    after = {row[0]: row[1] for row in rows(second, "SELECT path, slug FROM repositories")}
    second.close()

    assert all(after[path] == slug for path, slug in before.items())


def test_repository_graph_nodes_are_distinct(duplicates: Workspace) -> None:
    """One interned `repo:common` node used to stand for two repositories."""
    nodes = rows(
        duplicates,
        "SELECT key, repo_id FROM entities WHERE type = 'repository' AND file_id IS NULL",
    )

    assert len({key for key, _ in nodes}) == 3
    assert len({repo_id for _, repo_id in nodes}) == 3


# --- 2. file and symbol identity ----------------------------------------------


def test_file_entity_keys_are_distinct(duplicates: Workspace) -> None:
    """`file:common:index.ts` named two different files."""
    keys = rows(
        duplicates,
        "SELECT e.key FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE e.type = 'file' AND f.rel_path = 'index.ts'",
    )

    assert len(keys) == 2
    assert len({key for (key,) in keys}) == 2


def test_symbol_entity_keys_are_distinct(duplicates: Workspace) -> None:
    """Same name, same relative path, same line, two repositories."""
    keys = rows(
        duplicates,
        "SELECT e.key, e.line_start FROM entities e WHERE e.name = 'SHARED_NAME'",
    )

    assert len(keys) == 2
    assert len({line for _, line in keys}) == 1, "the fixture must collide on line number"
    assert len({key for key, _ in keys}) == 2


def test_identical_files_in_two_repositories_are_not_deduplicated(tmp_path: Path) -> None:
    """Byte-identical content in two repositories is two facts, not one."""
    root = tmp_path / "root"
    root.mkdir()
    same = "export const SAME = 1;\n"
    make_repo(root, "libs/common", {"index.ts": same})
    make_repo(root, "apps/common", {"index.ts": same})
    space = open_indexed(tmp_path, root)

    files = rows(space, "SELECT id FROM files WHERE rel_path = 'index.ts'")
    entities = rows(
        space,
        "SELECT e.key FROM entities e JOIN files f ON f.id = e.file_id WHERE e.type = 'file'",
    )

    assert len(files) == 2
    assert len({key for (key,) in entities}) == 2
    space.close()


# --- 3. retrieval, fusion and deduplication -----------------------------------


def test_a_symbol_in_both_repositories_returns_two_candidates(duplicates: Workspace) -> None:
    """Two repositories define SHARED_NAME. Both are evidence."""
    evidence = locate_from(duplicates, "SHARED_NAME")
    sources = {(c.repository_slug, c.path) for c in evidence.candidates}

    assert ("teamA/common", "index.ts") in sources
    assert ("teamB/common", "index.ts") in sources


def test_a_unique_symbol_resolves_to_exactly_one_repository(duplicates: Workspace) -> None:
    """Only one repository defines it, so only one may be cited as defining it.

    Asserted over the *deterministic* candidates. The other ``common`` repository
    holds a near-identical file and is a perfectly good associative neighbour; what
    must never happen is that its evidence is presented as a fact about a symbol it
    does not contain (V1_SPEC §14).
    """
    for symbol, slug in (("TEAM_A_ONLY", "teamA/common"), ("TEAM_B_ONLY", "teamB/common")):
        evidence = locate_from(duplicates, symbol)
        found = {c.repository_slug for c in evidence.deterministic if c.path == "index.ts"}

        assert found == {slug}, f"{symbol} attributed outside {slug}: {found}"


def test_fusion_does_not_merge_identical_appearances_from_two_repositories() -> None:
    """Same display name, path, line span, text — still two provenance sources."""

    def candidate(slug: str) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository="common",
            repository_slug=slug,
            path="index.ts",
            line_start=1,
            line_end=1,
            text='export const SHARED_NAME = "shared";',
        )

    fused = fuse([[candidate("teamA/common")], [candidate("teamB/common")]])

    assert len(fused) == 2
    assert {c.repository_slug for c in fused} == {"teamA/common", "teamB/common"}


def test_deduplication_does_not_merge_across_repositories() -> None:
    def candidate(slug: str) -> EvidenceCandidate:
        return EvidenceCandidate(
            repository="common",
            repository_slug=slug,
            path="index.ts",
            line_start=1,
            line_end=20,
            text="identical",
        )

    kept = deduplicate([candidate("teamA/common"), candidate("teamB/common")])

    assert len(kept) == 2


def test_the_primary_repository_boost_follows_identity_not_display_name(
    duplicates: Workspace,
) -> None:
    """Being inside teamB/common must not boost teamA/common."""
    root = duplicates.settings.root
    assert root is not None
    evidence = locate_from(duplicates, "SHARED_NAME", cwd=root / "teamB" / "common")
    by_slug = {c.repository_slug: c.score for c in evidence.candidates if c.path == "index.ts"}

    assert by_slug["teamB/common"] > by_slug["teamA/common"]


# --- 4. graph relationships ---------------------------------------------------


def test_a_dependency_reaches_only_the_providing_repository(duplicates: Workspace) -> None:
    """svc depends on @company/shared, which only teamA/common provides."""
    edges = rows(
        duplicates,
        "SELECT src.key, dst.key FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE src.type = 'repository' AND dst.type = 'repository'",
    )
    targets = {str(dst) for _, dst in edges}

    assert any("teamA/common" in target for target in targets)
    assert not any("teamB/common" in target for target in targets)


def test_two_providers_of_one_package_still_produce_no_edge(tmp_path: Path) -> None:
    """Ambiguity refuses; repository identity must not become a tie-breaker."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "teamA/common", {"package.json": package_json("@company/shared")})
    make_repo(root, "teamB/common", {"package.json": package_json("@company/shared")})
    make_repo(
        root, "svc", {"package.json": package_json("@company/svc", {"@company/shared": "^1.0.0"})}
    )
    space = open_indexed(tmp_path, root)

    edges = rows(
        space,
        "SELECT src.key, dst.key FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE src.type = 'repository' AND dst.type = 'repository'",
    )

    assert edges == []
    space.close()


def test_interned_global_entities_stay_shared(tmp_path: Path) -> None:
    """SERVICE_HOST is one hub connecting repositories, not one node per repo."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "teamA/common", {"a.py": 'import os\nA = os.environ["SERVICE_HOST"]\n'})
    make_repo(root, "teamB/common", {"b.py": 'import os\nB = os.environ["SERVICE_HOST"]\n'})
    space = open_indexed(tmp_path, root)

    hubs = rows(space, "SELECT id, key FROM entities WHERE key = 'env:SERVICE_HOST'")
    edges = rows(
        space,
        "SELECT e.repo_id FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key = 'env:SERVICE_HOST'",
    )

    assert len(hubs) == 1, "the environment variable must remain one interned entity"
    assert len({repo_id for (repo_id,) in edges}) == 2, "both repositories must reach it"
    space.close()


# --- 5. grounding -------------------------------------------------------------


def supplied(slug: str) -> EvidenceSet:
    return EvidenceSet(
        question="where is SHARED_NAME defined?",
        candidates=(
            EvidenceCandidate(
                repository="common",
                repository_slug=slug,
                path="index.ts",
                line_start=1,
                line_end=3,
                text='export const SHARED_NAME = "shared";',
            ),
        ),
    )


def answer_citing(slug: str | None) -> RepoAtlasAnswer:
    return RepoAtlasAnswer(
        summary="SHARED_NAME is defined in the shared package.",
        hypotheses=[
            Hypothesis(
                statement="SHARED_NAME is exported at the top of the module.",
                confidence=0.9,
                evidence=[
                    Evidence(
                        repository="common",
                        repository_slug=slug,
                        path="index.ts",
                        line_start=1,
                        line_end=3,
                        description="the export",
                    )
                ],
            )
        ],
    )


def test_evidence_from_one_repository_cannot_validate_a_citation_to_the_other() -> None:
    """The security-relevant case: identical appearance, different provenance."""
    report = ground_answer(answer_citing("teamB/common"), supplied("teamA/common"))

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


def test_a_citation_to_the_supplied_repository_survives() -> None:
    report = ground_answer(answer_citing("teamA/common"), supplied("teamA/common"))

    assert len(report.answer.hypotheses) == 1
    assert report.dropped_citations == 0


def test_an_ambiguous_citation_is_refused_when_two_sources_share_a_display_name() -> None:
    """With both `common` repositories supplied, a name-only citation names neither."""
    both = EvidenceSet(
        question="where is SHARED_NAME defined?",
        candidates=(
            *supplied("teamA/common").candidates,
            *supplied("teamB/common").candidates,
        ),
    )

    report = ground_answer(answer_citing(None), both)

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


def test_a_name_only_citation_still_works_when_the_display_name_is_unique() -> None:
    """Concise citations must keep working for ordinary, unambiguous repositories."""
    report = ground_answer(answer_citing(None), supplied("teamA/common"))

    assert len(report.answer.hypotheses) == 1


# --- 6. serialization and rendering -------------------------------------------


def test_evidence_serialization_distinguishes_two_sources() -> None:
    from repoatlas.llm.reasoner import format_evidence

    both = EvidenceSet(
        question="q",
        candidates=(
            *supplied("teamA/common").candidates,
            *supplied("teamB/common").candidates,
        ),
    )

    serialized = format_evidence(both)

    assert "teamA/common" in serialized
    assert "teamB/common" in serialized


def test_evidence_rendering_disambiguates_colliding_repositories() -> None:
    both = EvidenceSet(
        question="q",
        candidates=(
            *supplied("teamA/common").candidates,
            *supplied("teamB/common").candidates,
        ),
    )

    rendered = render_evidence(both)

    assert "teamA/common/index.ts:1-3" in rendered
    assert "teamB/common/index.ts:1-3" in rendered


def test_evidence_rendering_stays_concise_when_names_are_unique() -> None:
    single = EvidenceSet(
        question="q",
        candidates=(
            EvidenceCandidate(
                repository="python-service",
                repository_slug="services/python-service",
                path="src/config.py",
                line_start=14,
                line_end=14,
                text="HOST = 1",
            ),
        ),
    )

    assert "python-service/src/config.py:14" in render_evidence(single)


def test_repos_listing_distinguishes_colliding_names(duplicates: Workspace) -> None:
    rendered = render_repos(duplicates.repositories.list_all())

    assert "teamA/common" in rendered
    assert "teamB/common" in rendered


def test_repos_listing_stays_concise_when_names_are_unique(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "alpha", {"a.py": "A = 1\n"})
    make_repo(root, "beta", {"b.py": "B = 2\n"})
    space = open_indexed(tmp_path, root)

    rendered = render_repos(space.repositories.list_all())

    assert "alpha" in rendered
    assert "/" not in rendered
    space.close()


# --- 7. context resolution ----------------------------------------------------


def test_cwd_resolution_picks_the_containing_repository(duplicates: Workspace) -> None:
    root = duplicates.settings.root
    assert root is not None
    resolver = duplicates.resolver()

    for team in ("teamA", "teamB"):
        deep = root / team / "common" / "src" / "deep"
        deep.mkdir(parents=True, exist_ok=True)
        context = resolver.resolve(deep)

        assert context.primary is not None
        assert context.primary.slug == f"{team}/common"


def test_an_ambiguous_explicit_name_is_still_refused(duplicates: Workspace) -> None:
    root = duplicates.settings.root
    assert root is not None

    with pytest.raises(ContextResolutionError) as raised:
        duplicates.resolver().resolve(root, "common")

    assert "ambiguous" in str(raised.value).lower()


def test_a_name_matching_another_repositorys_identity_is_still_ambiguous(tmp_path: Path) -> None:
    """`scripts` and `nested/scripts`: the identity of one equals the name of both.

    Resolving identities first and stopping there would answer this by picking the
    top-level repository — an arbitrary choice dressed up as an exact match.
    """
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "scripts", {"a.sh": "#!/usr/bin/env bash\necho a\n"})
    make_repo(root, "nested/scripts", {"b.sh": "#!/usr/bin/env bash\necho b\n"})
    space = open_indexed(tmp_path, root)

    with pytest.raises(ContextResolutionError) as raised:
        space.resolver().resolve(root, "scripts")

    assert "ambiguous" in str(raised.value).lower()
    assert space.resolver().resolve(root, "nested/scripts").primary is not None
    space.close()


def test_an_explicit_identity_selects_exactly_one_repository(duplicates: Workspace) -> None:
    root = duplicates.settings.root
    assert root is not None

    context = duplicates.resolver().resolve(root, "teamB/common")

    assert context.scope is ContextScope.REPOSITORY
    assert context.primary is not None
    assert context.primary.slug == "teamB/common"


# --- 8. many duplicates, and case ---------------------------------------------


def test_duplicate_names_at_several_nesting_levels(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for relative in ("apps/api", "backend/api", "services/common", "libs/common", "deep/x/common"):
        make_repo(root, relative, {"index.ts": f"export const N = '{relative}';\n"})
    space = open_indexed(tmp_path, root)

    slugs = {row[0] for row in rows(space, "SELECT slug FROM repositories")}
    keys = {row[0] for row in rows(space, "SELECT key FROM entities WHERE type = 'file'")}

    assert len(slugs) == 5
    assert len(keys) == 5
    space.close()


def test_repository_identity_is_case_sensitive(tmp_path: Path) -> None:
    """`Common` and `common` are different directories on this filesystem."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root, "Common", {"a.ts": "export const A = 1;\n"})
    make_repo(root, "common", {"b.ts": "export const B = 2;\n"})
    space = open_indexed(tmp_path, root)

    slugs = {row[0] for row in rows(space, "SELECT slug FROM repositories")}

    assert slugs == {"Common", "common"}
    space.close()


# --- 9. lifecycle -------------------------------------------------------------


def test_deleting_one_duplicate_leaves_the_other_intact(duplicates: Workspace) -> None:
    import shutil

    root = duplicates.settings.root
    assert root is not None
    team_b = repo_at(duplicates, "teamB/common")
    shutil.rmtree(root / "teamA" / "common")
    sync_root(duplicates)

    remaining = {c.repository_slug for c in locate_from(duplicates, "SHARED_NAME").candidates}
    nodes = {
        str(row[0])
        for row in rows(
            duplicates, "SELECT key FROM entities WHERE type = 'repository' AND file_id IS NULL"
        )
    }
    still_there = duplicates.repositories.get_by_path(root / "teamB" / "common")

    assert "teamA/common" not in remaining
    assert "teamB/common" in remaining
    assert not any("teamA/common" in key for key in nodes)
    assert still_there is not None
    assert still_there.id == team_b.id
    assert locate_from(duplicates, "TEAM_B_ONLY").candidates


def test_a_moved_repository_leaves_no_stale_identity(duplicates: Workspace) -> None:
    """A move is a removal plus a discovery: the old identity must not survive."""
    root = duplicates.settings.root
    assert root is not None
    (root / "teamA" / "common").rename(root / "teamA" / "shared")
    initialize(duplicates, root, config_path=root.parent / "config.yaml")

    slugs = {row[0] for row in rows(duplicates, "SELECT slug FROM repositories")}
    evidence = locate_from(duplicates, "TEAM_A_ONLY")
    found = {c.repository_slug for c in evidence.deterministic if c.path == "index.ts"}
    keys = {str(row[0]) for row in rows(duplicates, "SELECT key FROM entities")}

    assert "teamA/shared" in slugs
    assert "teamA/common" not in slugs
    assert found == {"teamA/shared"}
    assert not any("teamA/common" in key for key in keys), "a stale identity survived the move"


def downgrade_to_schema_v4(database: Path) -> None:
    """Rewrite a v5 database into exactly the shape schema v4 produced.

    Migration tests that only ever see a fresh database prove nothing about the
    upgrade path, and this pass changes what every repository-scoped key *means*.
    So the database is genuinely regressed: the identity column is removed, every
    key is rewritten back to the display-name form, and the duplicate repository
    nodes are collapsed into the single interned row that v4's unique-key index
    forced them to be. Reopening it then exercises 005 against v4 data.
    """
    import sqlite3

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    with connection:
        names = {
            row["id"]: row["name"]
            for row in connection.execute("SELECT id, name FROM repositories")
        }
        slugs = {
            row["id"]: row["slug"]
            for row in connection.execute("SELECT id, slug FROM repositories")
        }
        for repo_id, slug in slugs.items():
            name = names[repo_id]
            connection.execute(
                "UPDATE entities SET key = replace(key, ?, ?) WHERE key LIKE ?",
                (f"file:{slug}:", f"file:{name}:", f"file:{slug}:%"),
            )
            connection.execute(
                "UPDATE entities SET key = replace(key, ?, ?) WHERE key LIKE ?",
                (f"sym:{slug}:", f"sym:{name}:", f"sym:{slug}:%"),
            )
        # v4 could hold only one interned node per name, whichever was seen first.
        for repo_id, slug in slugs.items():
            connection.execute(
                "UPDATE OR IGNORE entities SET key = ?, qualified_name = NULL "
                "WHERE key = ? AND file_id IS NULL",
                (f"repo:{names[repo_id]}", f"repo:{slug}"),
            )
        connection.execute("DELETE FROM entities WHERE key LIKE 'repo:%/%'")
        # The stored per-file `parser_version` embeds the schema version, and it is
        # what makes an upgrade reparse every file. A downgrade that left it saying
        # `schema:5` would make the upgrade a no-op and the test vacuous.
        connection.execute(
            "UPDATE files SET parser_version = replace(parser_version, 'schema:5', 'schema:4')"
        )
        connection.execute("DROP INDEX IF EXISTS idx_repositories_slug")
        connection.execute("ALTER TABLE repositories DROP COLUMN slug")
        connection.execute("PRAGMA user_version = 4")
    connection.close()


def test_a_schema_v4_database_upgrades_to_correct_identities(tmp_path: Path) -> None:
    """The upgrade path, from real v4 data with a duplicated repository name."""
    root = tmp_path / "root"
    root.mkdir()
    build_duplicate_tree(root)
    settings = make_settings(tmp_path)
    database = tmp_path / "state" / "atlas.db"

    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    space.close()
    downgrade_to_schema_v4(database)

    upgraded = open_workspace(settings=settings, require_root=False)
    upgraded.settings = upgraded.settings.model_copy(
        update={"repos": upgraded.settings.repos.model_copy(update={"root": root})}
    )
    sync_root(upgraded)

    slugs = {row[0] for row in rows(upgraded, "SELECT slug FROM repositories")}
    keys = {str(row[0]) for row in rows(upgraded, "SELECT key FROM entities")}
    nodes = {
        str(row[0])
        for row in rows(
            upgraded, "SELECT key FROM entities WHERE type = 'repository' AND file_id IS NULL"
        )
    }

    assert slugs == {"svc", "teamA/common", "teamB/common"}
    assert nodes == {"repo:svc", "repo:teamA/common", "repo:teamB/common"}
    assert "file:common:index.ts" not in keys, "a display-name key survived the upgrade"
    assert {"file:teamA/common:index.ts", "file:teamB/common:index.ts"} <= keys
    upgraded.close()


def test_an_upgraded_database_equals_a_clean_full_index(tmp_path: Path) -> None:
    """§24: the upgraded state and a from-scratch index must be the same state."""
    root = tmp_path / "root"
    root.mkdir()
    build_duplicate_tree(root)
    settings = make_settings(tmp_path)
    database = tmp_path / "state" / "atlas.db"

    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    space.close()
    downgrade_to_schema_v4(database)

    upgraded = open_workspace(settings=settings, require_root=False)
    upgraded.settings = upgraded.settings.model_copy(
        update={"repos": upgraded.settings.repos.model_copy(update={"root": root})}
    )
    sync_root(upgraded)

    assert fact_snapshot(upgraded) == reindex_from_scratch(settings, root, tmp_path, "clean")
    upgraded.close()


def test_incremental_state_equals_a_full_reindex(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    build_duplicate_tree(root)
    settings = make_settings(tmp_path)
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")

    (root / "teamB" / "common" / "index.ts").write_text(
        INDEX_TS.format(unique="TEAM_B_ONLY") + "export const EXTRA = 1;\n", encoding="utf-8"
    )
    sync_root(space)

    assert fact_snapshot(space) == reindex_from_scratch(settings, root, tmp_path, "fresh")
    space.close()
