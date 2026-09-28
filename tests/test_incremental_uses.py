"""Incremental ``USES`` resolution: an edit then a sync equals a fresh index of the same tree.

Each case edits one tree, syncs it, and holds the result to a pristine database indexing
the same tree (:func:`~tests.incremental_corpus.assert_equals_fresh`), then to the work
the sync's ``USES`` stage reports: which files it resolved again, and why. The fixtures
are in :mod:`tests.incremental_corpus`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, uses
from tests.incremental_corpus import (
    PROVIDER_MANIFEST_MOVED,
    add_repo,
    append,
    assert_equals_fresh,
    chain_repo,
    consumer_repo,
    dup_repo,
    edit,
    fresh_index,
    mono_repo,
    provider_repo,
    read_snapshot,
    remove_repo,
    sync_all,
    use_edge_rows,
    use_snapshot,
)

SHALLOW = ("shallow.ts#useShallow", "chain/c13.ts#Deep", "value")
DEEP = ("deep.ts#useDeep", "chain/c13.ts#Deep", "value")

KIT = "packages/kit/src"
WEB = "apps/web/src"
CONSUMER = f"{WEB}/consumer.ts"


def _use(source: str, target: str, kind: str = "value") -> tuple[str, str, str]:
    return (source, target, kind)


def _replace(root: Path, repo: str, path: str, old: str, new: str) -> None:
    text = (root / repo / path).read_text()
    assert old in text, f"{old!r} not in {path}"
    edit(root, repo, path, text.replace(old, new))


@pytest.fixture
def mono(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"mono": mono_repo()})


@pytest.fixture
def linked(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"provider": provider_repo(), "consumer": consumer_repo()})


# -- I0: order ---------------------------------------------------------------------------


def test_i0_a_use_gets_the_same_answer_whatever_was_resolved_before_it(
    settings: Settings, workspace: Workspace, tmp_path: Path
) -> None:
    """Thirteen hops is one too many, whether or not seven of them were traced before."""
    build(workspace, tmp_path, {"chain": chain_repo()})
    root = tmp_path / "repos"
    found = uses(workspace)
    assert SHALLOW in found
    assert DEEP not in found

    # Re-indexing deep.ts puts its claim after shallow.ts's. Touching c13.ts too, where
    # shallow.ts's use lands and deep.ts's never reaches, makes the pass resolve both files
    # again -- shallow.ts first, by claim order -- so deep.ts's trace meets the seven hops
    # shallow.ts's just walked, before its own thirteen.
    append(root, "chain", "deep.ts", "// touched\n")
    append(root, "chain", "chain/c13.ts", "// touched\n")
    work = sync_all(workspace, root)
    assert (work.mode, work.own, work.landed) == ("incremental", 2, 1)

    incremental = use_snapshot(workspace)
    with fresh_index(settings, root, tmp_path, "fresh") as fresh:
        assert incremental == use_snapshot(fresh)
    assert DEEP not in uses(workspace)


def test_a_use_records_every_export_table_its_trace_read(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Up to where the trace stopped: ``deep.ts`` read ``c00``-``c12``; the bound cut ``c13``."""
    build(workspace, tmp_path, {"chain": chain_repo()})
    reads = read_snapshot(workspace)
    assert [read for consumer, read in reads if consumer == "chain:shallow.ts"] == [
        f"chain:chain/c{i:02d}.ts" for i in range(7, 14)
    ]
    assert [read for consumer, read in reads if consumer == "chain:deep.ts"] == [
        f"chain:chain/c{i:02d}.ts" for i in range(0, 13)
    ]


# -- I1, I2: the consumer changes --------------------------------------------------------


def test_i1_a_consumer_s_own_edit_resolves_only_its_own_uses(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(
        root,
        "mono",
        CONSUMER,
        "export function useUtil() {\n  return util + local;\n}\n",
        "export function useAlphaAgain() {\n  return Alpha + Beta;\n}\n"
        "export function useUtil() {\n  return util;\n}\n",
    )
    work = sync_all(mono, root)

    found = uses(mono)
    assert _use(f"{CONSUMER}#useAlphaAgain", f"{KIT}/a.ts#Alpha") in found
    assert _use(f"{CONSUMER}#useUtil", f"{WEB}/local.ts#local") not in found
    assert (work.mode, work.files_invalidated, work.own) == ("incremental", 1, 1)
    assert (work.landed, work.readers, work.repository, work.package) == (0, 0, 0, 0)
    claims = mono.database.execute(
        "SELECT COUNT(*) AS n FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "WHERE u.edge_type = 'USES' AND u.target_key != 'export-table' AND f.rel_path = ?",
        (CONSUMER,),
    ).fetchone()["n"]
    assert work.claims_examined == claims
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh")


def test_i2_changing_what_a_consumer_imports_moves_its_edge(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(
        root,
        "mono",
        CONSUMER,
        'import { Alpha, Beta, Renamed, Def, NS } from "@mono/kit";\n',
        'import { Alpha, Renamed, Def, NS } from "@mono/kit";\nimport { Beta } from "./local";\n',
    )
    work = sync_all(mono, root)

    found = uses(mono)
    assert _use(f"{CONSUMER}#useBeta", f"{WEB}/local.ts#Beta") in found
    assert _use(f"{CONSUMER}#useBeta", f"{KIT}/b.ts#Beta") not in found
    assert (work.files_invalidated, work.own, work.readers, work.repository) == (1, 1, 0, 0)
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh")


# -- I3, I4: the provider changes --------------------------------------------------------


def test_i3_a_barrel_change_re_resolves_the_files_that_read_it(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """A renamed re-export, then a second ``export *`` path to ``Alpha``: both reach readers."""
    root = tmp_path / "repos"
    unrelated = use_edge_rows(mono, f"{WEB}/unrelated.ts")

    _replace(root, "mono", f"{KIT}/index.ts", "Gamma as Renamed", "Gamma as Other")
    work = sync_all(mono, root)
    assert _use(f"{CONSUMER}#useRenamed", f"{KIT}/c.ts#Gamma") not in uses(mono)
    # the barrel itself, and consumer.ts, later.ts and other.ts, which all read it
    assert (work.mode, work.own, work.readers, work.repository) == ("incremental", 1, 3, 0)
    assert work.files_invalidated == 4
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh-alias")

    # b.ts now also exports Alpha: two `export *` paths disagree, so Alpha names nothing.
    append(root, "mono", f"{KIT}/b.ts", "export const Alpha = 10;\n")
    work = sync_all(mono, root)
    assert _use(f"{CONSUMER}#useAlpha", f"{KIT}/a.ts#Alpha") not in uses(mono)
    # every reader of b.ts: its export * branch was searched even where it held nothing
    assert (work.own, work.readers, work.repository) == (1, 3, 0)
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh-ambiguous")

    # The barrel stops re-exporting b.ts wholesale: Beta is no longer reachable through it.
    _replace(root, "mono", f"{KIT}/index.ts", 'export * from "./b";\n', "")
    work = sync_all(mono, root)
    assert not any(target == f"{KIT}/b.ts#Beta" for _, target, _ in uses(mono))
    assert (work.own, work.readers, work.repository) == (1, 3, 0)
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh-star")
    assert use_edge_rows(mono, f"{WEB}/unrelated.ts") == unrelated


def test_i4_a_body_edit_re_resolves_what_landed_in_the_file_and_nothing_else(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """Every declaration below the edit moves, so every use landing in the file moves too.

    ``other.ts`` read ``a.ts`` on its way to ``b.ts`` but landed elsewhere, and what
    ``a.ts`` exports is unchanged: its edge is left exactly as it was.
    """
    root = tmp_path / "repos"
    other = use_edge_rows(mono, f"{WEB}/other.ts")
    _replace(
        root,
        "mono",
        f"{KIT}/a.ts",
        "  return Alpha;\n",
        "  const doubled = Alpha * 2;\n  return doubled / 2;\n",
    )
    work = sync_all(mono, root)

    assert _use(f"{WEB}/later.ts#useLater", f"{KIT}/a.ts#Later") in uses(mono)
    # a.ts itself, and consumer.ts and later.ts, whose uses of Alpha and Later landed in it
    assert (work.files_invalidated, work.own, work.landed, work.readers) == (3, 1, 2, 0)
    assert work.repository == 0
    assert use_edge_rows(mono, f"{WEB}/other.ts") == other
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh")


# -- I5: module resolution ---------------------------------------------------------------


def test_i5_a_tsconfig_paths_change_re_resolves_its_repository(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(root, "mono", "tsconfig.json", "apps/web/src/*", "apps/web/src/alt/*")
    work = sync_all(mono, root)

    found = uses(mono)
    assert _use(f"{CONSUMER}#useUtil", f"{WEB}/alt/util.ts#util") in found
    assert _use(f"{CONSUMER}#useUtil", f"{WEB}/util.ts#util") not in found
    assert (work.mode, work.repositories_invalidated) == ("incremental", 1)
    assert work.repository == len(mono_repo())
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh")


# -- I6, I7, I10: across repositories ----------------------------------------------------


def test_i6_moving_a_package_subpath_re_resolves_its_consumers_elsewhere(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "provider", "package.json", PROVIDER_MANIFEST_MOVED)
    work = sync_all(linked, root)
    found = uses(linked)
    assert _use("src/use.ts#useSub", "src/sub2.ts#Sub") in found
    assert _use("src/use.ts#useSub", "src/sub.ts#Sub") not in found
    # the provider's repository and the one that declares its package
    assert (work.mode, work.repositories_invalidated) == ("incremental", 2)
    assert_equals_fresh(linked, settings, root, tmp_path, "fresh-moved")

    moved = json.loads(PROVIDER_MANIFEST_MOVED)
    del moved["exports"]["./sub"]
    edit(root, "provider", "package.json", json.dumps(moved, indent=2) + "\n")
    sync_all(linked, root)
    assert not any(target.endswith("#Sub") for _, target, _ in uses(linked))
    assert_equals_fresh(linked, settings, root, tmp_path, "fresh-dropped")


def test_i7_a_second_provider_makes_the_crossing_ambiguous_and_its_removal_restores_it(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    crossing = _use("src/use.ts#usePea", "src/pee.ts#Pee")
    assert crossing in uses(linked)

    add_repo(root, "dup", dup_repo("@fx/provider"))
    work = sync_all(linked, root)
    assert crossing not in uses(linked)
    assert (work.mode, work.packages_invalidated) == ("incremental", 1)
    assert_equals_fresh(linked, settings, root, tmp_path, "fresh-two")

    edit(root, "dup", "package.json", dup_repo("@fx/other")["package.json"])
    work = sync_all(linked, root)
    assert crossing in uses(linked)
    assert (work.mode, work.packages_invalidated) == ("incremental", 2)
    assert_equals_fresh(linked, settings, root, tmp_path, "fresh-renamed")

    edit(root, "dup", "package.json", dup_repo("@fx/provider")["package.json"])
    sync_all(linked, root)
    assert crossing not in uses(linked)

    # Forgetting the repository takes its provision with it.
    remove_repo(root, "dup")
    work = sync_all(linked, root)
    assert crossing in uses(linked)
    assert (work.mode, work.packages_invalidated) == ("incremental", 1)
    assert_equals_fresh(linked, settings, root, tmp_path, "fresh-removed")


def test_i10_an_export_change_in_the_provider_reaches_its_reader_in_another_repository(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    bystander = use_edge_rows(linked, "src/bystander.ts")
    _replace(root, "provider", "src/index.ts", "./pee", "./pee2")
    work = sync_all(linked, root)

    assert _use("src/use.ts#usePea", "src/pee2.ts#Pee") in uses(linked)
    # the barrel itself, and use.ts, which read it from the other repository
    assert (work.own, work.readers, work.repository, work.package) == (1, 1, 0, 0)
    assert use_edge_rows(linked, "src/bystander.ts") == bystander
    assert_equals_fresh(linked, settings, root, tmp_path, "fresh")

    # the change's impact is the same answered from either database
    request = ImpactRequest(
        context=CapabilityContext(scope=CapabilityScope.GLOBAL),
        seed="sym:provider:src/pee2.ts:Pee:1",
    )
    incremental = CapabilityService(linked).impact(request)
    with fresh_index(settings, root, tmp_path, "fresh-impact") as fresh:
        full = CapabilityService(fresh).impact(request)
    assert incremental.candidates, "the crossing is a candidate"
    assert [c.model_dump() for c in incremental.candidates] == [
        c.model_dump() for c in full.candidates
    ]


# -- I8, I9: files come and go, and files nothing uses -----------------------------------


def test_i8_adding_deleting_and_renaming_files_re_resolves_their_repository(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    # The file consumer.ts has imported all along. No trace read it and no use landed in
    # it, so only the file set says consumer.ts must be resolved again.
    edit(root, "mono", f"{WEB}/fresh.ts", "export const Fresh = 11;\n")
    work = sync_all(mono, root)
    assert _use(f"{CONSUMER}#useFresh", f"{WEB}/fresh.ts#Fresh") in uses(mono)
    assert (work.mode, work.repositories_invalidated) == ("incremental", 1)
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh-added")

    (root / "mono" / KIT / "a.ts").unlink()
    work = sync_all(mono, root)
    assert not any(target.startswith(f"{KIT}/a.ts#") for _, target, _ in uses(mono))
    assert (work.mode, work.repositories_invalidated) == ("incremental", 1)
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh-deleted")

    (root / "mono" / KIT / "b.ts").rename(root / "mono" / KIT / "b2.ts")
    work = sync_all(mono, root)
    assert _use(f"{CONSUMER}#useBeta", f"{KIT}/b.ts#Beta") not in uses(mono)
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh-renamed")


def test_i9_an_unrelated_edit_leaves_every_other_use_untouched(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    before = {path: use_edge_rows(mono, path) for path in (CONSUMER, f"{WEB}/later.ts")}
    append(root, "mono", f"{WEB}/unrelated.ts", "export function lonelier() {\n  return 2;\n}\n")
    work = sync_all(mono, root)

    assert (work.mode, work.files_invalidated, work.own) == ("incremental", 1, 1)
    assert (work.landed, work.readers, work.repository, work.package) == (0, 0, 0, 0)
    assert (work.claims_examined, work.edges_deleted, work.edges_inserted) == (0, 0, 0)
    assert {path: use_edge_rows(mono, path) for path in before} == before
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh")


def test_the_sync_totals_count_every_use_kept_or_resolved_again(
    mono: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    append(root, "mono", f"{WEB}/unrelated.ts", "// touched\n")
    incremental = sync(mono, mono.resolver().resolve(root))
    with fresh_index(settings, root, tmp_path, "fresh") as fresh:
        full = fresh.sync_engine().link_global()
    assert (incremental.linked_references, incremental.unresolved_references) == (
        full.resolved,
        full.unresolved,
    )


# -- I11, I12: when the kept edges cannot be trusted ---------------------------------------


class _InjectedError(Exception):
    pass


def test_i11_a_sync_that_died_before_its_commit_leaves_the_next_one_resolving_everything(
    mono: Workspace, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repos"
    _replace(root, "mono", f"{KIT}/index.ts", "Gamma as Renamed", "Gamma as Other")

    def failing_commit(self: ReferenceLinker, plan: object) -> object:
        raise _InjectedError("the link commit failed")

    monkeypatch.setattr(ReferenceLinker, "commit_global", failing_commit)
    with pytest.raises(_InjectedError):
        sync_all(mono, root)
    monkeypatch.undo()

    # Nothing changed on disk since; the file facts already committed are current.
    work = sync_all(mono, root)
    assert (work.mode, work.reason) == ("global", "the previous sync did not finish")
    assert_equals_fresh(mono, settings, root, tmp_path, "fresh")


@pytest.mark.parametrize(
    ("state", "reason"),
    [(None, "no USES edges recorded yet"), ("complete:0", "USES resolution changed")],
)
def test_i12_edges_not_known_complete_are_all_resolved_again(
    mono: Workspace, tmp_path: Path, state: str | None, reason: str
) -> None:
    root = tmp_path / "repos"
    with mono.database.transaction():
        if state is None:
            mono.database.execute("DELETE FROM meta WHERE key = 'uses_links'")
        else:
            mono.database.set_meta("uses_links", state)
    work = sync_all(mono, root)
    assert (work.mode, work.reason) == ("global", reason)
    assert sync_all(mono, root).mode == "incremental"
