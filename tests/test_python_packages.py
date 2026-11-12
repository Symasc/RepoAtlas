"""Python uses and ordinary imports across projects, proven by declarations (P12b-P18).

Every repository is indexed whole; each case names the declaration, pin or layout that
must decide it, and what it resolves to -- or why it refuses. The fixtures are in
:mod:`tests.python_packages_corpus`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import RelationshipDirection, RelationshipsRequest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit
from tests.python_corpus import refusals
from tests.python_packages_corpus import (
    MODELS,
    SERVICE,
    TWINS,
    UVWS,
    XREPO,
    consumer_uses,
    edges,
    uses,
)

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SEED = "sym:provider:src/internal_fixture/models.py:make:3"


@pytest.fixture
def xrepo(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, XREPO)


def test_p14_a_version_declaration_reaches_the_one_provider(xrepo: Workspace) -> None:
    """Through the provider's `__init__` under both names (P17), never the shadow."""
    assert uses(xrepo, "consumer") == consumer_uses("consumer")
    assert not [e for e in edges(xrepo) if e["target"].startswith("shadow:")]
    names = {
        (e["local"], tuple(e["via"]))
        for e in edges(xrepo)
        if e["source"] == f"consumer:{SERVICE}#build" and e["target"] == f"{MODELS}#Widget"
    }
    assert names == {
        ("Widget", ("src/internal_fixture/__init__.py",)),
        ("PublicWidget", ("src/internal_fixture/__init__.py",)),
    }


def test_p14_the_crossing_records_what_proved_it(xrepo: Workspace) -> None:
    [edge] = [
        e
        for e in edges(xrepo)
        if e["source"] == f"consumer:{SERVICE}#via_module" and e["target"] == f"{MODELS}#make"
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:internal-fixture",
        "provider": "provider",
        "provided_at": "pyproject.toml:2",
        "entry": "src/internal_fixture/models.py",
        "entry_fields": "src-layout",
        "subpath": "models",
        "declared_at": "pyproject.toml:3",
        "constraint": ">=1.0",
        "imported_at": 3,
    }


def test_p15_a_uv_path_pins_the_provider(xrepo: Workspace) -> None:
    assert uses(xrepo, "pinned") == consumer_uses("pinned")
    crossings = {
        (e["crossing"]["resolved_by"], e["crossing"]["pinned_at"])
        for e in edges(xrepo)
        if e["source"].startswith("pinned:src/") and "crossing" in e
    }
    assert crossings == {("path", "pinned/pyproject.toml")}


def test_p15_a_pin_that_fails_never_falls_back_to_the_name(xrepo: Workspace) -> None:
    """`internal-fixture` has one provider, but these pins do not lead to it."""
    names = {"Widget", "PublicWidget", "make", "models.make"}
    assert refusals(xrepo, "mispinned", SERVICE) == dict.fromkeys(names, "dependency-elsewhere")
    assert refusals(xrepo, "unpinned", SERVICE) == dict.fromkeys(names, "path-outside-index")
    for repository in ("mispinned", "unpinned"):
        test = f"{repository}:tests/test_service.py#test_build"
        assert uses(xrepo, repository) == {(test, f"{repository}:{SERVICE}#build", "call")}


def test_p13_a_distribution_is_declared_by_its_own_name_never_its_module(
    xrepo: Workspace,
) -> None:
    assert uses(xrepo, "rightname") == {
        ("rightname:src/importer/use.py#use", "distpkg:src/fixture_pkg/__init__.py#Thing", "type")
    }
    assert refusals(xrepo, "wrongname", "src/importer/use.py") == {"Thing": "dependency-absent"}


def test_declarations_in_forms_not_read_are_refused_by_name(xrepo: Workspace) -> None:
    assert refusals(xrepo, "unread", "src/unread/use.py") == {
        "Thing": "dependency-unread",  # `fixture-dist @ file:...`
        "Widget": "dependency-elsewhere",  # a git source
    }
    assert refusals(xrepo, "poetic", "poetic/use.py") == {"Widget": "dependency-unread"}


def test_p16_two_providers_of_one_name_join_neither(workspace: Workspace, tmp_path: Path) -> None:
    twins = build(workspace, tmp_path, TWINS)
    assert set(refusals(twins, "consumer", SERVICE).values()) == {"provider-ambiguous"}
    assert not [
        e
        for e in edges(twins)
        if e["source"].startswith("consumer:") and e["target"].startswith(("provider:", "twin:"))
    ]


def test_p16_two_declared_projects_shipping_one_module_join_neither(xrepo: Workspace) -> None:
    assert set(refusals(xrepo, "both", SERVICE).values()) == {"provider-ambiguous"}
    assert not [
        e
        for e in edges(xrepo)
        if e["source"].startswith("both:") and e["target"].startswith(("provider:", "shadow:"))
    ]


def test_p12b_a_uv_workspace_member_inherits_its_roots_source(
    workspace: Workspace, tmp_path: Path
) -> None:
    uvws = build(workspace, tmp_path, {"uvws": UVWS})
    helper = "uvws:packages/lib/src/lib/__init__.py#helper"
    found = {
        (e["source"], e["resolved_by"], e["pinned_at"])
        for e in edges(uvws)
        if e["target"] == helper and e["basis"] == "python-dependency"
    }
    assert found == {
        # no source of its own: the root's, which every member inherits
        ("uvws:packages/app/src/app/__init__.py#run", "workspace", "uvws/pyproject.toml"),
        # its own source overrides the root's
        ("uvws:packages/tool/src/tool/__init__.py#go", "path", "uvws/packages/tool/pyproject.toml"),
    }
    assert all("crossing" not in e for e in edges(uvws))
    assert refusals(uvws, "uvws", "packages/legacy/src/legacy/__init__.py") == {
        "helper": "workspace-not-member"
    }


def test_p18_a_change_reaches_the_consumer_in_another_repository_and_its_test(
    xrepo: Workspace,
) -> None:
    response = CapabilityService(xrepo).impact(ImpactRequest(context=GLOBAL, seed=SEED))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    consumer = by_entity[f"sym:consumer:{SERVICE}:build:4"]
    assert consumer.distance == 1
    assert consumer.step.crossing is not None
    assert consumer.step.crossing.package == "pypi:internal-fixture"
    assert consumer.step.crossing.declared_by == "consumer/pyproject.toml:3"
    [test] = [c for c in response.tests if c.entity.startswith("sym:consumer:tests/")]
    assert (test.distance, test.from_) == (2, consumer.id)


def test_a_dependent_is_listed_only_when_its_declaration_names_the_provider(
    xrepo: Workspace,
) -> None:
    """A version, or a uv path to the provider, names it; a uv path anywhere else, a git
    source, a direct reference or a Poetry table does not -- as a package link reads it.
    Coarse by construction: ``both`` declares it by version, whatever else it declares."""
    response = CapabilityService(xrepo).impact(ImpactRequest(context=GLOBAL, seed=SEED))
    listed = {p.dependent for p in response.packages if p.package == "pypi:internal-fixture"}
    assert listed == {"consumer:.", "pinned:.", "both:."}


def test_ordinary_python_imports_and_calls_follow_proven_project_and_provider(
    xrepo: Workspace,
) -> None:
    rows = xrepo.database.execute(
        """
        SELECT sr.slug, sf.rel_path, e.type, e.line_start, dr.slug, df.rel_path,
               CASE WHEN d.type = 'file' THEN NULL ELSE d.qualified_name END, e.resolution
        FROM edges e JOIN files sf ON sf.id = e.file_id
        JOIN repositories sr ON sr.id = sf.repo_id
        JOIN entities d ON d.id = e.dst_id JOIN files df ON df.id = d.file_id
        JOIN repositories dr ON dr.id = df.repo_id
        WHERE e.parser = 'linker:global' AND e.type IN ('IMPORTS', 'CALLS')
          AND sr.slug IN ('consumer', 'pinned', 'mispinned', 'unpinned', 'both')
        """
    ).fetchall()
    found = {(r[0], r[1], r[2], r[3], r[4], r[5], r[6]): r[7] for r in rows}
    for repo in ("consumer", "pinned"):
        expected = {
            (repo, SERVICE, "IMPORTS", 1, "provider", "src/internal_fixture/__init__.py", None),
            (repo, SERVICE, "IMPORTS", 2, "provider", "src/internal_fixture/models.py", None),
            (repo, SERVICE, "IMPORTS", 3, "provider", "src/internal_fixture/models.py", None),
            (repo, SERVICE, "CALLS", 5, "provider", "src/internal_fixture/models.py", "make"),
            (repo, SERVICE, "CALLS", 7, "provider", "src/internal_fixture/models.py", "make"),
        }
        assert expected <= found.keys()
        for key in expected:
            crossing = json.loads(found[key])["crossing"]
            assert crossing["package"] == "pypi:internal-fixture"
            assert crossing["provider"] == "provider"
            assert crossing["imported_at"] == (2 if key[3] == 5 else 3 if key[3] == 7 else key[3])

    assert (
        "consumer",
        "tests/test_service.py",
        "IMPORTS",
        1,
        "consumer",
        SERVICE,
        None,
    ) in found
    assert (
        "consumer",
        "tests/test_service.py",
        "CALLS",
        3,
        "consumer",
        SERVICE,
        "build",
    ) in found
    assert not [
        key for key in found if key[0] in {"mispinned", "unpinned", "both"} and key[0] != key[4]
    ]


def test_class_reference_crosses_only_to_a_declared_python_provider(
    workspace: Workspace, tmp_path: Path
) -> None:
    source = "from internal_fixture.models import Widget as Base\nclass Concrete(Base):\n    pass\n"
    consumer = {**XREPO["consumer"], SERVICE: source}
    unproven = {**consumer, "pyproject.toml": '[project]\nname = "unproven"\n'}
    indexed = build(
        workspace,
        tmp_path,
        {"provider": XREPO["provider"], "consumer": consumer, "unproven": unproven},
    )
    rows = indexed.database.execute(
        """
        SELECT sr.slug, e.line_start, d.qualified_name, e.resolution FROM edges e
        JOIN files sf ON sf.id = e.file_id JOIN repositories sr ON sr.id = sf.repo_id
        JOIN entities d ON d.id = e.dst_id JOIN repositories dr ON dr.id = d.repo_id
        WHERE sf.rel_path = ? AND dr.slug = 'provider' AND e.parser = 'linker:global'
          AND e.type = 'REFERENCES'
        """,
        (SERVICE,),
    ).fetchall()
    [row] = rows
    assert tuple(row[:3]) == ("consumer", 2, "Widget")
    crossing = json.loads(row[3])["crossing"]
    assert (crossing["package"], crossing["imported_at"]) == ("pypi:internal-fixture", 1)


def test_proven_cross_repository_calls_are_explicitly_traversable(xrepo: Workspace) -> None:
    service = CapabilityService(xrepo)
    request = {
        "context": GLOBAL,
        "target": SEED,
        "edge_types": (EdgeType.CALLS,),
        "direction": RelationshipDirection.INCOMING,
        "max_neighbors": 200,
    }
    confined = service.relationships(RelationshipsRequest(**request))
    assert not any(edge.cross_repository for edge in confined.edges)
    crossed = service.relationships(RelationshipsRequest(**request, cross_repository=True))
    callers = {
        edge.source.key
        for edge in crossed.edges
        if edge.target.key == SEED and edge.cross_repository
    }
    assert {
        f"sym:consumer:{SERVICE}:build:4",
        f"sym:consumer:{SERVICE}:via_module:6",
    } <= callers


def test_impact_follows_a_proven_call_without_a_uses_edge(xrepo: Workspace) -> None:
    caller = f"sym:consumer:{SERVICE}:build:4"
    with xrepo.database.transaction() as conn:
        conn.execute(
            """
            DELETE FROM edges WHERE type = 'USES'
              AND src_id = (SELECT id FROM entities WHERE key = ?)
              AND dst_id = (SELECT id FROM entities WHERE key = ?)
            """,
            (caller, SEED),
        )
    response = CapabilityService(xrepo).impact(ImpactRequest(context=GLOBAL, seed=SEED))
    [consumer] = [candidate for candidate in response.candidates if candidate.entity == caller]
    assert consumer.step.edge == "CALLS"
    assert consumer.step.crossing is not None
    assert consumer.step.crossing.package == "pypi:internal-fixture"


def test_python_source_root_wins_over_a_same_named_unshipped_directory(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(
        workspace,
        tmp_path,
        {
            "app": {
                "pyproject.toml": '[project]\nname = "app"\n',
                "src/app/__init__.py": "",
                "src/app/use.py": "from helper import make\ndef run():\n    return make()\n",
                "src/helper/__init__.py": "def make():\n    return 'source root'\n",
                "helper/__init__.py": "def make():\n    return 'outside root'\n",
            }
        },
    )
    targets = indexed.database.execute(
        """
        SELECT e.type, df.rel_path FROM edges e
        JOIN files sf ON sf.id = e.file_id JOIN entities d ON d.id = e.dst_id
        JOIN files df ON df.id = d.file_id
        WHERE sf.rel_path = 'src/app/use.py' AND e.parser = 'linker:global'
          AND e.type IN ('IMPORTS', 'CALLS')
        ORDER BY e.type
        """
    ).fetchall()
    assert [tuple(row) for row in targets] == [
        ("CALLS", "src/helper/__init__.py"),
        ("IMPORTS", "src/helper/__init__.py"),
    ]


@pytest.mark.parametrize("shadowed", [False, True])
def test_provider_fromlist_follows_only_an_unshadowed_submodule(
    workspace: Workspace, tmp_path: Path, shadowed: bool
) -> None:
    provider = dict(XREPO["provider"])
    if shadowed:
        provider["src/internal_fixture/__init__.py"] = "models = object()\n"
    consumer = {
        **XREPO["consumer"],
        SERVICE: (
            "from internal_fixture import models as mod\n"
            "def run():\n    return mod.make()\n"
            "def not_a_call_to_make():\n    return mod.make.missing()\n"
        ),
    }
    indexed = build(workspace, tmp_path, {"provider": provider, "consumer": consumer})
    rows = indexed.database.execute(
        """
        SELECT e.type, e.line_start, df.rel_path, e.resolution FROM edges e
        JOIN files sf ON sf.id = e.file_id JOIN entities d ON d.id = e.dst_id
        JOIN files df ON df.id = d.file_id JOIN repositories dr ON dr.id = df.repo_id
        WHERE sf.rel_path = ? AND dr.slug = 'provider' AND e.parser = 'linker:global'
          AND e.type IN ('IMPORTS', 'CALLS')
        """,
        (SERVICE,),
    ).fetchall()
    targets = {(row[0], row[1], row[2]) for row in rows}
    assert ("IMPORTS", 1, "src/internal_fixture/__init__.py") in targets
    assert (("IMPORTS", 1, "src/internal_fixture/models.py") in targets) is not shadowed
    assert (("CALLS", 3, "src/internal_fixture/models.py") in targets) is not shadowed
    assert not any(row[0] == "CALLS" and row[1] == 5 for row in rows)
    if not shadowed:
        [submodule] = [row for row in rows if row[0] == "IMPORTS" and row[2].endswith("models.py")]
        crossing = json.loads(submodule[3])["crossing"]
        assert crossing["entry"] == "src/internal_fixture/models.py"
        assert crossing["imported_at"] == 1


def test_cross_repository_edges_track_dependency_changes_on_incremental_sync(
    xrepo: Workspace, tmp_path: Path
) -> None:
    def crossings(repo: str) -> set[str]:
        rows = xrepo.database.execute(
            """
            SELECT e.type FROM edges e JOIN files sf ON sf.id = e.file_id
            JOIN repositories sr ON sr.id = sf.repo_id JOIN entities d ON d.id = e.dst_id
            JOIN repositories dr ON dr.id = d.repo_id
            WHERE sr.slug = ? AND dr.slug = 'provider' AND sf.rel_path = ?
              AND e.parser = 'linker:global' AND e.type IN ('IMPORTS', 'CALLS')
            """,
            (repo, SERVICE),
        ).fetchall()
        return {row[0] for row in rows}

    assert crossings("consumer") == {"IMPORTS", "CALLS"}
    root = tmp_path / "repos"
    edit(
        root, "consumer", "pyproject.toml", '[project]\nname = "consumer-app"\ndependencies = []\n'
    )
    sync(xrepo, xrepo.resolver().resolve(root))
    assert crossings("consumer") == set()
    assert crossings("pinned") == {"IMPORTS", "CALLS"}
    assert_equals_fresh(xrepo, xrepo.settings, root, tmp_path, "provider-removed")
